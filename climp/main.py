"""Process entry point. Starts the loops in the one order that is safe.

Startup ordering is load-bearing in two places:

  * Recovery of stranded uploads runs before anything can claim rows.
  * The reconciler runs TO COMPLETION before the watcher is armed. Reversed, a
    file created during the scan is missed by both.

Shutdown is deliberately abrupt. Nothing needs flushing, because every state
change was committed when it happened. A row abandoned at uploading is not a
loss -- it is exactly what recovery handles on the next start.

The tray owns the main thread (pystray requires it on Windows), so every
other loop is a daemon thread. --no-tray runs headless for testing.

See IMPLEMENTATION-PLAN.md section 2.10.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from dataclasses import replace
from pathlib import Path

from climp import activity
from climp import config as config_module
from climp import logging_setup
from climp import retention
from climp.db import Db
from climp.drive import AuthExpired, DriveClient
from climp.reconciler import reconcile
from climp.settle import POLL_SECONDS, DbSettleChecker
from climp.uploader import UploadWorker
from climp.tray import Tray
from climp.watcher import Watcher

log = logging.getLogger("climp")

QUOTA_REFRESH_SECONDS = 300
# A gap this large between wall time and monotonic time means Windows slept.
SLEEP_DETECT_SECONDS = 30.0


def human_gb(n: int | None) -> str:
    return "unknown" if n is None else f"{n / 1024**3:.2f} GB"


class RetentionControl:
    """What the tray is allowed to change about retention.

    Settings live in config.toml, not in memory, so a change survives a
    restart -- and the sweep loop re-reads them each cycle, so a change takes
    effect without one. The tray never calls the sweep itself; it sets an
    event and the loop does the work, keeping deletion in one place.
    """

    def __init__(self, db: Db, config_path: Path, cfg: config_module.Config) -> None:
        self.db = db
        self.config_path = config_path
        self._cfg = cfg
        self.trigger = threading.Event()

    def reload(self) -> config_module.Config:
        try:
            self._cfg = config_module.load(self.config_path)
        except Exception:
            log.exception("could not re-read config; keeping the previous settings")
        return self._cfg

    @property
    def enabled(self) -> bool:
        return self._cfg.retention_enabled

    @property
    def keep_newest(self) -> int:
        return self._cfg.retention_keep_newest

    @property
    def min_age_hours(self) -> float:
        return self._cfg.retention_min_age_hours

    def in_drive_count(self) -> int:
        try:
            return self.db.in_drive_count()
        except Exception:
            log.debug("in_drive_count failed", exc_info=True)
            return 0

    def set_keep(self, n: int) -> None:
        self._cfg = config_module.update(self.config_path, retention_enabled=True, retention_keep_newest=n)

    def set_enabled(self, enabled: bool) -> None:
        self._cfg = config_module.update(self.config_path, retention_enabled=enabled)

    def run_now(self) -> None:
        self.trigger.set()


class SourceControl:
    """Lets the tray change which folder clips are taken from.

    Repointing at runtime has to preserve the startup invariant: reconcile the
    new folder to completion BEFORE arming the watcher on it. Reversed, a clip
    recorded during the scan is missed by both.
    """

    def __init__(self, app: "Application") -> None:
        self._app = app

    @property
    def clips_root(self) -> Path:
        return self._app.cfg.clips_root

    def exists(self) -> bool:
        try:
            return self.clips_root.is_dir()
        except OSError:
            return False

    def clip_count(self) -> int:
        try:
            return sum(1 for _ in self.clips_root.rglob("*.mp4"))
        except OSError:
            return 0

    def set_clips_root(self, new_root: Path) -> bool:
        return self._app.change_clips_root(Path(new_root))


class Application:
    def __init__(self, cfg: config_module.Config, allow_interactive_auth: bool = True) -> None:
        self.cfg = cfg
        self.allow_interactive_auth = allow_interactive_auth
        self.stop = threading.Event()
        self.db = Db(cfg.db_path)
        self.client = DriveClient(cfg.client_secret_path, cfg.token_path)
        self.watcher: Watcher | None = None
        self.tray: Tray | None = None
        self.log_path: Path | None = None
        self._threads: list[threading.Thread] = []
        self._workers: list[UploadWorker] = []
        self.retention_control = RetentionControl(self.db, cfg.app_dir / "config.toml", cfg)
        self.source_control = SourceControl(self)
        self._source_lock = threading.Lock()
        self._quota_cache: dict | None = None
        self._quota_checked_at = 0.0

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
        self._spawn("retention", self._retention_loop)
        if self.cfg.retention_enabled:
            log.info(
                "retention on: keeping the newest %d clips, nothing younger than %.0fh",
                self.cfg.retention_keep_newest, self.cfg.retention_min_age_hours,
            )
        else:
            log.info("retention off - change it from the tray menu at any time")

    def change_clips_root(self, new_root: Path) -> bool:
        """Point the watcher and the scan at a different folder. Returns True if it changed."""
        from climp.db import normalise

        if not new_root.is_dir():
            log.warning("not a folder, ignoring: %s", new_root)
            return False

        with self._source_lock:
            if normalise(new_root) == normalise(self.cfg.clips_root):
                return False

            old_root = self.cfg.clips_root
            config_module.update(self.cfg.app_dir / "config.toml", clips_root=new_root)
            self.cfg = replace(self.cfg, clips_root=new_root)

            # Stop watching the old folder first, so events from it cannot
            # arrive while the new one is being scanned.
            if self.watcher is not None:
                self.watcher.stop()
                self.watcher = None

            # Same ordering rule as startup: scan to completion, then arm.
            reconcile(self.db, new_root, backfill_since=self.cfg.backfill_since)

            self.watcher = Watcher(new_root, on_candidate=self.db.insert_candidate)
            self.watcher.start()

        log.info("clips folder changed: %s -> %s", old_root, new_root)
        return True

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
        # Only the folder id is persisted. Everything else is read back from
        # disk by update(), so a CLI override cannot leak into the config.
        config_module.update(self.cfg.app_dir / "config.toml", drive_folder_id=self.folder_id)
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

    def quota(self) -> dict | None:
        """Cached, because the tray asks every few seconds and this is an API call."""
        now = time.monotonic()
        if self._quota_cache is None or (now - self._quota_checked_at) > QUOTA_REFRESH_SECONDS:
            try:
                self._quota_cache = self.client.storage_quota()
            except Exception:
                log.debug("quota read failed", exc_info=True)
            self._quota_checked_at = now
        return self._quota_cache

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
            should_defer=activity.is_busy if self.cfg.defer_while_gaming else None,
        )
        self._workers.append(worker)
        try:
            worker.run()
        except Exception:
            log.exception("upload worker died")
        finally:
            self.db.close()

    def _sweep_loop(self) -> None:
        """Roadblock 7. A slept machine wakes to dead sockets, not errors.

        Sleep is detected rather than waited out. time.monotonic() is frozen
        while Windows is suspended but time.time() is not, so a gap between the
        two means the machine was asleep. That turns a ten-minute staleness
        timeout into an immediate requeue on wake, without subscribing to power
        broadcasts or owning a message window.
        """
        interval = 60.0
        while not self.stop.is_set():
            before_mono, before_wall = time.monotonic(), time.time()
            self.stop.wait(interval)
            if self.stop.is_set():
                break

            slept = (time.time() - before_wall) - (time.monotonic() - before_mono)
            try:
                if slept > SLEEP_DETECT_SECONDS:
                    log.info("system resumed after about %.0fs suspended; requeueing uploads", slept)
                    self.db.recover_stranded_uploads()
                else:
                    self.db.recover_stranded_uploads(stale_after_seconds=self.cfg.stale_upload_seconds)
            except Exception:
                log.exception("stale sweep failed")
        self.db.close()

    def _retention_loop(self) -> None:
        """Roadblock 2. Always running; it asks the config whether to act.

        Re-reading each cycle is what lets the tray turn retention on or change
        the keep count without a restart. Reading a small TOML every thirty
        seconds costs nothing next to what it controls.
        """
        control = self.retention_control
        last_run = time.monotonic()

        while not self.stop.is_set():
            asked = control.trigger.wait(timeout=30)
            if self.stop.is_set():
                break
            control.trigger.clear()

            cfg = control.reload()
            due = (time.monotonic() - last_run) >= cfg.retention_interval_hours * 3600
            if not (asked or due):
                continue
            last_run = time.monotonic()

            if not cfg.retention_enabled:
                if asked:
                    log.info("retention is off; nothing to do")
                continue

            try:
                retention.sweep(
                    self.db,
                    self.client,
                    keep_newest=cfg.retention_keep_newest,
                    min_age_hours=cfg.retention_min_age_hours,
                )
            except Exception:
                log.exception("retention sweep failed")

        self.db.close()

    def _auth_needed(self, detail: str) -> None:
        log.error("SIGN IN AGAIN: %s", detail)
        if self.tray is not None:
            self.tray.auth_needed(detail)

    # --- shutdown ---------------------------------------------------------

    def shutdown(self) -> None:
        log.info("stopping")
        self.stop.set()
        if self.tray is not None:
            try:
                self.tray.icon.stop()
            except Exception:
                log.debug("tray already stopped", exc_info=True)
        if self.watcher:
            self.watcher.stop()
        for t in self._threads:
            t.join(timeout=5)
        self.db.close()

    @property
    def deferring(self) -> bool:
        return any(w.deferring for w in self._workers)

    def status_line(self) -> str:
        c = self.db.counts_by_state()
        if self.deferring:
            return (
                f"PAUSED (fullscreen app)  candidate {c['candidate']}  ready {c['ready']}  "
                f"done {c['done']}  failed {c['failed']}"
            )
        return (
            f"candidate {c['candidate']}  ready {c['ready']}  uploading {c['uploading']}  "
            f"done {c['done']}  failed {c['failed']}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="climp - upload game clips to Drive automatically.")
    ap.add_argument("--clips-root", type=Path, help="Override the configured clips folder.")
    ap.add_argument("--seconds", type=float, help="Run for N seconds then stop. Omit for Ctrl-C.")
    ap.add_argument("--no-tray", action="store_true", help="Run headless with a console status line.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cfg = config_module.load()
    if args.clips_root:
        cfg = config_module.Config(**{**cfg.__dict__, "clips_root": args.clips_root})

    log_path = logging_setup.init(cfg.app_dir, verbose=args.verbose, console=True)
    log.info("log file: %s", log_path)

    app = Application(cfg)
    app.log_path = log_path
    signal.signal(signal.SIGINT, lambda *_: app.stop.set())

    app.start()
    logging_setup.start_rss_logger(app.stop)

    if args.no_tray or args.seconds is not None:
        _run_headless(app, args.seconds)
        return

    app.tray = Tray(
        app.db,
        app.stop,
        log_path=log_path,
        drive_folder_id=app.folder_id,
        quota_provider=app.quota,
        deferring_provider=lambda: app.deferring,
        retention=app.retention_control,
        source=app.source_control,
    )
    log.info("running in the tray. Use its Quit item to stop.")
    try:
        app.tray.run()  # blocks on the main thread
    finally:
        app.shutdown()
        log.info("final state: %s", app.status_line())


def _run_headless(app: "Application", seconds: float | None) -> None:
    log.info("running headless. Ctrl-C to stop.")
    started = time.monotonic()
    last = ""
    try:
        while not app.stop.is_set():
            if seconds is not None and (time.monotonic() - started) >= seconds:
                break
            line = app.status_line()
            if line != last:
                log.info(line)
                last = line
            app.stop.wait(2)
    finally:
        app.shutdown()
        print()
        print("final state:", app.status_line())


if __name__ == "__main__":
    main()
