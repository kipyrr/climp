"""Thin Google Drive client. Knows nothing about states, rows or retries.

It raises; uploader.py classifies. That split is what lets the upload worker be
tested against a fake without a network, and what keeps Drive-specific
knowledge out of the state machine.

Not a sixth component -- it holds no state and reads no rows.

See IMPLEMENTATION-PLAN.md section 2.8.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

try:
    import win32crypt
except ImportError:  # non-Windows dev machine
    win32crypt = None

log = logging.getLogger(__name__)

# Only ever this. drive would be a sensitive scope requiring verification, and
# drive.file means a bug here cannot touch anything the app did not create.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

CHUNK_BYTES = 16 * 1024 * 1024  # blueprint says 8-32 MB
FOLDER_MIME = "application/vnd.google-apps.folder"


class SessionExpired(Exception):
    """The resumable session URI is no longer valid.

    Sessions last about a week, but Google also drops them on its own schedule.
    The row keeps its file; only the session is lost, so the fix is to clear
    the URI and start a fresh upload.
    """


@dataclass
class ResumableUpload:
    """What build_upload hands back.

    `request` is None when the server already holds the whole file -- which
    happens if the process died between the final chunk and marking the row
    done. In that case `completed` carries the metadata and there is nothing
    left to send.
    """

    request: object | None
    completed: dict | None = None


class AuthExpired(Exception):
    """The stored refresh token is dead and only a human can fix it.

    D13: the app runs in OAuth Testing mode, so this fires roughly weekly. It
    is a normal condition, not a crash, and it must never consume a clip's
    retry budget.
    """


class DriveClient:
    def __init__(self, client_secret: Path, token_path: Path) -> None:
        self.client_secret = client_secret
        self.token_path = token_path
        self._service = None

    # --- auth -------------------------------------------------------------

    @property
    def encrypted_token_path(self) -> Path:
        """DPAPI-encrypted token. Sits beside the plaintext path it replaces."""
        return self.token_path.with_suffix(".bin")

    def _load_credentials(self) -> Credentials | None:
        encrypted = self.encrypted_token_path
        if encrypted.exists():
            try:
                blob = encrypted.read_bytes()
                _desc, plaintext = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
                return Credentials.from_authorized_user_info(json.loads(plaintext.decode("utf-8")), SCOPES)
            except Exception:
                # Encrypted by another user or machine, or corrupt. DPAPI keys
                # are bound to the Windows account, so this is not recoverable
                # here -- discard it and sign in again.
                log.warning("could not decrypt %s; discarding it", encrypted)
                return None

        # Migration: a plaintext token from before encryption existed.
        if self.token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            except ValueError:
                log.warning("token file at %s is unreadable; discarding", self.token_path)
                return None
            log.info("migrating the plaintext token to DPAPI-encrypted storage")
            self._save(creds)
            try:
                self.token_path.unlink()
            except OSError:
                log.warning("could not remove the plaintext token at %s", self.token_path)
            return creds

        return None

    def authorise(self) -> None:
        """Load and refresh silently. Raises AuthExpired if a human is needed."""
        creds = self._load_credentials()
        if creds is None:
            raise AuthExpired("no stored token; sign in required")

        if creds.expired:
            if not creds.refresh_token:
                raise AuthExpired("stored token has no refresh token; sign in required")
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise AuthExpired(f"refresh failed ({e}); sign in required") from e
            self._save(creds)

        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def reauthorise(self) -> None:
        """Run the interactive consent flow. Opens a browser; blocks."""
        if not self.client_secret.exists():
            # Otherwise this surfaces as a bare FileNotFoundError naming a path,
            # which says nothing about what to do next.
            raise AuthExpired(
                f"The Google client secret is missing from {self.client_secret}. "
                "Download it from the Google Cloud console (Clients -> your Desktop app) "
                "and save it there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secret), SCOPES)
        creds = flow.run_local_server(port=0)
        self._save(creds)
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _save(self, creds: Credentials) -> None:
        """Encrypt with DPAPI, keyed to this Windows account (roadblock 11).

        A refresh token in plaintext JSON is a standing grant to this Drive
        folder for anything that can read the file. DPAPI ties it to the logged
        in user, so copying the file to another machine or account yields
        nothing usable.
        """
        target = self.encrypted_token_path
        target.parent.mkdir(parents=True, exist_ok=True)

        if win32crypt is None:
            log.warning("win32crypt unavailable; storing the token unencrypted")
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
            return

        blob = win32crypt.CryptProtectData(
            creds.to_json().encode("utf-8"), "climp OAuth token", None, None, None, 0
        )
        # Write then replace, so an interrupted save cannot leave a truncated
        # token that would force a needless sign-in.
        tmp = target.with_suffix(".bin.tmp")
        tmp.write_bytes(blob)
        tmp.replace(target)

    @property
    def service(self):
        if self._service is None:
            raise AuthExpired("not authorised")
        return self._service

    # --- quota ------------------------------------------------------------

    def storage_quota(self) -> dict[str, int | None]:
        q = self.service.about().get(fields="storageQuota").execute()["storageQuota"]
        limit = int(q["limit"]) if q.get("limit") else None
        usage = int(q.get("usage", 0))
        return {"limit": limit, "usage": usage, "free": None if limit is None else limit - usage}

    # --- folder -----------------------------------------------------------

    def folder_exists(self, folder_id: str) -> bool:
        try:
            got = self.service.files().get(fileId=folder_id, fields="id,trashed").execute()
        except HttpError as e:
            if e.resp.status == 404:
                return False
            raise
        return not got.get("trashed", False)

    def ensure_folder(self, name: str) -> str:
        res = self.service.files().list(
            q=f"mimeType='{FOLDER_MIME}' and name='{name}' and trashed=false",
            spaces="drive",
            fields="files(id)",
        ).execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]
        created = self.service.files().create(
            body={"name": name, "mimeType": FOLDER_MIME}, fields="id"
        ).execute()
        return created["id"]

    def delete_file(self, file_id: str) -> None:
        """Permanently delete a file this app uploaded.

        Under drive.file this cannot reach anything the app did not create, so
        the blast radius is the Game Clips folder and nothing else.
        """
        self.service.files().delete(fileId=file_id).execute()

    # --- upload -----------------------------------------------------------

    def build_upload(self, path: Path, folder_id: str, session_uri: str | None = None) -> ResumableUpload:
        """Return a resumable request, genuinely continuing an existing session.

        Setting `resumable_uri` alone is NOT enough, and this was measured
        rather than assumed. A fresh request object starts with its progress
        counter at zero, and the client library never asks the server where it
        got to, so it re-sends the file from the beginning under the old
        session. A 120 MB gate test resumed "successfully" while actually
        uploading every byte twice.

        So we do what the blueprint prescribed: ask Google for the received
        byte count first, then tell the request to start there.
        """
        media = MediaFileUpload(str(path), chunksize=CHUNK_BYTES, resumable=True, mimetype="video/mp4")
        request = self.service.files().create(
            body={"name": path.name, "parents": [folder_id]},
            media_body=media,
            fields="id,name,size,webViewLink",
        )
        if not session_uri:
            return ResumableUpload(request=request)

        offset = self.received_bytes(session_uri, media.size())
        if isinstance(offset, dict):
            return ResumableUpload(request=None, completed=offset)

        request.resumable_uri = session_uri
        request.resumable_progress = offset
        return ResumableUpload(request=request)

    def received_bytes(self, session_uri: str, total: int) -> int | dict:
        """Ask how much of this session the server already holds.

        A zero-length PUT with `Content-Range: bytes */<total>` is the query
        form of the resumable protocol. Returns the byte offset to continue
        from, or the finished file's metadata if the server already has it all.
        """
        # _http carries the credentials the service was built with.
        resp, content = self.service._http.request(
            session_uri, "PUT", body=b"", headers={"Content-Range": f"bytes */{total}"}
        )

        if resp.status in (200, 201):
            return json.loads(content.decode("utf-8"))

        if resp.status == 308:
            received = resp.get("range")
            # No Range header means the server has nothing yet.
            return int(received.split("-")[1]) + 1 if received else 0

        if resp.status in (404, 410):
            raise SessionExpired(f"Drive no longer recognises the upload session ({resp.status})")

        raise HttpError(resp, content, uri=session_uri)
