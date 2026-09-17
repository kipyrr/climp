"""The resumable offset query.

Exists because of a measured failure: setting `resumable_uri` on a fresh
request does NOT resume. The library never asks the server where it got to, so
it re-sends from byte zero under the old session. A 120 MB gate test reported
success while uploading every byte twice. These tests pin the fix.
"""

from __future__ import annotations

import json

import pytest

from climp.drive import DriveClient, SessionExpired

SESSION = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=abc"
TOTAL = 125_829_120


class FakeResponse(dict):
    def __init__(self, status, **headers):
        super().__init__(**headers)
        self.status = status


class FakeHttp:
    def __init__(self, response, content=b""):
        self.response = response
        self.content = content
        self.calls = []

    def request(self, uri, method, body=None, headers=None):
        self.calls.append((uri, method, body, headers))
        return self.response, self.content


def client_with(http) -> DriveClient:
    c = DriveClient(client_secret=None, token_path=None)

    class FakeService:
        _http = http

    c._service = FakeService()
    return c


def test_asks_with_the_query_form_of_content_range():
    """bytes */total is what makes this a query rather than a write."""
    http = FakeHttp(FakeResponse(308, range="bytes=0-67108863"))
    client_with(http).received_bytes(SESSION, TOTAL)

    _uri, method, body, headers = http.calls[0]
    assert method == "PUT"
    assert body == b"", "any body would append data instead of asking a question"
    assert headers["Content-Range"] == f"bytes */{TOTAL}"


def test_offset_is_one_past_the_last_received_byte():
    """Range is inclusive, so 0-67108863 means 67108864 bytes are held."""
    http = FakeHttp(FakeResponse(308, range="bytes=0-67108863"))
    assert client_with(http).received_bytes(SESSION, TOTAL) == 67_108_864


def test_no_range_header_means_the_server_has_nothing():
    http = FakeHttp(FakeResponse(308))
    assert client_with(http).received_bytes(SESSION, TOTAL) == 0


def test_a_finished_session_returns_the_file_metadata():
    """Crash between the last chunk and marking done: nothing left to send."""
    meta = {"id": "driveid", "name": "clip.mp4", "size": str(TOTAL)}
    http = FakeHttp(FakeResponse(200), json.dumps(meta).encode())
    result = client_with(http).received_bytes(SESSION, TOTAL)
    assert isinstance(result, dict)
    assert result["id"] == "driveid"


@pytest.mark.parametrize("status", [404, 410])
def test_a_dead_session_raises_so_the_worker_starts_a_fresh_one(status):
    http = FakeHttp(FakeResponse(status))
    with pytest.raises(SessionExpired):
        client_with(http).received_bytes(SESSION, TOTAL)
