"""Thin Google Drive client. Knows nothing about states, rows or retries.

It raises; uploader.py classifies. That split is what lets the upload worker be
tested against a fake without a network, and what keeps Drive-specific
knowledge out of the state machine.

Not a sixth component -- it holds no state and reads no rows.

See IMPLEMENTATION-PLAN.md section 2.8.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

log = logging.getLogger(__name__)

# Only ever this. drive would be a sensitive scope requiring verification, and
# drive.file means a bug here cannot touch anything the app did not create.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

CHUNK_BYTES = 16 * 1024 * 1024  # blueprint says 8-32 MB
FOLDER_MIME = "application/vnd.google-apps.folder"


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

    def _load_credentials(self) -> Credentials | None:
        if not self.token_path.exists():
            return None
        try:
            return Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        except ValueError:
            log.warning("token file at %s is unreadable; discarding", self.token_path)
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
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secret), SCOPES)
        creds = flow.run_local_server(port=0)
        self._save(creds)
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _save(self, creds: Credentials) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")

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

    # --- upload -----------------------------------------------------------

    def build_upload(self, path: Path, folder_id: str, session_uri: str | None = None):
        """Return a resumable request, resuming an existing session if given.

        Setting `resumable_uri` on the request makes the client library query
        Google for the byte count already received and continue from there,
        rather than starting a new session. Confirmed working in the Phase 0
        spike -- this is what makes crash recovery cheap instead of a full
        re-send of 225 MB.
        """
        media = MediaFileUpload(str(path), chunksize=CHUNK_BYTES, resumable=True, mimetype="video/mp4")
        request = self.service.files().create(
            body={"name": path.name, "parents": [folder_id]},
            media_body=media,
            fields="id,name,size,webViewLink",
        )
        if session_uri:
            request.resumable_uri = session_uri
        return request
