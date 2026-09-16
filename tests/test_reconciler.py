"""Reconciler — the pass that makes closing the app safe.

Uses real files in tmp_path, because the thing being tested is a directory
walk and an mtime comparison.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clipsync.db import CANDIDATE, DONE, Db
from clipsync.reconciler import reconcile


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def db(tmp_path: Path) -> Db:
    d = Db(tmp_path / "state" / "clips.db")
    d.db_path = str(tmp_path / "clips.db")
    d.init_schema()
    return d


@pytest.fixture
def clips_root(tmp_path: Path) -> Path:
    root = tmp_path / "NVIDIA"
    root.mkdir()
    return root


def make_clip(root: Path, relative: str, mtime: float | None = None, size: int = 1024) -> Path:
    p = root / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# --- the basic pass -------------------------------------------------------


def test_enqueues_everything_on_a_fresh_database(db, clips_root):
    for i in range(3):
        make_clip(clips_root, f"Marvel Rivals/clip{i}.mp4")

    result = reconcile(db, clips_root)
    assert result.scanned == 3
    assert result.inserted == 3
    assert len(db.by_state(CANDIDATE)) == 3


def test_finds_clips_in_nested_per_game_folders(db, clips_root):
    make_clip(clips_root, "Marvel Rivals/a.mp4")
    make_clip(clips_root, "Palworld/b.mp4")
    make_clip(clips_root, "Overwatch 2/deeper/c.mp4")

    assert reconcile(db, clips_root).inserted == 3


def test_ignores_everything_that_is_not_an_mp4(db, clips_root):
    make_clip(clips_root, "Marvel Rivals/real.mp4")
    make_clip(clips_root, "Marvel Rivals/notes.txt")
    make_clip(clips_root, "Marvel Rivals/edit.mov")
    make_clip(clips_root, "Marvel Rivals/thumb.png")

    result = reconcile(db, clips_root)
    assert result.scanned == 1
    assert result.inserted == 1


def test_running_twice_creates_nothing_new(db, clips_root):
    for i in range(3):
        make_clip(clips_root, f"G/clip{i}.mp4")

    first = reconcile(db, clips_root)
    second = reconcile(db, clips_root)

    assert first.inserted == 3
    assert second.inserted == 0
    assert second.already_known == 3
    assert len(db.by_state(CANDIDATE)) == 3


def test_a_missing_clips_folder_is_an_error_not_a_silent_no_op(db, tmp_path):
    with pytest.raises(FileNotFoundError):
        reconcile(db, tmp_path / "does-not-exist")


# --- the restart guarantee ------------------------------------------------


def test_done_rows_are_never_re_enqueued(db, clips_root):
    """What stops a restart re-uploading the whole library."""
    clip = make_clip(clips_root, "G/uploaded.mp4")
    db.insert_candidate(clip)
    row = db.by_state(CANDIDATE)[0]
    db.mark_ready(row.id)
    db.claim_ready()
    db.mark_done(row.id, "driveid", "link")

    result = reconcile(db, clips_root)

    assert result.inserted == 0
    assert result.already_known == 1
    assert len(db.by_state(DONE)) == 1
    assert db.by_state(CANDIDATE) == []


def test_a_clip_added_while_the_app_was_closed_is_picked_up(db, clips_root):
    make_clip(clips_root, "G/old.mp4")
    reconcile(db, clips_root)

    make_clip(clips_root, "G/recorded-while-closed.mp4")
    result = reconcile(db, clips_root)

    assert result.inserted == 1
    assert len(db.by_state(CANDIDATE)) == 2


def test_case_differences_between_scan_and_watcher_do_not_duplicate(db, clips_root):
    clip = make_clip(clips_root, "G/clip.mp4")
    db.insert_candidate(str(clip).upper())

    result = reconcile(db, clips_root)
    assert result.inserted == 0, "the unique index must survive a spelling difference"
    assert len(db.by_state(CANDIDATE)) == 1


# --- the backfill gate, D14 and D15 --------------------------------------


def test_old_clips_are_skipped_so_the_back_catalogue_stays_local(db, clips_root):
    make_clip(clips_root, "G/july.mp4", mtime=1_750_000_000)
    make_clip(clips_root, "G/august.mp4", mtime=1_755_000_000)
    make_clip(clips_root, "G/today.mp4", mtime=1_758_100_000)

    result = reconcile(db, clips_root, backfill_since=1_758_000_000)

    assert result.scanned == 3
    assert result.skipped_old == 2
    assert result.inserted == 1
    assert db.by_state(CANDIDATE)[0].path.name == "today.mp4"


def test_lowering_the_cutoff_pulls_in_what_was_skipped(db, clips_root):
    """D14 claims the decision is reversible. If this fails, it is not."""
    make_clip(clips_root, "G/july.mp4", mtime=1_750_000_000)
    make_clip(clips_root, "G/today.mp4", mtime=1_758_100_000)

    reconcile(db, clips_root, backfill_since=1_758_000_000)
    assert len(db.by_state(CANDIDATE)) == 1

    result = reconcile(db, clips_root, backfill_since=1_700_000_000)
    assert result.inserted == 1
    assert len(db.by_state(CANDIDATE)) == 2

    paths = sorted(c.path.name for c in db.by_state(CANDIDATE))
    assert paths == ["july.mp4", "today.mp4"]


def test_no_cutoff_means_everything(db, clips_root):
    make_clip(clips_root, "G/ancient.mp4", mtime=1_000_000_000)
    result = reconcile(db, clips_root, backfill_since=None)
    assert result.inserted == 1
