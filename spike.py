"""Phase 0 spike — throwaway.

Proves four things before any watcher, database or state machine exists:

  1. OAuth works end to end with only the drive.file scope.
  2. How much Drive quota is actually left (decision D14 — free 15 GB tier).
  3. The app can create its own "Game Clips" folder and find it again.
  4. A resumable upload works, and hands back a session URI.

(4) matters most. The session URI is what the whole Phase 2 durability design
rests on — persist it on the row, resume after a crash. If it does not come
back here, that design needs revisiting before it is written.

Nothing in this file survives into the real app. See IMPLEMENTATION-PLAN.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# Only ever this scope. Never drive. See IMPLEMENTATION-PLAN.md roadblock 3.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

APP_DIR = Path(os.environ["LOCALAPPDATA"]) / "climp"
CLIENT_SECRET = APP_DIR / "client_secret.json"
TOKEN = APP_DIR / "token.json"
STATE = APP_DIR / "spike_state.json"

CLIPS_ROOT = Path(os.environ["USERPROFILE"]) / "Videos" / "NVIDIA"
FOLDER_NAME = "Game Clips"
CHUNK = 16 * 1024 * 1024  # 16 MB, mid-range of the blueprint's 8-32 MB


def human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:,.1f} {unit}"
        x /= 1024
    return f"{x:.1f} TB"


def authenticate() -> Credentials:
    if not CLIENT_SECRET.exists():
        sys.exit(f"No client secret at {CLIENT_SECRET}\nDownload it from the Google Cloud console (Clients -> your Desktop app).")

    creds = None
    if TOKEN.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
        except ValueError:
            creds = None  # corrupt or written by an older scope set

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            print("Refreshed the stored token.")
        except RefreshError:
            # D13: the app is in OAuth Testing mode, so this happens about weekly.
            # In the real app this becomes AuthExpired and the tray asks you to sign in.
            print("Stored token could not be refreshed (expected roughly weekly in Testing mode).")
            creds = None

    if not creds or not creds.valid:
        print("Opening a browser to sign in. Expect an 'unverified app' warning -> Advanced -> Go to ...")
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
        print(f"Token saved to {TOKEN}")

    return creds


def report_quota(svc) -> int | None:
    """Print quota. Returns free bytes, or None if the account has no stated limit."""
    about = svc.about().get(fields="storageQuota,user(emailAddress)").execute()
    q = about["storageQuota"]
    email = about.get("user", {}).get("emailAddress", "?")

    usage = int(q.get("usage", 0))
    in_drive = int(q.get("usageInDrive", 0))
    limit = int(q["limit"]) if q.get("limit") else None

    print(f"\n  Account      : {email}")
    print(f"  Used total   : {human(usage)}   (Drive, Gmail and Photos combined)")
    print(f"  Of which Drive: {human(in_drive)}")

    if limit is None:
        print("  Limit        : none reported")
        return None

    free = limit - usage
    print(f"  Limit        : {human(limit)}")
    print(f"  Free         : {human(free)}")
    print(f"  Headroom     : about {free // (225 * 1024 * 1024)} clips at your 225 MB average")
    return free


def ensure_folder(svc) -> str:
    if STATE.exists():
        folder_id = json.loads(STATE.read_text(encoding="utf-8")).get("folder_id")
        if folder_id:
            try:
                got = svc.files().get(fileId=folder_id, fields="id,name,trashed").execute()
                if not got.get("trashed"):
                    print(f"\nUsing existing folder '{got['name']}' ({folder_id})")
                    return folder_id
                print("\nSaved folder is in the trash; making a new one.")
            except HttpError as e:
                if e.resp.status != 404:
                    raise
                print("\nSaved folder ID no longer resolves; making a new one.")

    # Under drive.file this only ever sees folders THIS app created, which is
    # exactly why the folder must not be made by hand in the Drive web UI.
    res = svc.files().list(
        q=f"mimeType='application/vnd.google-apps.folder' and name='{FOLDER_NAME}' and trashed=false",
        spaces="drive",
        fields="files(id,name)",
    ).execute()
    files = res.get("files", [])
    if files:
        folder_id = files[0]["id"]
        print(f"\nFound folder '{FOLDER_NAME}' ({folder_id})")
    else:
        created = svc.files().create(
            body={"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
            fields="id",
        ).execute()
        folder_id = created["id"]
        print(f"\nCreated folder '{FOLDER_NAME}' ({folder_id})")

    STATE.write_text(json.dumps({"folder_id": folder_id}, indent=2), encoding="utf-8")
    print(f"Folder ID saved to {STATE}")
    return folder_id


def pick_clip(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            sys.exit(f"Not a file: {p}")
        return p

    clips = sorted(CLIPS_ROOT.rglob("*.mp4"), key=lambda p: p.stat().st_size)
    if not clips:
        sys.exit(f"No .mp4 found under {CLIPS_ROOT}. Pass one with --clip.")
    # Smallest on purpose: this is a test upload against a 15 GB shared quota.
    return clips[0]


def upload(svc, clip: Path, folder_id: str) -> None:
    total = clip.stat().st_size
    media = MediaFileUpload(str(clip), chunksize=CHUNK, resumable=True, mimetype="video/mp4")
    req = svc.files().create(
        body={"name": clip.name, "parents": [folder_id]},
        media_body=media,
        fields="id,name,size,webViewLink",
    )

    print(f"\nUploading {clip.name} ({human(total)})")
    shown_uri = False
    response = None
    while response is None:
        status, response = req.next_chunk()
        if not shown_uri and req.resumable_uri:
            # THE thing this spike exists to prove. Phase 2 persists this on the row.
            print(f"  session URI: {req.resumable_uri[:90]}...")
            shown_uri = True
        if status:
            print(f"  {int(status.progress() * 100):3d}%")

    print("\n  Uploaded.")
    print(f"  file id : {response['id']}")
    print(f"  size    : {human(int(response.get('size', total)))}")
    print(f"  link    : {response.get('webViewLink')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 0 spike: prove Drive auth and resumable upload.")
    ap.add_argument("--clip", help="Path to a clip. Default: the smallest .mp4 under the NVIDIA folder.")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    ap.add_argument("--no-upload", action="store_true", help="Auth, quota and folder only. Uploads nothing.")
    args = ap.parse_args()

    APP_DIR.mkdir(parents=True, exist_ok=True)

    svc = build("drive", "v3", credentials=authenticate(), cache_discovery=False)
    free = report_quota(svc)
    folder_id = ensure_folder(svc)

    if args.no_upload:
        print("\n--no-upload set. Stopping here.")
        return

    clip = pick_clip(args.clip)
    size = clip.stat().st_size
    print(f"\nAbout to upload:\n  {clip}\n  {human(size)}")

    if free is not None and size > free:
        sys.exit(f"\nThat does not fit. Free: {human(free)}, needed: {human(size)}.")

    if not args.yes:
        if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled. Nothing uploaded.")
            return

    upload(svc, clip, folder_id)


if __name__ == "__main__":
    main()
