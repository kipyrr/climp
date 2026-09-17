"""The state table. Owns the schema, the connections, and every SQL statement.

No SQL exists anywhere else in this codebase. Every component calls a named
function here, and the function name is the state transition. When something
goes wrong with locking, ordering or duplicates, this is the only file to read.

Concurrency: four loops share one database file, so WAL is on (readers never
block the writer), busy_timeout absorbs contention, and each thread gets its
own connection via threading.local. None of that is visible to callers.

See IMPLEMENTATION-PLAN.md sections 2.3, 2.3.1, 2.3.2, 2.3.3.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CANDIDATE = "candidate"
READY = "ready"
UPLOADING = "uploading"
DONE = "done"
FAILED = "failed"

ALL_STATES = (CANDIDATE, READY, UPLOADING, DONE, FAILED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
  id              INTEGER PRIMARY KEY,
  path            TEXT    NOT NULL,          -- original spelling, for display
  path_key        TEXT    NOT NULL UNIQUE,   -- normalised; THE duplicate guard (D16)
  size_bytes      INTEGER,
  mtime           REAL,
  content_hash    TEXT,                      -- reserved, unused (D9)
  state           TEXT    NOT NULL CHECK (state IN
                          ('candidate','ready','uploading','done','failed')),
  stable_count    INTEGER NOT NULL DEFAULT 0,
  first_seen_at   REAL    NOT NULL,
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  next_attempt_at REAL,
  session_uri     TEXT,
  drive_file_id   TEXT,
  drive_link      TEXT,
  retired_at      REAL,                      -- removed from Drive by retention (D17)
  updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_clips_state ON clips(state);
CREATE INDEX IF NOT EXISTS ix_clips_claim ON clips(state, next_attempt_at);
"""


def normalise(path: Path | str) -> str:
    """The one place a path is canonicalised.

    Windows hands the same file to watchdog and os.walk with different
    spellings -- differing case, or a different root. Two spellings would mean
    two rows and two uploads, silently defeating the unique index. Doing this
    in exactly one place is what stops that drifting.
    """
    return os.path.normcase(os.path.abspath(str(path)))


@dataclass(frozen=True)
class Clip:
    id: int
    path: Path
    state: str
    size_bytes: int | None
    mtime: float | None
    stable_count: int
    first_seen_at: float
    attempts: int
    last_error: str | None
    next_attempt_at: float | None
    session_uri: str | None
    drive_file_id: str | None
    drive_link: str | None
    retired_at: float | None
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Clip:
        return cls(
            id=row["id"],
            path=Path(row["path"]),
            state=row["state"],
            size_bytes=row["size_bytes"],
            mtime=row["mtime"],
            stable_count=row["stable_count"],
            first_seen_at=row["first_seen_at"],
            attempts=row["attempts"],
            last_error=row["last_error"],
            next_attempt_at=row["next_attempt_at"],
            session_uri=row["session_uri"],
            drive_file_id=row["drive_file_id"],
            drive_link=row["drive_link"],
            retired_at=row["retired_at"] if "retired_at" in row.keys() else None,
            updated_at=row["updated_at"],
        )


