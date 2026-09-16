"""Reconciler — the single pass that makes closing the app safe.

Runs once at startup, to completion, BEFORE the watcher is armed. Walks the
clips folder and inserts a candidate row for every .mp4 it does not already
know about. Rows at done are left alone, which is what stops a restart
re-uploading everything.

It hands nothing to anyone. It finishes, and then main.py starts the loops.

See IMPLEMENTATION-PLAN.md section 2.6. Decisions D14, D15.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from clipsync.db import Db

log = logging.getLogger(__name__)

CLIP_SUFFIX = ".mp4"


@dataclass(frozen=True)
class ReconcileResult:
    scanned: int
    inserted: int
    already_known: int
    skipped_old: int
    duration_seconds: float

    def __str__(self) -> str:
        return (
            f"scanned {self.scanned}, enqueued {self.inserted}, "
            f"already known {self.already_known}, too old {self.skipped_old} "
            f"in {self.duration_seconds:.2f}s"
        )


def reconcile(
    db: Db,
    clips_root: Path,
    backfill_since: float | None = None,
    clock=time.monotonic,
) -> ReconcileResult:
    """Walk the tree once and enqueue what is missing.

    `backfill_since` is a pre-filter only. The settle checker holds the
    authoritative gate (D15), because it is the component that stats files on
    every pass anyway. Filtering here simply avoids creating rows that would be
    dropped moments later -- which matters on a first run against a folder
    holding 10.75 GB of back catalogue.
    """
    started = clock()
    if not clips_root.is_dir():
        raise FileNotFoundError(f"Clips folder does not exist: {clips_root}")

    known = db.known_path_keys()
    scanned = inserted = already_known = skipped_old = 0

    for path in clips_root.rglob(f"*{CLIP_SUFFIX}"):
        if not path.is_file():
            continue
        scanned += 1

        from clipsync.db import normalise  # local: keeps normalisation owned by db.py

        if normalise(path) in known:
            already_known += 1
            continue

        if backfill_since is not None:
            try:
                if path.stat().st_mtime < backfill_since:
                    skipped_old += 1
                    continue
            except OSError:
                # Vanished between rglob and stat. Nothing to enqueue.
                continue

        if db.insert_candidate(path):
            inserted += 1

    result = ReconcileResult(
        scanned=scanned,
        inserted=inserted,
        already_known=already_known,
        skipped_old=skipped_old,
        duration_seconds=clock() - started,
    )
    log.info("reconcile: %s", result)
    return result
