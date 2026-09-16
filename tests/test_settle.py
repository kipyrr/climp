"""The test the blueprint singles out: settle logic, before anything depends on it.

"Feed it a fake file that grows, then stops growing, then gets released, and
assert it only reports ready at the last moment. That logic misbehaves
mid-game, where you cannot debug it."

Everything here drives `evaluate` directly, so there is no clock, no disk and
no waiting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clipsync.settle import (
    Candidate,
    Outcome,
    Probe,
    SettleLoop,
    evaluate,
    needs_exclusive_check,
)

CLIP = Path(r"C:\clips\Game\clip.mp4")


def fresh(first_seen_at: float = 0.0) -> Candidate:
    return Candidate(path=CLIP, first_seen_at=first_seen_at)


def step(candidate: Candidate, size: int, now: float, *, locked: bool = True, mtime: float = 1000.0, **kw) -> tuple[Candidate, Outcome]:
    """One poll. `locked` means another process still holds the file."""
    probe = Probe(size_bytes=size, mtime=mtime)
    exclusive = False
    if needs_exclusive_check(candidate, probe):
        exclusive = not locked
    d = evaluate(candidate, probe, now, exclusive_open=exclusive, **kw)
    return d.candidate, d.outcome


# --- the core sequence ----------------------------------------------------


def test_growing_file_never_promotes():
    c = fresh()
    for i, size in enumerate([10, 20, 30, 40, 50], start=1):
        c, outcome = step(c, size, now=i)
        assert outcome is Outcome.WAITING
        assert c.stable_count == 0, "a size change must reset the consecutive count"


def test_growth_pause_then_resume_resets_the_counter():
    """Two stable polls then growth resumes. The counter must not carry over."""
    c = fresh()
    c, _ = step(c, 10, now=1)
    c, _ = step(c, 10, now=2)
    assert c.stable_count == 1
    c, outcome = step(c, 99, now=3)
    assert outcome is Outcome.WAITING
    assert c.stable_count == 0


def test_stable_but_locked_never_promotes_however_long_it_waits():
    """The case a size-only check gets wrong. This is the important one."""
    c = fresh()
    for now in range(1, 20):
        c, outcome = step(c, 500, now=now, locked=True)
        assert outcome is Outcome.WAITING, f"promoted a locked file at poll {now}"
    assert c.stable_count >= 3, "size test passed, so only the lock is holding it back"


def test_ready_only_after_release():
    c = fresh()
    c, o1 = step(c, 500, now=1, locked=True)   # first sighting, count 0
    c, o2 = step(c, 500, now=2, locked=True)   # count 1
    c, o3 = step(c, 500, now=3, locked=True)   # count 2
    c, o4 = step(c, 500, now=4, locked=True)   # count 3, but still locked
    assert [o1, o2, o3, o4] == [Outcome.WAITING] * 4

    c, o5 = step(c, 500, now=5, locked=False)  # released
    assert o5 is Outcome.READY


def test_full_recording_lifecycle():
    """Grow, pause while locked, then release -- ready exactly once, at the end."""
    c = fresh()
    outcomes = []
    for now, size in enumerate([10, 20, 30, 40, 40, 40, 40], start=1):
        c, o = step(c, size, now=now, locked=True)
        outcomes.append(o)
    assert all(o is Outcome.WAITING for o in outcomes)

    c, final = step(c, 40, now=99, locked=False)
    assert final is Outcome.READY
    assert outcomes.count(Outcome.READY) == 0


# --- timeout, D2 and D6 ---------------------------------------------------


def test_timeout_sends_the_row_to_failed():
    c = fresh(first_seen_at=0.0)
    _, outcome = step(c, 500, now=30 * 60 + 1, locked=True)
    assert outcome is Outcome.FAILED_TIMEOUT


def test_timeout_does_not_fire_early():
    c = fresh(first_seen_at=0.0)
    _, outcome = step(c, 500, now=30 * 60 - 1, locked=True)
    assert outcome is Outcome.WAITING


def test_a_file_that_settles_just_before_the_timeout_still_promotes():
    c = fresh(first_seen_at=0.0)
    c, _ = step(c, 500, now=1, locked=True)
    c, _ = step(c, 500, now=2, locked=True)
    c, _ = step(c, 500, now=3, locked=True)
    c, outcome = step(c, 500, now=30 * 60 - 1, locked=False)
    assert outcome is Outcome.READY, "release must win over an imminent timeout"


def test_exactly_how_many_polls_promotion_takes():
    """Pins the off-by-one the blueprint leaves ambiguous.

    "Size unchanged for 3 consecutive polls" could mean 3 observations of the
    same size (2 comparisons) or 3 comparisons that found no change (4
    observations). This implementation takes the second, stricter reading: the
    first poll only establishes a baseline, because there is nothing yet to
    compare it against.

    At a 2s interval that is roughly 8 seconds from first sighting to ready,
    not 6. Recorded here so a future change to POLL_SECONDS or
    STABLE_POLLS_REQUIRED cannot silently shift it.
    """
    c = fresh()
    outcomes = []
    for now in range(1, 5):
        c, o = step(c, 500, now=now, locked=False)
        outcomes.append(o)

    assert outcomes[:3] == [Outcome.WAITING] * 3, "first poll is a baseline, next two are comparisons"
    assert outcomes[3] is Outcome.READY, "third successful comparison promotes"


# --- missing files, D3 ----------------------------------------------------


def test_file_that_vanishes_after_being_seen_fails():
    c = fresh()
    c, _ = step(c, 500, now=1)
    d = evaluate(c, None, now=2)
    assert d.outcome is Outcome.FAILED_VANISHED


def test_event_for_a_file_that_never_existed_is_dropped_silently():
    """Must not become `failed` -- it would be tray noise for a non-event."""
    d = evaluate(fresh(), None, now=1)
    assert d.outcome is Outcome.DROPPED_PHANTOM


# --- the backfill gate, D15 ----------------------------------------------


def test_old_file_is_dropped_before_the_size_test_can_promote_it():
    """The 2026-09-16 regression.

    Explorer browsing the clips folder fired `modified` events for 17 clips
    from July and August. An untouched file passes the size test on the very
    next poll, so without this gate a folder browse queues the whole back
    catalogue. 10.75 GB against 3.7 GB of free Drive.
    """
    old = Probe(size_bytes=236_000_000, mtime=1_750_000_000.0)  # July
    backfill_since = 1_758_000_000.0                             # first run, September

    c = fresh()
    for now in range(1, 6):
        d = evaluate(c, old, now=now, exclusive_open=True, backfill_since=backfill_since)
        assert d.outcome is Outcome.DROPPED_TOO_OLD
        c = d.candidate


def test_new_file_passes_the_backfill_gate():
    backfill_since = 1_758_000_000.0
    new = Probe(size_bytes=181_000_000, mtime=backfill_since + 60)

    c = fresh()
    d = evaluate(c, new, now=1, backfill_since=backfill_since)
    assert d.outcome is Outcome.WAITING
    d = evaluate(d.candidate, new, now=2, backfill_since=backfill_since)
    d = evaluate(d.candidate, new, now=3, backfill_since=backfill_since)
    d = evaluate(d.candidate, new, now=4, exclusive_open=True, backfill_since=backfill_since)
    assert d.outcome is Outcome.READY


def test_gate_is_inert_when_unset():
    old = Probe(size_bytes=1, mtime=0.0)
    d = evaluate(fresh(), old, now=1, backfill_since=None)
    assert d.outcome is Outcome.WAITING


# --- odd files ------------------------------------------------------------


def test_zero_byte_file_needs_the_lock_test_like_any_other():
    c = fresh()
    c, o1 = step(c, 0, now=1, locked=True)
    c, o2 = step(c, 0, now=2, locked=True)
    c, o3 = step(c, 0, now=3, locked=True)
    assert [o1, o2, o3] == [Outcome.WAITING] * 3
    c, o4 = step(c, 0, now=4, locked=False)
    assert o4 is Outcome.READY, "a 0-byte file is still a settled file; the uploader decides what to do with it"


def test_exclusive_check_is_not_attempted_while_the_file_is_growing():
    """Paying for a file handle on a growing file is wasted work."""
    c = fresh()
    assert not needs_exclusive_check(c, Probe(size_bytes=10, mtime=1.0))
    c, _ = step(c, 10, now=1)
    assert not needs_exclusive_check(c, Probe(size_bytes=20, mtime=1.0)), "size changed"


# --- the loop wrapper -----------------------------------------------------


def test_loop_add_is_idempotent():
    loop = SettleLoop(on_ready=lambda c: None, stat_fn=lambda p: None)
    for _ in range(5):
        loop.add(CLIP)
    assert loop.pending == 1


def test_loop_add_is_case_insensitive_on_path():
    """Windows path spellings must not create two entries. Mirrors the unique index."""
    loop = SettleLoop(on_ready=lambda c: None, stat_fn=lambda p: None)
    loop.add(Path(r"C:\clips\Game\clip.mp4"))
    loop.add(Path(r"c:\CLIPS\game\CLIP.MP4"))
    assert loop.pending == 1


def test_loop_promotes_once_and_forgets():
    promoted: list[Candidate] = []
    loop = SettleLoop(
        on_ready=promoted.append,
        stat_fn=lambda p: Probe(size_bytes=100, mtime=1.0),
        exclusive_fn=lambda p: True,
        clock=lambda: 0.0,
    )
    loop.add(CLIP)
    for _ in range(6):
        loop.poll_once()
        if promoted:
            break
    assert len(promoted) == 1
    assert loop.pending == 0, "a promoted candidate must leave the queue"


def test_loop_reports_drops_separately_from_promotions():
    dropped: list[tuple] = []
    loop = SettleLoop(
        on_ready=lambda c: pytest.fail("must not promote a missing file"),
        on_dropped=lambda c, o, r: dropped.append((o, r)),
        stat_fn=lambda p: None,
        clock=lambda: 0.0,
    )
    loop.add(CLIP)
    loop.poll_once()
    assert len(dropped) == 1
    assert dropped[0][0] is Outcome.DROPPED_PHANTOM
    assert loop.pending == 0