class Db:
    def __init__(self, db_path: Path | str, clock=time.time) -> None:
        self.db_path = str(db_path)
        self._clock = clock
        self._local = threading.local()
        self._init_lock = threading.Lock()

    # --- connection management ------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.isolation_level = None  # we manage transactions explicitly
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def init_schema(self) -> None:
        with self._init_lock:
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        SQLite has no IF NOT EXISTS for ADD COLUMN, so the existing columns are
        read first. Kept here rather than in a migration framework because the
        whole schema is one table.
        """
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(clips)")}
        for column, ddl in (("retired_at", "REAL"),):
            if column not in existing:
                log.info("migrating: adding clips.%s", column)
                self.conn.execute(f"ALTER TABLE clips ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # --- watcher and reconciler -----------------------------------------

    def insert_candidate(self, path: Path | str) -> bool:
        """Idempotent insert. Returns True only if a new row was created.

        Both inserting components go through this one function, which is what
        makes the duplicate guard a single mechanism rather than two.
        """
        now = self._clock()
        cur = self.conn.execute(
            """INSERT INTO clips (path, path_key, state, stable_count, first_seen_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?)
               ON CONFLICT(path_key) DO NOTHING""",
            (str(path), normalise(path), CANDIDATE, now, now),
        )
        return cur.rowcount > 0

    def known_path_keys(self) -> set[str]:
        return {r["path_key"] for r in self.conn.execute("SELECT path_key FROM clips")}

    # --- settle checker ---------------------------------------------------

    def candidates(self, limit: int = 500) -> list[Clip]:
        rows = self.conn.execute(
            "SELECT * FROM clips WHERE state = ? ORDER BY first_seen_at LIMIT ?",
            (CANDIDATE, limit),
        ).fetchall()
        return [Clip.from_row(r) for r in rows]

    def record_probe(self, clip_id: int, size_bytes: int, mtime: float, stable_count: int) -> None:
        self.conn.execute(
            "UPDATE clips SET size_bytes=?, mtime=?, stable_count=?, updated_at=? WHERE id=?",
            (size_bytes, mtime, stable_count, self._clock(), clip_id),
        )

    def mark_ready(self, clip_id: int) -> None:
        self._transition(clip_id, from_states=(CANDIDATE, FAILED), to_state=READY)

    def mark_settle_timeout(self, clip_id: int, reason: str) -> None:
        self.conn.execute(
            "UPDATE clips SET state=?, last_error=?, updated_at=? WHERE id=? AND state=?",
            (FAILED, reason, self._clock(), clip_id, CANDIDATE),
        )

    def drop(self, clip_id: int) -> None:
        """Remove a row entirely. Only for D15 backfill drops and phantoms.

        Deliberately not a state: a dropped row carries no information worth
        keeping, and if the watcher re-inserts it the settle checker simply
        drops it again for one insert and one delete.
        """
        self.conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))

    # --- upload worker ----------------------------------------------------

    def claim_ready(self, limit: int = 1) -> list[Clip]:
        """Select and mark in one transaction, so two workers cannot take one row.

        BEGIN IMMEDIATE takes the write lock up front, so the SELECT inside
        cannot see a row another worker is about to claim.
        """
        now = self._clock()
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """UPDATE clips SET state=?, updated_at=?
                    WHERE id IN (
                      SELECT id FROM clips
                       WHERE state=?
                         AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                       ORDER BY first_seen_at
                       LIMIT ?)
                 RETURNING *""",
                (UPLOADING, now, READY, now, limit),
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return [Clip.from_row(r) for r in rows]

    def save_session_uri(self, clip_id: int, uri: str) -> None:
        self.conn.execute(
            "UPDATE clips SET session_uri=?, updated_at=? WHERE id=?",
            (uri, self._clock(), clip_id),
        )

    def clear_session_uri(self, clip_id: int) -> None:
        self.conn.execute(
            "UPDATE clips SET session_uri=NULL, updated_at=? WHERE id=?",
            (self._clock(), clip_id),
        )

    def mark_done(self, clip_id: int, drive_file_id: str, drive_link: str) -> None:
        """One statement, so a crash cannot leave a done row with no file id."""
        self.conn.execute(
            """UPDATE clips
                  SET state=?, drive_file_id=?, drive_link=?, last_error=NULL, updated_at=?
                WHERE id=?""",
            (DONE, drive_file_id, drive_link, self._clock(), clip_id),
        )

    def mark_retry(self, clip_id: int, error: str, next_attempt_at: float) -> None:
        """Back to ready, but invisible to claim_ready until the delay elapses."""
        self.conn.execute(
            """UPDATE clips
                  SET state=?, attempts=attempts+1, last_error=?, next_attempt_at=?, updated_at=?
                WHERE id=?""",
            (READY, error, next_attempt_at, self._clock(), clip_id),
        )

    def mark_auth_blocked(self, clip_id: int, error: str) -> None:
        """Requeue without burning an attempt.

        D13: the app runs in OAuth Testing mode, so the token dies weekly. A
        queue of clips hit by one expiry must not exhaust its retry ceiling for
        a reason that has nothing to do with the clips.
        """
        self.conn.execute(
            "UPDATE clips SET state=?, last_error=?, updated_at=? WHERE id=?",
            (READY, error, self._clock(), clip_id),
        )

    def mark_failed(self, clip_id: int, error: str) -> None:
        self.conn.execute(
            """UPDATE clips
                  SET state=?, attempts=attempts+1, last_error=?, updated_at=?
                WHERE id=?""",
            (FAILED, error, self._clock(), clip_id),
        )

    def recover_stranded_uploads(self, stale_after_seconds: float | None = None) -> int:
        """Hand rows left at uploading back to the queue, session URI intact.

        Called at startup for crash and kill recovery, and periodically with
        stale_after_seconds for the sleep and hibernate case (roadblock 7),
        where the process is suspended and wakes to a dead socket.
        """
        now = self._clock()
        if stale_after_seconds is None:
            cur = self.conn.execute(
                "UPDATE clips SET state=?, next_attempt_at=NULL, updated_at=? WHERE state=?",
                (READY, now, UPLOADING),
            )
        else:
            cur = self.conn.execute(
                "UPDATE clips SET state=?, next_attempt_at=NULL, updated_at=? WHERE state=? AND updated_at < ?",
                (READY, now, UPLOADING, now - stale_after_seconds),
            )
        if cur.rowcount:
            log.info("recovered %d stranded upload(s)", cur.rowcount)
        return cur.rowcount

    # --- retention (D17) --------------------------------------------------

    def retention_candidates(self, keep_newest: int, min_age_seconds: float, now: float) -> list[Clip]:
        """Uploaded clips eligible for removal from Drive.

        Ordered newest first by recording time, then everything past the keep
        count is a candidate -- provided it is also older than the age floor.
        The floor is a safety net: it means a misconfigured keep count can
        never delete something uploaded minutes ago.

        Only rows that are done, still have a Drive file id, and have not
        already been retired are ever returned.
        """
        rows = self.conn.execute(
            """SELECT * FROM clips
                WHERE state = ?
                  AND drive_file_id IS NOT NULL
                  AND retired_at IS NULL
                ORDER BY COALESCE(mtime, first_seen_at) DESC""",
            (DONE,),
        ).fetchall()

        clips = [Clip.from_row(r) for r in rows]
        older = clips[keep_newest:]
        return [c for c in older if (now - (c.mtime or c.first_seen_at)) >= min_age_seconds]

    def mark_retired(self, clip_id: int) -> None:
        """Record that the Drive copy is gone.

        The row stays at done deliberately. That is what stops the reconciler
        re-uploading the clip on the next start, which would undo the retention
        and refill the quota.
        """
        self.conn.execute(
            "UPDATE clips SET retired_at=?, updated_at=? WHERE id=?",
            (self._clock(), self._clock(), clip_id),
        )

    def retired_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM clips WHERE retired_at IS NOT NULL"
        ).fetchone()["n"]

    # --- tray -------------------------------------------------------------

    def counts_by_state(self) -> dict[str, int]:
        counts = dict.fromkeys(ALL_STATES, 0)
        for row in self.conn.execute("SELECT state, COUNT(*) n FROM clips GROUP BY state"):
            counts[row["state"]] = row["n"]
        return counts

    def recent_failures(self, limit: int = 10) -> list[Clip]:
        rows = self.conn.execute(
            "SELECT * FROM clips WHERE state=? ORDER BY updated_at DESC LIMIT ?",
            (FAILED, limit),
        ).fetchall()
        return [Clip.from_row(r) for r in rows]

    def request_retry(self, clip_id: int) -> None:
        """D4: back to candidate, not ready.

        One uniform reset for every failed row. A settle-timeout row must be
        re-verified before it can upload, and re-settling costs about eight
        seconds. session_uri is preserved, so an upload that failed part-way
        still resumes rather than restarting.
        """
        self.conn.execute(
            """UPDATE clips
                  SET state=?, attempts=0, stable_count=0, next_attempt_at=NULL,
                      last_error=NULL, first_seen_at=?, updated_at=?
                WHERE id=? AND state=?""",
            (CANDIDATE, self._clock(), self._clock(), clip_id, FAILED),
        )

    def get(self, clip_id: int) -> Clip | None:
        row = self.conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        return Clip.from_row(row) if row else None

    def by_state(self, state: str) -> list[Clip]:
        rows = self.conn.execute("SELECT * FROM clips WHERE state=? ORDER BY id", (state,)).fetchall()
        return [Clip.from_row(r) for r in rows]

    # --- internal ---------------------------------------------------------

    def _transition(self, clip_id: int, from_states: tuple[str, ...], to_state: str) -> None:
        placeholders = ",".join("?" * len(from_states))
        self.conn.execute(
            f"UPDATE clips SET state=?, updated_at=? WHERE id=? AND state IN ({placeholders})",
            (to_state, self._clock(), clip_id, *from_states),
        )
