"""Settle checker — decide that a file is finished writing, and promote it.

This is the component the blueprint says is where the real bugs live, so the
decision logic is a pure function: `evaluate(state, probe, now)` takes what was
known, what the disk says, and the time, and returns what to do. It touches no
globals, no clock, no filesystem. Everything that does touch those lives in
`probe_file`, `try_exclusive_open` and `SettleLoop`.

Promotion requires BOTH tests, never either:

  1. Size unchanged for 3 consecutive polls.
  2. An exclusive open succeeds.

NVIDIA can stop writing while still holding the handle, so the size test alone
promotes a file that is not finished. Python's builtin open() shares freely and
cannot detect this -- the exclusive test must go through CreateFile with a
share mode of 0.

See IMPLEMENTATION-PLAN.md sections 2.5 and 3.3. Decisions D2, D3, D6, D7, D15.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

import pywintypes
import win32file

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0
STABLE_POLLS_REQUIRED = 3
SETTLE_TIMEOUT_SECONDS = 30 * 60  # D6


class Outcome(Enum):
    """What the caller should do with this candidate."""

    WAITING = "waiting"          # stay at candidate, poll again
    READY = "ready"              # promote; the upload worker will find it
    FAILED_TIMEOUT = "failed_timeout"    # D2: never settled
    FAILED_VANISHED = "failed_vanished"  # D3: was real, then disappeared
    DROPPED_PHANTOM = "dropped_phantom"  # an event for a file that never existed
    DROPPED_TOO_OLD = "dropped_too_old"  # D15: older than backfill_since


@dataclass(frozen=True)
class Candidate:
    """Mirrors the columns the row will carry in Phase 2.

    Keeping the shape identical now means swapping the in-memory dict for
    db.py later changes the storage and nothing about the logic.
    """

    path: Path
    size_bytes: int | None = None
    mtime: float | None = None
    stable_count: int = 0
    first_seen_at: float = 0.0

    @property
    def ever_seen(self) -> bool:
        """True once the file has been successfully stat'd at least once.

        Distinguishes 'this file disappeared' from 'this event was never about
        a real file', which decides FAILED_VANISHED versus DROPPED_PHANTOM.
        """
        return self.size_bytes is not None


@dataclass(frozen=True)
class Probe:
    """What the disk said. None instead of a Probe means the file is not there."""

    size_bytes: int
    mtime: float


@dataclass(frozen=True)
class Decision:
    candidate: Candidate
    outcome: Outcome
    reason: str = ""


def evaluate(
    candidate: Candidate,
    probe: Probe | None,
    now: float,
    *,
    exclusive_open: bool = False,
    stable_required: int = STABLE_POLLS_REQUIRED,
    timeout_seconds: float = SETTLE_TIMEOUT_SECONDS,
    backfill_since: float | None = None,
) -> Decision:
    """Pure decision step. No I/O.

    `exclusive_open` is the result of an exclusive-open attempt, which the
    caller only performs once the size test has already passed -- there is no
    point paying for a file handle on a file that is still growing.
    """
    if probe is None:
        if candidate.ever_seen:
            return Decision(candidate, Outcome.FAILED_VANISHED, "file disappeared after being seen")
        return Decision(candidate, Outcome.DROPPED_PHANTOM, "event for a file that never existed")

    # D15. Checked before anything else: an old file must never reach the size
    # test, because an untouched file passes it immediately and would promote.
    if backfill_since is not None and probe.mtime < backfill_since:
        return Decision(candidate, Outcome.DROPPED_TOO_OLD, f"mtime {probe.mtime:.0f} < backfill_since {backfill_since:.0f}")

    if candidate.size_bytes == probe.size_bytes:
        updated = replace(
            candidate,
            size_bytes=probe.size_bytes,
            mtime=probe.mtime,
            stable_count=candidate.stable_count + 1,
        )
    else:
        # Still growing. Reset the counter -- three *consecutive* polls.
        updated = replace(
            candidate,
            size_bytes=probe.size_bytes,
            mtime=probe.mtime,
            stable_count=0,
        )

    if updated.stable_count >= stable_required:
        if exclusive_open:
            return Decision(updated, Outcome.READY, f"stable x{updated.stable_count} and exclusive open succeeded")
        # Size is stable but the handle is still held. This is the case a
        # size-only check gets wrong. Keep waiting; the timeout is the backstop.
        reason = f"stable x{updated.stable_count} but file is still locked"
    else:
        reason = f"stable x{updated.stable_count} of {stable_required}"

    if (now - updated.first_seen_at) > timeout_seconds:
        return Decision(updated, Outcome.FAILED_TIMEOUT, f"settle timeout after {timeout_seconds:.0f}s")

    return Decision(updated, Outcome.WAITING, reason)


def needs_exclusive_check(candidate: Candidate, probe: Probe | None, stable_required: int = STABLE_POLLS_REQUIRED) -> bool:
    """Whether this pass should pay for an exclusive-open attempt."""
    if probe is None:
        return False
    return candidate.size_bytes == probe.size_bytes and (candidate.stable_count + 1) >= stable_required


# --- I/O seams. Swapped for fakes in tests. -------------------------------


def probe_file(path: Path) -> Probe | None:
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        log.warning("stat failed for %s: %s", path, e)
        return None
    return Probe(size_bytes=st.st_size, mtime=st.st_mtime)


def try_exclusive_open(path: Path) -> bool:
    """Open with share mode 0. Fails while any other process holds the file.

    The handle is closed explicitly in every path -- a leaked exclusive handle
    would block NVIDIA from writing the file later, and roadblock 12 names this
    module's handles specifically.
    """
    try:
        handle = win32file.CreateFile(
            str(path),
            win32file.GENERIC_READ,
            0,  # share with nobody
            None,
            win32file.OPEN_EXISTING,
            win32file.FILE_ATTRIBUTE_NORMAL,
            None,
        )
    except pywintypes.error as e:
        log.debug("exclusive open refused for %s: %s", path.name, e.strerror)
        return False
    try:
        return True
    finally:
        win32file.CloseHandle(handle)


class DbSettleChecker:
    """Phase 2 settle checker. Same `evaluate`, rows instead of a dict.

    Every decision below is the identical pure function the Phase 1 loop and
    the unit tests use. Only where the state lives has changed, which is the
    whole reason `evaluate` takes its inputs as arguments.

    Note the clock is wall time, not monotonic: `first_seen_at` is persisted,
    so the timeout has to survive a restart, and a monotonic value would not.
    """

    def __init__(
        self,
        db,
        backfill_since: float | None = None,
        timeout_seconds: float = SETTLE_TIMEOUT_SECONDS,
        clock=time.time,
        stat_fn=probe_file,
        exclusive_fn=try_exclusive_open,
    ) -> None:
        self._db = db
        self._backfill_since = backfill_since
        self._timeout = timeout_seconds
        self._clock = clock
        self._stat = stat_fn
        self._exclusive = exclusive_fn

    def poll_once(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for row in self._db.candidates():
            candidate = Candidate(
                path=row.path,
                size_bytes=row.size_bytes,
                mtime=row.mtime,
                stable_count=row.stable_count,
                first_seen_at=row.first_seen_at,
            )
            probe = self._stat(row.path)
            exclusive = self._exclusive(row.path) if needs_exclusive_check(candidate, probe) else False

            decision = evaluate(
                candidate,
                probe,
                self._clock(),
                exclusive_open=exclusive,
                timeout_seconds=self._timeout,
                backfill_since=self._backfill_since,
            )
            self._apply(row.id, decision)
            tally[decision.outcome.value] = tally.get(decision.outcome.value, 0) + 1
        return tally

    def _apply(self, clip_id: int, decision: Decision) -> None:
        c = decision.candidate
        outcome = decision.outcome

        if outcome is Outcome.WAITING:
            self._db.record_probe(clip_id, c.size_bytes or 0, c.mtime or 0.0, c.stable_count)
        elif outcome is Outcome.READY:
            self._db.record_probe(clip_id, c.size_bytes or 0, c.mtime or 0.0, c.stable_count)
            self._db.mark_ready(clip_id)
            log.info("ready: %s (%s)", c.path.name, decision.reason)
        elif outcome in (Outcome.FAILED_TIMEOUT, Outcome.FAILED_VANISHED):
            self._db.mark_settle_timeout(clip_id, decision.reason)
            log.info("%s: %s (%s)", outcome.value, c.path.name, decision.reason)
        else:  # DROPPED_PHANTOM, DROPPED_TOO_OLD -- D15
            self._db.drop(clip_id)
            log.debug("%s: %s (%s)", outcome.value, c.path.name, decision.reason)


class SettleLoop:
    """Phase 1: holds candidates in a dict. Phase 2: db.py holds them instead.

    `on_ready` is the promotion point. In Phase 1 it calls the upload code
    directly; in Phase 2 it becomes a state transition and the upload worker
    finds the row on its own next pass.
    """

    def __init__(
        self,
        on_ready,
        on_dropped=None,
        backfill_since: float | None = None,
        clock=time.monotonic,
        stat_fn=probe_file,
        exclusive_fn=try_exclusive_open,
    ) -> None:
        self._on_ready = on_ready
        self._on_dropped = on_dropped or (lambda c, o, r: None)
        self._backfill_since = backfill_since
        self._clock = clock
        self._stat = stat_fn
        self._exclusive = exclusive_fn
        self._candidates: dict[str, Candidate] = {}

    def add(self, path: Path) -> None:
        """Idempotent, exactly like the ON CONFLICT DO NOTHING insert it becomes."""
        key = str(path).lower()
        if key not in self._candidates:
            self._candidates[key] = Candidate(path=path, first_seen_at=self._clock())

    @property
    def pending(self) -> int:
        return len(self._candidates)

    def poll_once(self) -> list[Decision]:
        decisions: list[Decision] = []
        for key, candidate in list(self._candidates.items()):
            probe = self._stat(candidate.path)
            exclusive = False
            if needs_exclusive_check(candidate, probe):
                exclusive = self._exclusive(candidate.path)

            decision = evaluate(
                candidate,
                probe,
                self._clock(),
                exclusive_open=exclusive,
                backfill_since=self._backfill_since,
            )
            decisions.append(decision)

            if decision.outcome is Outcome.WAITING:
                self._candidates[key] = decision.candidate
                continue

            del self._candidates[key]
            if decision.outcome is Outcome.READY:
                log.info("ready: %s (%s)", candidate.path.name, decision.reason)
                self._on_ready(decision.candidate)
            else:
                log.info("%s: %s (%s)", decision.outcome.value, candidate.path.name, decision.reason)
                self._on_dropped(decision.candidate, decision.outcome, decision.reason)

        return decisions
