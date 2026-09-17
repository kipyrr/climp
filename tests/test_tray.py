"""Tray behaviour.

The tray is a view, so the things worth testing are what it says and the one
thing it writes. Rendering is pystray's problem, not ours.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from clipsync.db import CANDIDATE, DONE, FAILED, READY, UPLOADING, Db
from clipsync.tray import ATTENTION, BUSY, IDLE, Tray

CLIP = r"C:\clips\Marvel Rivals\clip.mp4"


@pytest.fixture
def db(tmp_path: Path) -> Db:
    d = Db(tmp_path / "clips.db")
    d.init_schema()
    return d


@pytest.fixture
def tray(db: Db) -> Tray:
    return Tray(db, threading.Event(), quota_provider=lambda: {"free": 13 * 1024**3, "limit": 15 * 1024**3})


def ready_clip(db: Db, path: str = CLIP) -> int:
    db.insert_candidate(path)
    clip = [c for c in db.by_state(CANDIDATE) if str(c.path) == path][0]
    db.mark_ready(clip.id)
    return clip.id


# --- what it says ---------------------------------------------------------


def test_idle_summary(tray: Tray):
    tray.refresh()
    assert "idle" in tray._summary()


def test_failures_take_priority_in_the_summary(db: Db, tray: Tray):
    """A failure is the only state needing action, so it must not be buried."""
    clip_id = ready_clip(db)
    db.claim_ready()
    db.mark_failed(clip_id, "Google Drive is full")
    ready_clip(db, r"C:\clips\G\other.mp4")
    tray.refresh()

    assert "failed" in tray._summary()


def test_quota_is_expressed_in_clips_not_just_bytes(tray: Tray):
    tray.refresh()
    text = tray._quota_text()
    assert "GB free" in text
    assert "clips" in text, "bytes alone do not answer 'how many more can I record'"


def test_quota_handles_an_account_with_no_stated_limit(db: Db):
    t = Tray(db, threading.Event(), quota_provider=lambda: {"free": None, "limit": None})
    t.refresh()
    assert "no stated limit" in t._quota_text()


def test_a_broken_quota_provider_does_not_break_the_tray(db: Db):
    def boom():
        raise RuntimeError("network down")

    t = Tray(db, threading.Event(), quota_provider=boom)
    t.refresh()  # must not raise
    assert "checking" in t._quota_text()


# --- the icon colour ------------------------------------------------------


def test_icon_colours_track_the_queue(db: Db, tray: Tray):
    tray.refresh()
    assert tray._counts[DONE] == 0

    db.insert_candidate(CLIP)
    tray.refresh()
    assert tray._counts[CANDIDATE] == 1

    clip_id = db.by_state(CANDIDATE)[0].id
    db.mark_ready(clip_id)
    db.claim_ready()
    db.mark_failed(clip_id, "nope")
    tray.refresh()
    assert tray._counts[FAILED] == 1


def test_colours_are_distinct():
    assert len({IDLE, BUSY, ATTENTION}) == 3


# --- the one thing it writes ---------------------------------------------


def test_retry_resets_a_failed_row_to_candidate(db: Db, tray: Tray):
    """D4. The tray's only write."""
    clip_id = ready_clip(db)
    db.claim_ready()
    db.save_session_uri(clip_id, "https://upload/session/abc")
    db.mark_failed(clip_id, "connection died")

    tray._make_retry(clip_id)()

    clip = db.get(clip_id)
    assert clip.state == CANDIDATE
    assert clip.attempts == 0
    assert clip.session_uri == "https://upload/session/abc", "a retry should resume, not restart"


def test_retry_menu_lists_failures_with_their_error(db: Db, tray: Tray):
    clip_id = ready_clip(db)
    db.claim_ready()
    db.mark_failed(clip_id, "Google Drive is full - free up space, then press Retry")
    tray.refresh()

    labels = [item.text for item in tray._failure_items()]
    assert len(labels) == 1
    assert "full" in labels[0]


def test_tray_never_writes_anything_but_a_retry(db: Db, tray: Tray):
    """Guards the blueprint's rule that the tray stays dumb."""
    db.insert_candidate(CLIP)
    before = db.counts_by_state()
    for _ in range(3):
        tray.refresh()
    assert db.counts_by_state() == before


# --- auth prompt, D13 -----------------------------------------------------


def test_auth_message_surfaces_and_flags_attention(db: Db, tray: Tray):
    tray.auth_needed("refresh failed; sign in required")
    assert tray._auth_message
    assert "Sign in" in "".join(
        item.text for item in tray._menu() if getattr(item, "text", None)
    )


# --- quit -----------------------------------------------------------------


def test_quit_sets_the_stop_event(db: Db):
    stop = threading.Event()
    t = Tray(db, stop)
    t._quit()
    assert stop.is_set(), "quit must stop every other loop, not just the icon"
