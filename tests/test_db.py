"""The state table's guarantees, especially the ones two components rely on.

The claim race is the one that cannot be reasoned about from reading the code,
so it is tested with real threads against a real file-backed database.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from climp.db import CANDIDATE, DONE, FAILED, READY, UPLOADING, Db, normalise

CLIP = r"C:\clips\Marvel Rivals\clip.mp4"


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db(tmp_path: Path, clock: FakeClock) -> Db:
    d = Db(tmp_path / "clips.db", clock=clock)
    d.init_schema()
    return d


# --- duplicate guard ------------------------------------------------------


def test_insert_is_idempotent(db: Db):
    assert db.insert_candidate(CLIP) is True
    for _ in range(5):
        assert db.insert_candidate(CLIP) is False
    assert len(db.by_state(CANDIDATE)) == 1


def test_different_spellings_of_one_path_are_one_row(db: Db):
    """Guards section 2.3.3. Without this the unique index silently fails."""
    db.insert_candidate(r"C:\clips\Marvel Rivals\clip.mp4")
    db.insert_candidate(r"c:\CLIPS\marvel rivals\CLIP.MP4")
    db.insert_candidate(r"C:\clips\Marvel Rivals\..\Marvel Rivals\clip.mp4")
    assert len(db.by_state(CANDIDATE)) == 1


def test_original_spelling_is_preserved_for_display(db: Db):
    """path_key is normalised; path is not, so the tray shows a readable name."""
    db.insert_candidate(CLIP)
    clip = db.by_state(CANDIDATE)[0]
    assert clip.path.name == "clip.mp4"
    assert normalise(CLIP) != CLIP, "sanity: the two really do differ"


def test_a_done_row_blocks_reinsertion(db: Db):
    """What stops a restart re-uploading everything."""
    db.insert_candidate(CLIP)
    clip = db.by_state(CANDIDATE)[0]
    db.mark_ready(clip.id)
    db.claim_ready()
    db.mark_done(clip.id, "fileid", "https://link")

    assert db.insert_candidate(CLIP) is False
    assert len(db.by_state(DONE)) == 1
    assert db.by_state(CANDIDATE) == []


# --- the claim race -------------------------------------------------------


def _ready_clip(db: Db, path: str) -> int:
    db.insert_candidate(path)
    clip = [c for c in db.by_state(CANDIDATE) if str(c.path) == path][0]
    db.mark_ready(clip.id)
    return clip.id


def test_two_workers_cannot_claim_the_same_row(tmp_path: Path):
    """Real threads, real file, one row. Exactly one worker may win."""
    db = Db(tmp_path / "clips.db")
    db.init_schema()
    _ready_clip(db, CLIP)

    results: list[list] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def worker():
        # Each thread gets its own connection via threading.local inside Db.
        barrier.wait()
        claimed = db.claim_ready(limit=1)
        with lock:
            results.append(claimed)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r]
    assert len(winners) == 1, f"{len(winners)} workers claimed the same row"
    assert len(db.by_state(UPLOADING)) == 1


def test_claim_respects_the_limit(db: Db):
    for i in range(5):
        _ready_clip(db, rf"C:\clips\G\clip{i}.mp4")
    assert len(db.claim_ready(limit=2)) == 2
    assert len(db.by_state(READY)) == 3


def test_claim_returns_nothing_on_an_empty_queue(db: Db):
    assert db.claim_ready() == []


def test_claim_is_oldest_first(db: Db, clock: FakeClock):
    ids = []
    for i in range(3):
        clock.advance(10)
        ids.append(_ready_clip(db, rf"C:\clips\G\clip{i}.mp4"))
    assert db.claim_ready(limit=1)[0].id == ids[0]


# --- backoff, D1 ----------------------------------------------------------


def test_retry_returns_the_row_to_ready_but_hidden_until_the_delay_elapses(db: Db, clock: FakeClock):
    clip_id = _ready_clip(db, CLIP)
    db.claim_ready()
    db.mark_retry(clip_id, "connection reset", next_attempt_at=clock.t + 60)

    assert db.get(clip_id).state == READY
    assert db.claim_ready() == [], "claimed a row whose backoff has not elapsed"

    clock.advance(61)
    assert len(db.claim_ready()) == 1


def test_retry_increments_attempts(db: Db, clock: FakeClock):
    clip_id = _ready_clip(db, CLIP)
    for i in range(3):
        db.claim_ready()
        db.mark_retry(clip_id, "boom", next_attempt_at=clock.t)
    assert db.get(clip_id).attempts == 3


def test_auth_block_requeues_without_burning_an_attempt(db: Db):
    """D13. A weekly token expiry must not push a queue of good clips to failed."""
    ids = [_ready_clip(db, rf"C:\clips\G\clip{i}.mp4") for i in range(10)]

    claimed = db.claim_ready(limit=10)
    assert len(claimed) == 10
    for clip in claimed:
        db.mark_auth_blocked(clip.id, "AuthExpired: sign in again")

    assert all(db.get(i).attempts == 0 for i in ids)
    assert len(db.by_state(READY)) == 10
    assert db.by_state(FAILED) == []


# --- done is atomic -------------------------------------------------------


def test_done_always_carries_a_file_id(db: Db):
    clip_id = _ready_clip(db, CLIP)
    db.claim_ready()
    db.mark_done(clip_id, "abc123", "https://drive/abc123")

    clip = db.get(clip_id)
    assert clip.state == DONE
    assert clip.drive_file_id == "abc123"
    assert clip.drive_link == "https://drive/abc123"
    assert clip.last_error is None, "a successful upload must clear the previous error"


def test_done_clears_a_stale_error_from_an_earlier_attempt(db: Db, clock: FakeClock):
    clip_id = _ready_clip(db, CLIP)
    db.claim_ready()
    db.mark_retry(clip_id, "network died", next_attempt_at=clock.t)
    db.claim_ready()
    db.mark_done(clip_id, "abc", "link")
    assert db.get(clip_id).last_error is None


# --- crash recovery -------------------------------------------------------


def test_stranded_uploads_come_back_with_their_session_uri(db: Db):
    clip_id = _ready_clip(db, CLIP)
    db.claim_ready()
    db.save_session_uri(clip_id, "https://upload/session/xyz")

    assert db.recover_stranded_uploads() == 1
    clip = db.get(clip_id)
    assert clip.state == READY
    assert clip.session_uri == "https://upload/session/xyz", "losing this means restarting a 225 MB upload"


def test_recovery_leaves_finished_and_waiting_rows_alone(db: Db):
    done_id = _ready_clip(db, r"C:\clips\G\a.mp4")
    db.claim_ready()
    db.mark_done(done_id, "x", "y")
    _ready_clip(db, r"C:\clips\G\b.mp4")
    db.insert_candidate(r"C:\clips\G\c.mp4")

    assert db.recover_stranded_uploads() == 0
    assert len(db.by_state(DONE)) == 1
    assert len(db.by_state(READY)) == 1
    assert len(db.by_state(CANDIDATE)) == 1


def test_stale_sweep_only_touches_old_rows(db: Db, clock: FakeClock):
    """Roadblock 7: a sleeping PC wakes to dead sockets, but a live upload must not be stolen."""
    old_id = _ready_clip(db, r"C:\clips\G\old.mp4")
    db.claim_ready()
    clock.advance(1200)
    new_id = _ready_clip(db, r"C:\clips\G\new.mp4")
    db.claim_ready()

    assert db.recover_stranded_uploads(stale_after_seconds=600) == 1
    assert db.get(old_id).state == READY
    assert db.get(new_id).state == UPLOADING


# --- transitions reject the wrong source state ---------------------------


def test_settle_timeout_only_applies_to_a_candidate(db: Db):
    clip_id = _ready_clip(db, CLIP)
    db.mark_settle_timeout(clip_id, "should not apply")
    assert db.get(clip_id).state == READY


def test_retry_button_only_applies_to_a_failed_row(db: Db):
    clip_id = _ready_clip(db, CLIP)
    db.request_retry(clip_id)
    assert db.get(clip_id).state == READY


def test_retry_button_resets_to_candidate_and_keeps_the_session_uri(db: Db, clock: FakeClock):
    """D4."""
    clip_id = _ready_clip(db, CLIP)
    db.claim_ready()
    db.save_session_uri(clip_id, "https://upload/session/abc")
    db.mark_failed(clip_id, "attempts exhausted")

    db.request_retry(clip_id)
    clip = db.get(clip_id)
    assert clip.state == CANDIDATE, "a settle-timeout row must re-verify before uploading"
    assert clip.attempts == 0
    assert clip.last_error is None
    assert clip.session_uri == "https://upload/session/abc", "a retried upload should resume, not restart"


def test_settle_timeout_row_can_be_retried_back_into_the_queue(db: Db):
    db.insert_candidate(CLIP)
    clip_id = db.by_state(CANDIDATE)[0].id
    db.mark_settle_timeout(clip_id, "settle timeout after 1800s")
    assert db.get(clip_id).state == FAILED

    db.request_retry(clip_id)
    assert db.get(clip_id).state == CANDIDATE


# --- drops, D15 -----------------------------------------------------------


def test_drop_removes_the_row_entirely(db: Db):
    db.insert_candidate(CLIP)
    clip_id = db.by_state(CANDIDATE)[0].id
    db.drop(clip_id)
    assert db.get(clip_id) is None
    assert db.insert_candidate(CLIP) is True, "a dropped path must be insertable again"


# --- tray -----------------------------------------------------------------


def test_counts_cover_every_state_even_when_empty(db: Db):
    counts = db.counts_by_state()
    assert set(counts) == {CANDIDATE, READY, UPLOADING, DONE, FAILED}
    assert all(v == 0 for v in counts.values())


def test_counts_track_reality(db: Db):
    db.insert_candidate(r"C:\clips\G\a.mp4")
    b = _ready_clip(db, r"C:\clips\G\b.mp4")
    c = _ready_clip(db, r"C:\clips\G\c.mp4")
    db.claim_ready()
    db.mark_done(b, "x", "y")
    db.mark_failed(c, "nope")

    counts = db.counts_by_state()
    assert counts[CANDIDATE] == 1
    assert counts[DONE] == 1
    assert counts[FAILED] == 1
