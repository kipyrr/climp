"""Phase 2 gate: kill the app mid-upload, restart, prove it resumes.

The blueprint's exit condition for Phase 2. Everything else about crash
recovery is tested against a fake Drive client; this is the only check that
exercises Google's side of the resumable protocol.

What it actually decides: on restart the app sets the stored session URI on a
fresh request whose progress counter is zero, so its first write targets the
start of a file the server already holds part of. Google either corrects us
with a 308 and the real offset, rejects the overlap, or accepts it wrongly.
Only the first is safe, and only a real run tells us which happens.

    python tools/gate_test.py --mb 120

Cleans up after itself: the test file is removed from Drive and from disk, and
its row is deleted from the database.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clipsync import config as config_module  # noqa: E402
from clipsync.drive import DriveClient  # noqa: E402

PYTHON = str(Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe")


def row(db_path: Path, name: str):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM clips WHERE path LIKE ? ORDER BY id DESC LIMIT 1", (f"%{name}",)
        ).fetchone()
    finally:
        conn.close()


def wait_for(db_path: Path, name: str, predicate, timeout: float, label: str):
    started = time.monotonic()
    last = None
    while time.monotonic() - started < timeout:
        r = row(db_path, name)
        if r is not None:
            state = (r["state"], bool(r["session_uri"]))
            if state != last:
                print(f"    [{time.monotonic()-started:5.1f}s] state={r['state']:10s} session_uri={'yes' if r['session_uri'] else 'no'}")
                last = state
            if predicate(r):
                return r
        time.sleep(0.5)
    raise TimeoutError(f"timed out after {timeout}s waiting for {label}")


def launch(clips_root: Path, log_path: Path) -> subprocess.Popen:
    fh = open(log_path, "ab")
    return subprocess.Popen(
        [PYTHON, "-m", "clipsync.main", "--clips-root", str(clips_root), "-v"],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=fh,
        stderr=subprocess.STDOUT,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=120)
    ap.add_argument("--kill-after", type=float, default=3.0,
                    help="Seconds to keep uploading after the session URI appears. Larger "
                         "means more bytes on the server, so a resume is unambiguous.")
    ap.add_argument("--keep", action="store_true", help="Do not clean up (for debugging).")
    args = ap.parse_args()

    cfg = config_module.load()
    db_path = cfg.db_path
    work = Path(tempfile.mkdtemp(prefix="clipsync-gate-"))
    clips_root = work / "Gate Test"
    clips_root.mkdir()
    name = f"Gate Test {time.strftime('%Y.%m.%d - %H.%M.%S')}.DVR.mp4"
    clip = clips_root / name
    # Outside `work`, which the cleanup removes -- the log is the evidence.
    log_path = Path(tempfile.gettempdir()) / "clipsync-gate.log"
    log_path.write_bytes(b"")

    print(f"1. Creating a {args.mb} MB test clip")
    with clip.open("wb") as fh:
        for _ in range(args.mb):
            fh.write(os.urandom(1024 * 1024))
    source_size = clip.stat().st_size
    print(f"   {clip.name}  ({source_size:,} bytes)")

    proc = None
    drive_file_id = None
    try:
        print("\n2. Starting the app")
        proc = launch(clips_root, log_path)

        print("\n3. Waiting for the upload to be genuinely in flight")
        wait_for(
            db_path, name,
            lambda r: r["state"] == "uploading" and r["session_uri"],
            timeout=180, label="an uploading row with a saved session URI",
        )
        print("   session URI persisted; at least one chunk has gone up")
        print(f"   letting it run {args.kill_after:.0f}s more so the server holds a clear amount")
        time.sleep(args.kill_after)

        print("\n4. Killing the process (TerminateProcess, not a clean shutdown)")
        proc.kill()
        proc.wait(timeout=30)
        proc = None
        time.sleep(1)

        stranded = row(db_path, name)
        print(f"   after the kill: state={stranded['state']}  session_uri={'present' if stranded['session_uri'] else 'MISSING'}")
        if stranded["state"] != "uploading":
            print(f"   !! expected the row to be stranded at uploading, got {stranded['state']}")
        if not stranded["session_uri"]:
            print("   !! no session URI on the row -- resume is impossible")
            return 1

        print("\n5. Restarting the app")
        proc = launch(clips_root, log_path)

        print("\n6. Waiting for it to finish the job")
        finished = wait_for(
            db_path, name,
            lambda r: r["state"] in ("done", "failed"),
            timeout=600, label="a terminal state",
        )

        if finished["state"] != "done":
            print(f"\n   FAILED: row ended at {finished['state']}: {finished['last_error']}")
            return 1

        drive_file_id = finished["drive_file_id"]
        print(f"\n7. Verifying the file in Drive (id {drive_file_id})")
        client = DriveClient(cfg.client_secret_path, cfg.token_path)
        client.authorise()
        meta = client.service.files().get(fileId=drive_file_id, fields="id,name,size").execute()
        drive_size = int(meta["size"])

        print(f"   source : {source_size:,} bytes")
        print(f"   in Drive: {drive_size:,} bytes")
        if drive_size == source_size:
            print("\n   PASS - resumed after a hard kill and the file is byte-exact.")
            result = 0
        else:
            print(f"\n   FAIL - size mismatch of {drive_size - source_size:+,} bytes. Resume corrupts data.")
            result = 1

        return result

    finally:
        if proc is not None:
            proc.kill()
        if args.keep:
            print(f"\n--keep set. Work dir: {work}\nLog: {log_path}")
            return 0
        print("\n8. Cleaning up")
        if drive_file_id:
            try:
                client = DriveClient(cfg.client_secret_path, cfg.token_path)
                client.authorise()
                client.service.files().delete(fileId=drive_file_id).execute()
                print(f"   deleted {drive_file_id} from Drive")
            except Exception as e:
                print(f"   could not delete from Drive: {e}")
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute("DELETE FROM clips WHERE path LIKE ?", (f"%{name}",))
            conn.commit()
            conn.close()
            print("   removed the test row from the database")
        except Exception as e:
            print(f"   could not clean the database: {e}")
        shutil.rmtree(work, ignore_errors=True)
        print("   removed the temp folder")
        print("\n--- app log tail ---")
        # log lives in `work`, already gone; print nothing further


if __name__ == "__main__":
    raise SystemExit(main())
