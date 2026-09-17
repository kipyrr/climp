"""Upload worker — drain ready rows with bounded concurrency and backoff.

Owns the uploading state and the session URI, and therefore also owns startup
recovery of rows a crash left mid-upload.

The interesting part is `classify`, which decides retry versus fail. Getting it
wrong in either direction is expensive: too eager to fail and good clips end up
in the tray needing manual attention; too eager to retry and a full Drive
becomes a silent loop. It is a pure function so both directions are testable.

See IMPLEMENTATION-PLAN.md section 2.7. Decisions D1, D3, D8, D10, D13.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import threading
import time
from dataclasses import dataclass
from enum import Enum

import httplib2
from googleapiclient.errors import HttpError

from climp.db import Clip, Db
from climp.drive import AuthExpired, SessionExpired

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6          # D8
BACKOFF_CAP_SECONDS = 64  # blueprint: truncated exponential, capped around 64
IDLE_SLEEP_SECONDS = 2.0


class ErrorClass(Enum):
    RETRYABLE = "retryable"        # transient; back off and try again
    PERMANENT = "permanent"        # will never succeed unaided; go to failed
    AUTH = "auth"                  # a human must sign in; do not burn an attempt
    SESSION_GONE = "session_gone"  # resumable session expired; start a fresh one


class Result(Enum):
    DONE = "done"
    RETRY = "retry"
    FAILED = "failed"
    AUTH_BLOCKED = "auth_blocked"
    ABORTED = "aborted"  # shutdown mid-transfer; the row stays claimable


RETRYABLE_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "backenderror", "internalerror"}
QUOTA_REASONS = {"storagequotaexceeded", "quotaexceeded"}


def classify(exc: BaseException) -> tuple[ErrorClass, str]:
    """Decide what an exception means. Pure; no I/O, no state."""
    if isinstance(exc, AuthExpired):
        return ErrorClass.AUTH, f"Sign in again: {exc}"

    if isinstance(exc, SessionExpired):
        return ErrorClass.SESSION_GONE, str(exc)

    # Local filesystem problems. D3: the clip was deleted or moved while
    # queued. Retrying a file that no longer exists is an infinite loop.
    if isinstance(exc, FileNotFoundError):
        return ErrorClass.PERMANENT, "Clip file no longer exists"
    if isinstance(exc, PermissionError):
        return ErrorClass.PERMANENT, f"Cannot read clip file: {exc}"

    if isinstance(exc, HttpError):
        status = exc.resp.status
        reason = _reason(exc)

        if status == 410:
            return ErrorClass.SESSION_GONE, "Upload session expired; restarting"
        if status == 404:
            return ErrorClass.PERMANENT, "Drive folder not found - check the folder ID in config"
        if status in (401, 403) and reason in {"autherror", "unauthorized"}:
            return ErrorClass.AUTH, "Drive rejected the credentials; sign in again"
        if reason in QUOTA_REASONS:
            # Retrying forever would be the silent loop the plan forbids. Fail
            # loudly so the tray shows it; the retry button brings it back once
            # space is freed.
            return ErrorClass.PERMANENT, "Google Drive is full - free up space, then press Retry"
        if status == 429 or reason in RETRYABLE_REASONS:
            return ErrorClass.RETRYABLE, f"Rate limited ({status} {reason})"
        if 500 <= status < 600:
            return ErrorClass.RETRYABLE, f"Drive server error ({status})"
        return ErrorClass.PERMANENT, f"Drive rejected the upload ({status} {reason})"

    # Roadblock 5 and 7: dropped Wi-Fi, or a machine that slept and woke to a
    # dead socket. Always retryable -- the session URI makes resuming cheap.
    if isinstance(exc, (socket.timeout, socket.error, ConnectionError, OSError, httplib2.HttpLib2Error)):
        return ErrorClass.RETRYABLE, f"Connection problem: {type(exc).__name__}: {exc}"

    return ErrorClass.PERMANENT, f"{type(exc).__name__}: {exc}"


def _reason(exc: HttpError) -> str:
    """Pull Drive's machine-readable reason out of the error body.

    Parsed directly rather than via HttpError.error_details, whose shape has
    moved between client-library versions. Classification depends on this
    string, so it should not be able to break on a dependency bump.
    """
    try:
        content = exc.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        error = json.loads(content).get("error", {})
        errors = error.get("errors") or []
        if errors and isinstance(errors[0], dict):
            return str(errors[0].get("reason", "")).lower()
        return str(error.get("status", "")).lower()
    except Exception:
        return ""


def backoff_delay(attempts: int, cap: float = BACKOFF_CAP_SECONDS, jitter=random.uniform) -> float:
    """Truncated exponential with jitter. attempts is the count BEFORE this failure."""
    base = min(2.0 ** max(attempts, 0), cap)
    return base * jitter(0.8, 1.2)


@dataclass
class UploadOutcome:
    result: Result
    detail: str = ""


class UploadWorker:
    def __init__(
        self,
        db: Db,
        client,
        folder_id: str,
        stop_event: threading.Event | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        clock=time.time,
        jitter=random.uniform,
        on_auth_needed=None,
        should_defer=None,
    ) -> None:
        self.db = db
        self.client = client
        self.folder_id = folder_id
        self.stop = stop_event or threading.Event()
        self.max_attempts = max_attempts
        self._clock = clock
        self._jitter = jitter
        self._on_auth_needed = on_auth_needed or (lambda detail: None)
        # Roadblock 4. A condition on the claim, not a new component: the
        # worker simply declines to start something new while you are playing.
        # An upload already in flight is allowed to finish, which bounds the
        # interference at one clip rather than aborting work already done.
        self._should_defer = should_defer or (lambda: False)
        self._deferring = False

    def run(self) -> None:
        """Drain the queue until told to stop. One thread per concurrent upload."""
        while not self.stop.is_set():
            if self._deferred():
                self.stop.wait(IDLE_SLEEP_SECONDS)
                continue
            claimed = self.db.claim_ready(limit=1)
            if not claimed:
                self.stop.wait(IDLE_SLEEP_SECONDS)
                continue
            self.process(claimed[0])

    def _deferred(self) -> bool:
        try:
            deferring = bool(self._should_defer())
        except Exception:
            # Never let a broken check stop uploads forever. Failing open
            # costs some latency once; failing closed loses clips silently.
            log.debug("defer check failed; proceeding", exc_info=True)
            return False

        if deferring != self._deferring:
            log.info("uploads %s", "paused (fullscreen app)" if deferring else "resumed")
            self._deferring = deferring
        return deferring

    @property
    def deferring(self) -> bool:
        return self._deferring

    def process(self, clip: Clip) -> UploadOutcome:
        """Upload one claimed row and record what happened. Never raises."""
        try:
            upload = self.client.build_upload(clip.path, self.folder_id, clip.session_uri)
            if upload.completed is not None:
                # The server already had every byte; we died before recording it.
                log.info("already complete on Drive: %s", clip.path.name)
                response = upload.completed
            else:
                response = self._pump(upload.request, clip)
                if response is None:
                    return UploadOutcome(Result.ABORTED, "shutdown during upload")
        except BaseException as exc:  # noqa: BLE001 - classification is the whole point
            return self._record_failure(clip, exc)

        self.db.mark_done(clip.id, response["id"], response.get("webViewLink", ""))
        log.info("done: %s -> %s", clip.path.name, response["id"])
        return UploadOutcome(Result.DONE, response["id"])

    def _pump(self, request, clip: Clip):
        """Send chunks until complete. Returns None if shutdown interrupted it.

        The session URI is saved as soon as the library exposes it, which is
        after the first chunk rather than before it: the library creates the
        session and sends the first chunk inside one next_chunk() call, so
        there is no earlier moment to read it. A crash inside that first call
        therefore abandons one session -- wasteful, not lossy, and unavoidable
        without reimplementing the upload protocol by hand.
        """
        resuming = clip.session_uri
        saved = resuming is not None
        if resuming:
            # Set by build_upload from the server's own byte count, so this is
            # where the resume really continues from.
            log.info("resuming %s at byte %d", clip.path.name, getattr(request, "resumable_progress", 0))
        response = None
        while response is None:
            if self.stop.is_set():
                return None
            _status, response = request.next_chunk()
            if not saved and getattr(request, "resumable_uri", None):
                self.db.save_session_uri(clip.id, request.resumable_uri)
                saved = True
        return response

    def _record_failure(self, clip: Clip, exc: BaseException) -> UploadOutcome:
        kind, detail = classify(exc)
        log.warning("upload failed for %s: %s (%s)", clip.path.name, detail, kind.value)

        if kind is ErrorClass.AUTH:
            # D13. No attempt consumed: a weekly token expiry must not push a
            # queue of perfectly good clips into failed.
            self.db.mark_auth_blocked(clip.id, detail)
            self._on_auth_needed(detail)
            return UploadOutcome(Result.AUTH_BLOCKED, detail)

        if kind is ErrorClass.SESSION_GONE:
            self.db.clear_session_uri(clip.id)
            self.db.mark_retry(clip.id, detail, next_attempt_at=self._clock())
            return UploadOutcome(Result.RETRY, detail)

        if kind is ErrorClass.RETRYABLE and (clip.attempts + 1) < self.max_attempts:
            delay = backoff_delay(clip.attempts, jitter=self._jitter)
            self.db.mark_retry(clip.id, detail, next_attempt_at=self._clock() + delay)
            return UploadOutcome(Result.RETRY, f"{detail}; retry in {delay:.0f}s")

        if kind is ErrorClass.RETRYABLE:
            detail = f"{detail} (gave up after {clip.attempts + 1} attempts)"

        self.db.mark_failed(clip.id, detail)
        return UploadOutcome(Result.FAILED, detail)
