"""Structured logging to a rotating file, plus the hourly RSS line.

Once the app runs from a tray icon there is no console to read, so the log file
is the only way to answer "what did it do last night". It is also the only
place roadblock 12 -- memory creeping over weeks of uptime -- can be observed,
which is why RSS is sampled hourly rather than never.

See IMPLEMENTATION-PLAN.md roadblock 12.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)

LOG_NAME = "climp.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 5
RSS_INTERVAL_SECONDS = 3600


def init(app_dir: Path, verbose: bool = False, console: bool = True) -> Path:
    """Configure the root logger. Returns the log file path."""
    log_dir = app_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_NAME

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-20s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(asctime)s  %(name)-18s %(message)s", datefmt="%H:%M:%S"))
        root.addHandler(stream)

    # The Google client libraries are extremely chatty at DEBUG and would bury
    # everything this app says about its own queue.
    for noisy in ("googleapiclient.discovery", "googleapiclient.discovery_cache", "urllib3", "google_auth_httplib2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return log_path


def start_rss_logger(stop_event: threading.Event, interval: float = RSS_INTERVAL_SECONDS) -> threading.Thread:
    """Sample this process's memory hourly.

    Roadblock 12 is only observable over weeks, so the check has to be running
    long before anyone suspects a leak. One line an hour costs nothing.
    """

    def loop() -> None:
        try:
            import psutil
        except ImportError:
            log.debug("psutil not installed; RSS will not be sampled")
            return
        proc = psutil.Process(os.getpid())
        while not stop_event.is_set():
            try:
                mb = proc.memory_info().rss / (1024 * 1024)
                log.info("rss %.1f MB  threads %d", mb, proc.num_threads())
            except Exception:
                log.debug("RSS sample failed", exc_info=True)
            stop_event.wait(interval)

    t = threading.Thread(target=loop, name="rss", daemon=True)
    t.start()
    return t
