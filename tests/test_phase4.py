"""Phase 4: deferring while you play, retention, and the schema migration.

Retention is the only thing in this app that deletes anything, so most of
these tests are about what it refuses to do rather than what it does.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from climp import activity
from climp.db import CANDIDATE, DONE, READY, Db
from climp.retention import sweep
from climp.uploader import UploadWorker


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db(tmp_path: Path, clock: FakeClock) -> Db:
    d = Db(tmp_path / "clips.db", clock=clock)
    d.init_schema()
    return d


class FakeDrive:
    def __init__(self, fail_on=None):
        self.deleted: list[str] = []
        self.fail_on = fail_on or set()

    def delete_file(self, file_id):
        if file_id in self.fail_on:
            raise RuntimeError("drive said no")
        self.deleted.append(file_id)


def uploaded(db: Db, clock: FakeClock, name: str, age_hours: float, size_mb: int = 225) -> int:
    """A clip recorded `age_hours` ago and already uploaded."""
    path = "C:\\clips\\G\\" + name + ".mp4"
    db.insert_candidate(path)
    clip = [c for c in db.by_state(CANDIDATE) if str(c.path) == path][0]
    db.record_probe(clip.id, size_mb * 1024 * 1024, clock.t - age_hours * 3600, 3)
    db.mark_ready(clip.id)
    db.claim_ready()
    db.mark_done(clip.id, "drive-" + name, "https://drive/" + name)
    return clip.id


# --- deferring while you play --------------------------------------------


def test_busy_states_cover_fullscreen_games():
    assert activity.QUNS_RUNNING_D3D_FULL_SCREEN in activity.BUSY_STATES
    assert activity.QUNS_BUSY in activity.BUSY_STATES
    assert activity.QUNS_ACCEPTS_NOTIFICATIONS not in activity.BUSY_STATES, (
        "normal desktop use must never pause uploads"
    )


def test_worker_leaves_the_row_alone_while_deferring(db):
    """The whole mechanism: a condition on the claim, not a new component."""
    db.insert_candidate("C:\\clips\\G\\new.mp4")
    clip = db.by_state(CANDIDATE)[0]
    db.mark_ready(clip.id)

    worker = UploadWorker(db, FakeDrive(), "folder", stop_event=threading.Event(), should_defer=lambda: True)

    assert worker._deferred() is True
    assert len(db.by_state(READY)) == 1, "the row must still be waiting, not claimed"


def test_worker_resumes_when_the_game_exits(db):
    busy = {"now": True}
    worker = UploadWorker(db, FakeDrive(), "folder", should_defer=lambda: busy["now"])

    assert worker._deferred() is True
    busy["now"] = False
    assert worker._deferred() is False


def test_a_broken_defer_check_never_stops_uploads_forever(db):
    """Failing closed loses clips silently; failing open costs latency once."""

    def boom():
        raise RuntimeError("win32 unavailable")

    worker = UploadWorker(db, FakeDrive(), "folder", should_defer=boom)
    assert worker._deferred() is False


# --- retention: mostly what it refuses to do -----------------------------


def test_refuses_to_empty_the_folder(db, clock):
    with pytest.raises(ValueError):
        sweep(db, FakeDrive(), keep_newest=0, min_age_hours=0, clock=clock)


def test_keeps_the_newest_n(db, clock):
    for i in range(10):
        uploaded(db, clock, "clip%d" % i, age_hours=100 + i)  # clip0 is newest

    drive = FakeDrive()
    result = sweep(db, drive, keep_newest=3, min_age_hours=1, clock=clock)

    assert result.retired == 7
    assert len(drive.deleted) == 7
    kept = sorted(c.path.name for c in db.by_state(DONE) if c.retired_at is None)
    assert kept == ["clip0.mp4", "clip1.mp4", "clip2.mp4"]


def test_the_age_floor_overrides_a_bad_keep_count(db, clock):
    """A misconfigured keep count must not remove something uploaded minutes ago."""
    for i in range(5):
        uploaded(db, clock, "fresh%d" % i, age_hours=0.5)

    drive = FakeDrive()
    result = sweep(db, drive, keep_newest=1, min_age_hours=24, clock=clock)

    assert result.retired == 0
    assert drive.deleted == []


def test_never_touches_a_clip_that_is_not_uploaded(db, clock):
    db.insert_candidate("C:\\clips\\G\\pending.mp4")
    drive = FakeDrive()
    sweep(db, drive, keep_newest=1, min_age_hours=0, clock=clock)
    assert drive.deleted == []


def test_does_not_retire_the_same_clip_twice(db, clock):
    for i in range(4):
        uploaded(db, clock, "clip%d" % i, age_hours=100 + i)

    drive = FakeDrive()
    sweep(db, drive, keep_newest=1, min_age_hours=1, clock=clock)
    first = len(drive.deleted)

    sweep(db, drive, keep_newest=1, min_age_hours=1, clock=clock)
    assert len(drive.deleted) == first, "a retired clip must not be deleted again"


def test_a_retired_clip_is_never_re_uploaded(db, clock):
    """Otherwise retention would undo itself on the next startup scan."""
    clip_id = uploaded(db, clock, "old", age_hours=500)
    uploaded(db, clock, "recent", age_hours=1)

    sweep(db, FakeDrive(), keep_newest=1, min_age_hours=1, clock=clock)

    clip = db.get(clip_id)
    assert clip.retired_at is not None
    assert clip.state == DONE, "still done, so the reconciler keeps skipping it"
    assert db.insert_candidate(clip.path) is False


def test_dry_run_deletes_nothing(db, clock):
    for i in range(5):
        uploaded(db, clock, "clip%d" % i, age_hours=100 + i)

    drive = FakeDrive()
    result = sweep(db, drive, keep_newest=1, min_age_hours=1, dry_run=True, clock=clock)

    assert result.dry_run
    assert result.retired == 4
    assert drive.deleted == []
    assert db.retired_count() == 0


def test_one_failure_does_not_abort_the_sweep(db, clock):
    for i in range(4):
        uploaded(db, clock, "clip%d" % i, age_hours=100 + i)
    drive = FakeDrive(fail_on={"drive-clip3"})

    result = sweep(db, drive, keep_newest=1, min_age_hours=1, clock=clock)

    assert result.errors == 1
    assert result.retired == 2, "the other eligible clips still went"


def test_reports_the_space_it_freed(db, clock):
    for i in range(3):
        uploaded(db, clock, "clip%d" % i, age_hours=100 + i, size_mb=200)
    result = sweep(db, FakeDrive(), keep_newest=1, min_age_hours=1, clock=clock)
    assert result.freed_bytes == 2 * 200 * 1024 * 1024


# --- the schema migration ------------------------------------------------


def test_retired_at_is_added_to_an_existing_database(tmp_path: Path):
    """Phase 3 databases predate this column and must not need recreating."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE clips (
             id INTEGER PRIMARY KEY, path TEXT NOT NULL, path_key TEXT NOT NULL UNIQUE,
             size_bytes INTEGER, mtime REAL, content_hash TEXT,
             state TEXT NOT NULL CHECK (state IN ('candidate','ready','uploading','done','failed')),
             stable_count INTEGER NOT NULL DEFAULT 0, first_seen_at REAL NOT NULL,
             attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, next_attempt_at REAL,
             session_uri TEXT, drive_file_id TEXT, drive_link TEXT, updated_at REAL NOT NULL);"""
    )
    conn.execute(
        "INSERT INTO clips (path, path_key, state, first_seen_at, updated_at) VALUES (?,?,?,?,?)",
        ("C:\\a.mp4", "c:\\a.mp4", "done", 1.0, 1.0),
    )
    conn.commit()
    conn.close()

    db = Db(path)
    db.init_schema()

    assert db.get(1).retired_at is None
    assert len(db.by_state(DONE)) == 1, "the existing row survived the migration"
