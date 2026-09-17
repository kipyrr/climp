"""Upload worker behaviour, against a fake Drive client.

No network, no quota spent, and every failure mode reachable on demand --
which is the point of keeping drive.py free of state and uploader.py free of
Drive specifics.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import httplib2
import pytest
from googleapiclient.errors import HttpError

from clipsync.db import DONE, FAILED, READY, UPLOADING, Db
from clipsync.drive import AuthExpired, ResumableUpload, SessionExpired
from clipsync.uploader import (
    ErrorClass,
    Result,
    UploadWorker,
    backoff_delay,
    classify,
)

CLIP = r"C:\clips\Marvel Rivals\clip.mp4"
SESSION = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=abc"


def http_error(status: int, reason: str = "", message: str = "nope") -> HttpError:
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason}] if reason else []}}
    resp = httplib2.Response({"status": status, "reason": message})
    resp.status = status
    return HttpError(resp, json.dumps(body).encode())


class FakeRequest:
    """Mimics a resumable upload request: session URI appears after chunk one."""

    def __init__(self, chunks: int = 2, raise_on_chunk: int | None = None, exc=None, resume_from: str | None = None):
        self.chunks = chunks
        self.raise_on_chunk = raise_on_chunk
        self.exc = exc
        self.resumable_uri = resume_from
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        if self.raise_on_chunk is not None and self.calls == self.raise_on_chunk:
            raise self.exc
        if self.resumable_uri is None:
            self.resumable_uri = SESSION
        if self.calls >= self.chunks:
            return None, {"id": "driveid123", "name": "clip.mp4", "webViewLink": "https://drive/driveid123"}
        return object(), None


class FakeClient:
    def __init__(self, request_factory=None, already_complete=None, raise_on_build=None):
        self.request_factory = request_factory or (lambda: FakeRequest())
        self.already_complete = already_complete
        self.raise_on_build = raise_on_build
        self.built_with: list[tuple[Path, str, str | None]] = []

    def build_upload(self, path, folder_id, session_uri=None):
        self.built_with.append((path, folder_id, session_uri))
        if self.raise_on_build is not None:
            raise self.raise_on_build
        if self.already_complete is not None:
            return ResumableUpload(request=None, completed=self.already_complete)
        return ResumableUpload(request=self.request_factory())


class FakeClock:
    def __init__(self, t=1000.0):
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


def ready_clip(db: Db, path: str = CLIP):
    db.insert_candidate(path)
    clip = [c for c in db.by_state("candidate") if str(c.path) == path][0]
    db.mark_ready(clip.id)
    return clip.id


def worker(db, client, clock, **kw):
    return UploadWorker(db, client, "folder123", clock=clock, jitter=lambda a, b: 1.0, **kw)


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    "exc, expected",
    [
        (http_error(429), ErrorClass.RETRYABLE),
        (http_error(500), ErrorClass.RETRYABLE),
        (http_error(503), ErrorClass.RETRYABLE),
        (http_error(403, "rateLimitExceeded"), ErrorClass.RETRYABLE),
        (http_error(403, "userRateLimitExceeded"), ErrorClass.RETRYABLE),
        (http_error(403, "storageQuotaExceeded"), ErrorClass.PERMANENT),
        (http_error(404), ErrorClass.PERMANENT),
        (http_error(410), ErrorClass.SESSION_GONE),
        (http_error(400, "badRequest"), ErrorClass.PERMANENT),
        (AuthExpired("token dead"), ErrorClass.AUTH),
        (FileNotFoundError("gone"), ErrorClass.PERMANENT),
        (PermissionError("locked"), ErrorClass.PERMANENT),
        (ConnectionResetError("reset"), ErrorClass.RETRYABLE),
        (socket.timeout("timed out"), ErrorClass.RETRYABLE),
    ],
)
def test_classification(exc, expected):
    kind, _ = classify(exc)
    assert kind is expected


def test_quota_message_is_actionable():
    """Roadblock 2: a full Drive must be readable in the tray, not a status code."""
    _, detail = classify(http_error(403, "storageQuotaExceeded"))
    assert "full" in detail.lower()
    assert "retry" in detail.lower()


def test_wrong_folder_message_names_the_cause():
    _, detail = classify(http_error(404))
    assert "folder" in detail.lower()


# --- backoff --------------------------------------------------------------


def test_backoff_grows_then_caps():
    flat = lambda a, b: 1.0  # noqa: E731
    delays = [backoff_delay(n, jitter=flat) for n in range(10)]
    assert delays[:4] == [1, 2, 4, 8]
    assert max(delays) == 64, "truncated exponential, capped around 64s"


def test_backoff_has_jitter():
    lo = backoff_delay(5, jitter=lambda a, b: a)
    hi = backoff_delay(5, jitter=lambda a, b: b)
    assert lo < hi, "identical delays would synchronise retries after an outage"


# --- the happy path -------------------------------------------------------


def test_successful_upload_reaches_done(db, clock):
    clip_id = ready_clip(db)
    clip = db.claim_ready()[0]
    out = worker(db, FakeClient(), clock).process(clip)

    assert out.result is Result.DONE
    row = db.get(clip_id)
    assert row.state == DONE
    assert row.drive_file_id == "driveid123"
    assert row.drive_link == "https://drive/driveid123"


def test_session_uri_is_persisted_as_soon_as_it_exists(db, clock):
    """Without this on the row, a crash means re-sending the whole file."""
    clip_id = ready_clip(db)
    clip = db.claim_ready()[0]
    worker(db, FakeClient(lambda: FakeRequest(chunks=4)), clock).process(clip)
    # mark_done does not clear it; the point is that it was written at all.
    assert SESSION in (db.get(clip_id).session_uri or "")


# --- failure paths --------------------------------------------------------


def test_retryable_failure_requeues_with_a_future_deadline(db, clock):
    clip_id = ready_clip(db)
    clip = db.claim_ready()[0]
    client = FakeClient(lambda: FakeRequest(raise_on_chunk=1, exc=http_error(503)))

    out = worker(db, client, clock).process(clip)
    assert out.result is Result.RETRY

    row = db.get(clip_id)
    assert row.state == READY
    assert row.attempts == 1
    assert row.next_attempt_at > clock.t
    assert db.claim_ready() == [], "must stay invisible until the backoff elapses"


def test_attempts_exhausted_goes_to_failed_with_a_readable_error(db, clock):
    clip_id = ready_clip(db)
    client = FakeClient(lambda: FakeRequest(raise_on_chunk=1, exc=http_error(503)))
    w = worker(db, client, clock, max_attempts=3)

    for _ in range(3):
        clock.advance(1000)
        claimed = db.claim_ready()
        assert claimed, "row should be reclaimable after backoff"
        w.process(claimed[0])

    row = db.get(clip_id)
    assert row.state == FAILED
    assert row.attempts == 3
    assert "gave up" in row.last_error


def test_drive_full_fails_immediately_rather_than_looping(db, clock):
    """The plan forbids a silent retry loop on a full Drive."""
    clip_id = ready_clip(db)
    clip = db.claim_ready()[0]
    client = FakeClient(lambda: FakeRequest(raise_on_chunk=1, exc=http_error(403, "storageQuotaExceeded")))

    out = worker(db, client, clock).process(clip)
    assert out.result is Result.FAILED
    row = db.get(clip_id)
    assert row.state == FAILED
    assert "full" in row.last_error.lower()


def test_missing_clip_file_fails_without_retrying(db, clock):
    """D3. Retrying a file that no longer exists is an infinite loop."""
    clip_id = ready_clip(db)
    clip = db.claim_ready()[0]
    client = FakeClient(lambda: FakeRequest(raise_on_chunk=1, exc=FileNotFoundError("gone")))

    assert worker(db, client, clock).process(clip).result is Result.FAILED
    assert db.get(clip_id).state == FAILED


# --- auth, D13 ------------------------------------------------------------


def test_auth_failure_requeues_without_burning_an_attempt(db, clock):
    clip_id = ready_clip(db)
    clip = db.claim_ready()[0]
    client = FakeClient(lambda: FakeRequest(raise_on_chunk=1, exc=AuthExpired("refresh failed")))

    notified = []
    out = worker(db, client, clock, on_auth_needed=notified.append).process(clip)

    assert out.result is Result.AUTH_BLOCKED
    row = db.get(clip_id)
    assert row.state == READY
    assert row.attempts == 0, "a weekly expiry must not consume the retry budget"
    assert notified, "the tray has to be told a human is needed"


def test_a_whole_queue_survives_one_token_expiry(db, clock):
    ids = [ready_clip(db, rf"C:\clips\G\clip{i}.mp4") for i in range(10)]
    client = FakeClient(lambda: FakeRequest(raise_on_chunk=1, exc=AuthExpired("dead")))
    w = worker(db, client, clock)

    for clip in db.claim_ready(limit=10):
        w.process(clip)

    assert db.by_state(FAILED) == []
    assert len(db.by_state(READY)) == 10
    assert all(db.get(i).attempts == 0 for i in ids)


# --- resume ---------------------------------------------------------------


def test_a_stored_session_uri_is_handed_back_to_the_client(db, clock):
    """The resume path, rather than starting a fresh upload."""
    clip_id = ready_clip(db)
    db.claim_ready()
    db.save_session_uri(clip_id, SESSION)
    db.recover_stranded_uploads()
    clip = db.claim_ready()[0]

    client = FakeClient(lambda: FakeRequest(resume_from=SESSION))
    worker(db, client, clock).process(clip)

    assert client.built_with[0][2] == SESSION, "resumed against the stored session"
    assert db.get(clip_id).state == DONE


def test_expired_session_is_cleared_so_the_next_try_starts_fresh(db, clock):
    clip_id = ready_clip(db)
    db.claim_ready()
    db.save_session_uri(clip_id, SESSION)
    db.recover_stranded_uploads()
    clip = db.claim_ready()[0]

    client = FakeClient(lambda: FakeRequest(raise_on_chunk=1, exc=http_error(410)))
    out = worker(db, client, clock).process(clip)

    assert out.result is Result.RETRY
    row = db.get(clip_id)
    assert row.session_uri is None, "a dead session must not be retried against"
    assert row.state == READY


# --- shutdown -------------------------------------------------------------


def test_shutdown_mid_upload_leaves_the_row_recoverable(db, clock):
    """The row stays at uploading; startup recovery is what brings it back."""
    clip_id = ready_clip(db)
    clip = db.claim_ready()[0]
    stop = threading.Event()

    class StoppingRequest(FakeRequest):
        def next_chunk(self):
            out = super().next_chunk()
            stop.set()  # shutdown requested after the first chunk
            return out

    client = FakeClient(lambda: StoppingRequest(chunks=5))
    out = worker(db, client, clock, stop_event=stop).process(clip)

    assert out.result is Result.ABORTED
    row = db.get(clip_id)
    assert row.state == UPLOADING
    assert row.session_uri is not None

    assert db.recover_stranded_uploads() == 1
    assert db.get(clip_id).state == READY


# --- resume, after the gate test found it silently broken ----------------


def test_session_expired_during_the_offset_query_clears_the_uri(db, clock):
    clip_id = ready_clip(db)
    db.claim_ready()
    db.save_session_uri(clip_id, SESSION)
    db.recover_stranded_uploads()
    clip = db.claim_ready()[0]

    client = FakeClient(raise_on_build=SessionExpired("Drive no longer recognises the session (410)"))
    out = worker(db, client, clock).process(clip)

    assert out.result is Result.RETRY
    row = db.get(clip_id)
    assert row.session_uri is None
    assert row.state == READY


def test_a_session_the_server_already_completed_is_recorded_not_resent(db, clock):
    """Crash between the final chunk and mark_done. Re-sending would be waste."""
    clip_id = ready_clip(db)
    db.claim_ready()
    db.save_session_uri(clip_id, SESSION)
    db.recover_stranded_uploads()
    clip = db.claim_ready()[0]

    meta = {"id": "already-there", "webViewLink": "https://drive/already-there"}
    client = FakeClient(already_complete=meta)
    out = worker(db, client, clock).process(clip)

    assert out.result is Result.DONE
    row = db.get(clip_id)
    assert row.state == DONE
    assert row.drive_file_id == "already-there"
