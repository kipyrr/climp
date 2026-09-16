"""Process entry point. Starts the loops in the one order that is safe.

Startup ordering is load-bearing in two places:

  * Recovery of stranded uploads runs before anything can claim rows.
  * The reconciler runs TO COMPLETION before the watcher is armed. Reversed, a
    file created during the scan is missed by both.

Shutdown is deliberately abrupt. Nothing needs flushing, because every state
change was committed when it happened. A row abandoned at uploading is not a
loss -- it is exactly what recovery handles on the next start.

The tray is Phase 3. Until then this runs headless and prints a status line.

See IMPLEMENTATION-PLAN.md section 2.10.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from pathlib import Path

from clipsync import config as config_module
from clipsync.db import Db
from clipsync.drive import AuthExpired, DriveClient
from clipsync.reconciler import reconcile
from clipsync.settle import POLL_SECONDS, DbSettleChecker
from clipsync.uploader import UploadWorker
from clipsync.watcher import Watcher

log = logging.getLogger("clipsync")


def human_gb(n: int | None) -> str:
    return "unknown" if n is None else f"{n / 1024**3:.2f} GB"


class Application:
    def __init__(self, cfg: config_module.Config, allow_interactive_auth: bool = True) -> None:
        self.cfg = cfg
        self.allow_interactive_auth = allow_interactive_auth
        self.stop = threading.Event()
        self.db = Db(cfg.db_path)
        self.client = DriveClient(cfg.client_secret_path, cfg.token_path)
        self.watcher: Watcher | None = None
        self._threads: list[threading.Thread] = []

    # --- startup ----------------------------------------------------------

    def start(self) -> None:
        self.cfg.app_dir.mkdir(parents=True, exist_ok=True)

        # 1. Schema first: everything below writes rows.
        self.db.init_schema()

        # 2. Auth before the queue moves, so a broken token is a startup error
        #    rather than a queue of confusing failures.
        self._authorise()
        self._resolve_folder()
        self._report_quota()

        # 3. Rows a crash, kill or sleep left mid-upload. Before any worker runs.
        self.db.recover_stranded_uploads()

        # 4. Reconcile to completion. Before the watcher.
        reconcile(self.db, self.cfg.clips_root, backfill_since=self.cfg.backfill_since)

        # 5. Only now is the folder live.
        self.watcher = Watcher(self.cfg.clips_root, on_candidate=self.db.insert_candidate)
        self.watcher.start()

        # 6. The polling loops.
        self._spawn("settle", self._settle_loop)
        for i in range(self.cfg.concurrency):
            self._spawn(f"upload-{i}", self._upload_loop)
        self._spawn("sweeper", self._sweep_loop)

    def _authorise(self) -> None:
        try:
            self.client.authorise()
        except AuthExpired as e:
            log.warning("%s", e)
            if not self.allow_interactive_auth:
                raise
            log.info("opening a browser to sign in")
            self.client.reauthorise()

    def _resolve_folder(self) -> None:
        folder_id = self.cfg.drive_folder_id
        if folder_id and self.client.folder_exists(folder_id):
            self.folder_id = folder_id
            return
        if folder_id:
            log.warning("configured Drive folder %s is gone; recreating", folder_id)
        self.folder_id = self.client.ensure_folder(self.cfg.drive_folder_name)
        config_module.save(
            config_module.Config(
                clips_root=self.cfg.clips_root,
                drive_folder_id=self.folder_id,
                drive_folder_name=self.cfg.drive_folder_name,
                backfill_since=self.cfg.backfill_since,
                concurrency=self.cfg.concurrency,
                max_attempts=self.cfg.max_attempts,
                settle_timeout_seconds=self.cfg.settle_timeout_seconds,
                stale_upload_seconds=self.cfg.stale_upload_seconds,
                app_dir=self.cfg.app_dir,
            ),
            self.cfg.app_dir / "config.toml",
        )
        log.info("Drive folder: %s", self.folder_id)

    def _report_quota(self) -> None:
        """Roadblock 2. The app reads real headroom rather than assuming any."""
        try:
            q = self.client.storage_quota()
        except Exception as e:  # noqa: BLE001 - never let a quota read block startup
            log.warning("could not read Drive quota: %s", e)
            return
        free = q["free"]
        log.info("Drive: %s used of %s, %s free", human_gb(q["usage"]), human_gb(q["limit"]), human_gb(free))
        if free is not None and free < 1024**3:
            log.warning("less than 1 GB of Drive free - uploads will start failing soon")

    # --- loops ------------------------------------------------------------

    def _spawn(self, name: str, target) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _settle_loop(self) -> None:
        checker = DbSettleChecker(
            self.db,
            backfill_since=self.cfg.backfill_since,
            timeout_seconds=self.cfg.settle_timeout_seconds,
        )
        while not self.stop.is_set():
            try:
                checker.poll_once()
            except Exception:
                log.exception("settle pass failed")
            self.stop.wait(POLL_SECONDS)
        self.db.close()

    def _upload_loop(self) -> None:
        worker = UploadWorker(
            self.db,
            self.client,
            self.folder_id,
            stop_event=self.stop,
            max_attempts=self.cfg.max_attempts,
            on_auth_needed=self._auth_needed,
        )
        try:
            worker.run()
        except Exception:
            log.exception("upload worker died")
        finally:
            self.db.close()

    def _sweep_loop(self) -> None:
        """Roadblock 7. A slept machine wakes to dead sockets, not errors."""
        while not self.stop.is_set():
            self.stop.wait(60)
            if self.stop.is_set():
                break
            try:
                self.db.recover_stranded_uploads(stale_after_seconds=self.cfg.stale_upload_seconds)
            except Exception:
                log.exception("stale sweep failed")
        self.db.close()

    def _auth_needed(self, detail: str) -> None:
        # Phase 3 turns this into a tray prompt. For now it is loud in the log.
        log.error("SIGN IN AGAIN: %s", detail)

    # --- shutdown ---------------------------------------------------------

    def shutdown(self) -> None:
        log.info("stopping")
        self.stop.set()
        if self.watcher:
            self.watcher.stop()
        for t in self._threads:
            t.join(timeout=5)
        self.db.close()

    def status_line(self) -> str:
        c = self.db.counts_by_state()
        return (
            f"candidate {c['candidate']}  ready {c['ready']}  uploading {c['uploading']}  "
            f"done {c['done']}  failed {c['failed']}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="ClipSync - upload game clips to Drive automatically.")
    ap.add_argument("--clips-root", type=Path, help="Override the configured clips folder.")
    ap.add_argument("--seconds", type=float, help="Run for N seconds then stop. Omit for Ctrl-C.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = config_module.load()
    if args.clips_root:
        cfg = config_module.Config(**{**cfg.__dict__, "clips_root": args.clips_root})

    app = Application(cfg)
    signal.signal(signal.SIGINT, lambda *_: app.stop.set())

    app.start()
    log.info("running. Ctrl-C to stop.")

    started = time.monotonic()
    last = ""
    try:
        while not app.stop.is_set():
            if args.seconds is not None and (time.monotonic() - started) >= args.seconds:
                break
            line = app.status_line()
            if line != last:
                log.info(line)
                last = line
            app.stop.wait(2)
    finally:
        app.shutdown()
        print("\nfinal state:", app.status_line())


if __name__ == "__main__":
    main()
