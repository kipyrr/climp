"""Retention -- keep Drive from filling up by removing the oldest uploads.

Roadblock 2. The only component in this app that deletes anything, so the
safeguards matter more than the feature:

  * Off by default. It runs because it was configured to, never by accident.
  * Only rows that are `done`, still carry a Drive file id, and have not
    already been retired.
  * A minimum age floor, independent of the keep count, so a misconfigured
    setting cannot remove something uploaded minutes ago.
  * `drive.file` scope means it is *incapable* of touching anything the app
    did not upload itself, whatever a bug here might ask for.
  * Dry run prints exactly what would go, and removes nothing.

The row stays at `done` after its Drive copy is deleted. That is what stops the
reconciler noticing the clip again and re-uploading it, which would undo the
retention and refill the quota.

See IMPLEMENTATION-PLAN.md roadblock 2, decisions D17 and D18.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from googleapiclient.errors import HttpError

from clipsync.db import Clip, Db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionResult:
    considered: int
    retired: int
    freed_bytes: int
    dry_run: bool
    errors: int = 0

    def __str__(self) -> str:
        verb = "would remove" if self.dry_run else "removed"
        return (
            f"{verb} {self.retired} of {self.considered} eligible clip(s), "
            f"{self.freed_bytes / 1024**3:.2f} GB"
            + (f", {self.errors} error(s)" if self.errors else "")
        )


def sweep(
    db: Db,
    client,
    keep_newest: int,
    min_age_hours: float,
    dry_run: bool = False,
    clock=time.time,
) -> RetentionResult:
    """Remove the oldest uploaded clips from Drive, keeping the newest N."""
    if keep_newest < 1:
        raise ValueError("keep_newest must be at least 1; refusing to empty the folder")

    now = clock()
    candidates = db.retention_candidates(
        keep_newest=keep_newest, min_age_seconds=min_age_hours * 3600, now=now
    )
    if not candidates:
        return RetentionResult(considered=0, retired=0, freed_bytes=0, dry_run=dry_run)

    retired = freed = errors = 0
    for clip in candidates:
        size = clip.size_bytes or 0
        if dry_run:
            log.info("would retire %s (%.0f MB)", clip.path.name, size / 1024**2)
            retired += 1
            freed += size
            continue

        try:
            client.delete_file(clip.drive_file_id)
        except HttpError as e:
            if e.resp.status == 404:
                # Already gone -- deleted by hand in the Drive UI, most likely.
                log.info("%s was already absent from Drive; marking retired", clip.path.name)
            else:
                log.warning("could not retire %s: %s", clip.path.name, e)
                errors += 1
                continue
        except Exception as e:  # noqa: BLE001 - one failure must not stop the sweep
            log.warning("could not retire %s: %s", clip.path.name, e)
            errors += 1
            continue

        db.mark_retired(clip.id)
        retired += 1
        freed += size
        log.info("retired %s from Drive (%.0f MB freed)", clip.path.name, size / 1024**2)

    result = RetentionResult(
        considered=len(candidates), retired=retired, freed_bytes=freed, dry_run=dry_run, errors=errors
    )
    log.info("retention: %s", result)
    return result
