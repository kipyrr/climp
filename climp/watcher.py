"""Watcher — turn filesystem events into candidate clips.

Responsibility: notice that a path deserves attention. Nothing else.
It does not stat the file, does not open it, does not upload, does not block.
The event callback must return in microseconds; anything slower belongs in
another loop.

Phase 1: `on_candidate` prints. Phase 2: it becomes db.insert_candidate.
Nothing else in this module changes when that swap happens.

See IMPLEMENTATION-PLAN.md section 2.4.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import (
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

log = logging.getLogger(__name__)

CLIP_SUFFIX = ".mp4"
DEBOUNCE_SECONDS = 1.0


class ClipEventHandler(FileSystemEventHandler):
    """Collapses watchdog's event storm into one call per path.

    On Windows a single recording produces several create/modify events for the
    same file (roadblock 9). The debounce below is a *performance* measure only
    -- it keeps us from hammering the database with inserts that would be
    rejected anyway. Correctness comes from the unique index on `path` and
    ON CONFLICT DO NOTHING, never from this timer. Do not remove one on the
    grounds that the other exists.
    """

    def __init__(
        self,
        on_candidate: Callable[[Path], None],
        debounce_seconds: float = DEBOUNCE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_candidate = on_candidate
        self._debounce = debounce_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last_seen: dict[str, float] = {}

        # Phase 1 diagnostics: how many raw events does one clip really produce?
        self.raw_event_counts: dict[str, int] = {}

    def on_created(self, event: FileSystemEvent) -> None:
        if isinstance(event, FileCreatedEvent):
            self._consider(event.src_path, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        if isinstance(event, FileModifiedEvent):
            self._consider(event.src_path, "modified")

    def on_moved(self, event: FileSystemEvent) -> None:
        # Some capture tools write to a temp name and rename on completion.
        # ShadowPlay appears not to, but the destination is the only name that
        # matters if it ever does, and handling it costs one branch.
        if isinstance(event, FileMovedEvent):
            self._consider(event.dest_path, "moved")

    def _consider(self, raw_path: str | bytes, kind: str) -> None:
        path = Path(raw_path.decode() if isinstance(raw_path, bytes) else raw_path)
        if path.suffix.lower() != CLIP_SUFFIX:
            return

        key = str(path).lower()
        now = self._clock()

        with self._lock:
            self.raw_event_counts[key] = self.raw_event_counts.get(key, 0) + 1
            last = self._last_seen.get(key)
            if last is not None and (now - last) < self._debounce:
                log.debug("debounced %s event for %s", kind, path.name)
                return
            self._last_seen[key] = now

        log.info("candidate (%s): %s", kind, path.name)
        self._on_candidate(path)


class Watcher:
    """Owns the watchdog observer. Recursive, because clips nest per game."""

    def __init__(self, clips_root: Path, on_candidate: Callable[[Path], None]) -> None:
        self.clips_root = clips_root
        self.handler = ClipEventHandler(on_candidate)
        self._observer = Observer()

    def start(self) -> None:
        if not self.clips_root.is_dir():
            raise FileNotFoundError(f"Clips folder does not exist: {self.clips_root}")
        self._observer.schedule(self.handler, str(self.clips_root), recursive=True)
        self._observer.start()
        log.info("watching %s (recursive)", self.clips_root)

    def stop(self, timeout: float = 5.0) -> None:
        self._observer.stop()
        self._observer.join(timeout=timeout)
        log.info("watcher stopped")
