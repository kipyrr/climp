"""Tray behaviour.

The tray is a view, so the things worth testing are what it says and the one
thing it writes. Rendering is pystray's problem, not ours.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from climp.db import CANDIDATE, DONE, FAILED, READY, UPLOADING, Db
from climp.tray import (
    ANIMATION_PERIOD_SECONDS,
    FRAME_COUNT,
    RED,
    REFRESH_SECONDS,
    Tray,
    _frames,
    _icon_image,
)

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


def test_counts_track_the_queue(db: Db, tray: Tray):
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


# --- the icon -------------------------------------------------------------


def centre(img):
    return img.getpixel((32, 32))


def test_idle_icon_is_a_solid_red_circle(tray: Tray):
    img = _icon_image(1.0)
    assert centre(img)[:3] == RED
    # A corner stays transparent, so it is a circle rather than a square.
    assert img.getpixel((1, 1))[3] == 0


def test_brightness_zero_is_black_not_transparent():
    px = centre(_icon_image(0.0))
    assert px[:3] == (0, 0, 0)
    assert px[3] == 255, "fading to transparent would make the icon vanish, not darken"


def test_the_pulse_runs_red_to_black_to_red():
    frames = _frames()
    px = [centre(f)[0] for f in frames]

    assert px[0] == 255, "starts at full red"
    assert px[len(px) // 2] == 0, "reaches black halfway"
    assert px[-1] < 255 and px[-1] > 200, "on its way back to red as it loops"


def test_the_fade_has_no_sudden_jumps():
    """A visible step would read as a flicker rather than a fade."""
    px = [centre(f)[0] for f in _frames()]
    steps = [abs(px[i] - px[i - 1]) for i in range(len(px))]  # includes the wrap
    assert max(steps) < 40, f"largest single-frame change was {max(steps)} of 255"


def test_the_loop_joins_up_smoothly():
    """The wrap from last frame to first must be no harsher than any other step."""
    px = [centre(f)[0] for f in _frames()]
    wrap = abs(px[0] - px[-1])
    typical = max(abs(px[i] - px[i - 1]) for i in range(1, len(px)))
    assert wrap <= typical, "the loop point would be visible as a jolt"


def test_animation_period_matches_the_existing_refresh_cadence():
    """The only timing the icon previously had; kept so the change is visual only."""
    assert ANIMATION_PERIOD_SECONDS == REFRESH_SECONDS
    assert FRAME_COUNT >= 20, "too few frames to read as a fade"


# --- when it animates -----------------------------------------------------


def test_idle_does_not_animate(db: Db, tray: Tray):
    tray.refresh()
    assert tray._animating is False


def test_work_in_the_queue_animates(db: Db, tray: Tray):
    db.insert_candidate(CLIP)
    tray.refresh()
    assert tray._animating is True


def test_animation_stops_when_the_work_finishes(db: Db, tray: Tray):
    db.insert_candidate(CLIP)
    tray.refresh()
    assert tray._animating is True

    clip_id = db.by_state(CANDIDATE)[0].id
    db.mark_ready(clip_id)
    db.claim_ready()
    db.mark_done(clip_id, "fileid", "link")
    tray.refresh()

    assert tray._animating is False
    assert centre(tray.current_image)[:3] == RED, "must settle on solid red, not mid-fade"


def test_a_finished_queue_with_failures_does_not_animate(db: Db, tray: Tray):
    """Failed is terminal -- nothing is running, so nothing should pulse."""
    db.insert_candidate(CLIP)
    clip_id = db.by_state(CANDIDATE)[0].id
    db.mark_ready(clip_id)
    db.claim_ready()
    db.mark_failed(clip_id, "nope")
    tray.refresh()

    assert tray._animating is False


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
    assert "Not signed in" in tray._summary()


def test_not_signed_in_takes_priority_over_the_queue(db: Db, tray: Tray):
    """Clips piling up with no explanation looks exactly like it working."""
    db.insert_candidate(CLIP)
    tray.refresh()
    assert "in progress" in tray._summary()

    tray.auth_needed("token expired")
    assert "Not signed in" in tray._summary(), "the real problem must not be buried"


class FakeAuth:
    def __init__(self, problem=None, succeeds=True):
        self.auth_problem = problem
        self.succeeds = succeeds
        self.attempts = 0

    def sign_in(self):
        self.attempts += 1
        if self.succeeds:
            self.auth_problem = None
        return self.succeeds


def test_a_signin_problem_from_the_app_reaches_the_menu(db: Db):
    auth = FakeAuth(problem="The Google client secret is missing from C:/x/client_secret.json")
    t = Tray(db, threading.Event(), auth=auth)
    t.refresh()

    labels = " ".join(i.text for i in t._menu() if getattr(i, "text", None))
    assert "client secret is missing" in labels, "the actual cause, not a generic message"
    assert "Sign in to Google" in labels, "and a way to act on it"


def test_no_signin_item_when_there_is_nothing_wrong(db: Db):
    t = Tray(db, threading.Event(), auth=FakeAuth(problem=None))
    t.refresh()
    labels = " ".join(i.text for i in t._menu() if getattr(i, "text", None))
    assert "Sign in to Google" not in labels


def test_signing_in_from_the_menu_clears_the_problem(db: Db):
    auth = FakeAuth(problem="token expired", succeeds=True)
    t = Tray(db, threading.Event(), auth=auth)
    t.auth_needed("token expired")

    t._sign_in_blocking()

    assert auth.attempts == 1
    assert t._auth_problem() is None


def test_a_failed_signin_leaves_the_problem_visible(db: Db):
    auth = FakeAuth(problem="token expired", succeeds=False)
    t = Tray(db, threading.Event(), auth=auth)

    t._sign_in_blocking()  # must not raise

    assert t._auth_problem() == "token expired"


# --- quit -----------------------------------------------------------------


def test_quit_sets_the_stop_event(db: Db):
    stop = threading.Event()
    t = Tray(db, stop)
    t._quit()
    assert stop.is_set(), "quit must stop every other loop, not just the icon"
