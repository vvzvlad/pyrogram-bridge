# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Stage 3 (FileResponse + HTTP-layer cleanup) regression tests.

Covers:
- The HTTP Range matrix asserted against the FINAL (Starlette FileResponse) behavior,
  each case documented, including the RFC-7233-permitted deltas vs the old hand-rolled
  streaming (garbage header -> 400 not 416; multi-range -> proper multipart 206 not 416;
  416 Content-Range now `*/size`; ETag/Last-Modified now present; ASCII filename no longer
  gets a redundant filename*= form).
- Stage-2 behavior preserved through the rewrite: a served temp_* file still has its mtime
  refreshed; delete_after still schedules the temp-file BackgroundTask (and it actually
  runs); the media_key MIME cache is still consulted and populated.
- The pure-ASGI RequestLoggingMiddleware: a normal request still returns (body intact, not
  buffered/truncated) and is logged.

Before/after Range matrix (2048-byte file), old = hand-rolled streaming, new = FileResponse:

  request                | old                         | new (FileResponse)
  -----------------------+-----------------------------+-----------------------------------
  no Range               | 200 full                    | 200 full (+ETag/Last-Modified)
  bytes=0-499            | 206 bytes 0-499/2048        | 206 bytes 0-499/2048 (same bytes)
  bytes=500-             | 206 bytes 500-2047/2048     | 206 bytes 500-2047/2048 (same)
  bytes=-500             | 206 bytes 1548-2047/2048    | 206 bytes 1548-2047/2048 (same)
  bytes=999999- (>EOF)   | 416 `bytes */2048`          | 416 `*/2048`  (Starlette omits the
                         |                             |   `bytes ` prefix; RFC-acceptable)
  garbage header         | 416                         | 400 Bad Request (RFC 7233 lets a
                         |                             |   server reject/ignore a malformed
                         |                             |   Range; Starlette returns 400)
  bytes=0-9,20-29        | 416 (old parser bug)        | 206 multipart/byteranges (correct)

All 206 responses that carry data return byte-for-byte identical slices old vs new, so no
real regression is hidden behind an "accepted difference".
"""
import os
import sys
import time
import logging

import pytest

# Add project root to sys.path and mock the config module (same pattern as the other tests).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.modules['config'] = __import__('tests.mock_config', fromlist=['get_settings'])

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import api_server


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
BODY = bytes(range(256)) * 8  # 2048 deterministic bytes
SIZE = len(BODY)


def _make_client(file_path, delete_after=False, media_key=None):
    """Mount prepare_file_response on a tiny app so it is driven through real ASGI
    (FileResponse computes Range/206/416 at send time, so it must be exercised via a client)."""
    app = FastAPI()

    @app.get("/f")
    async def _serve(request: Request):
        return await api_server.prepare_file_response(
            file_path, request=request, delete_after=delete_after, media_key=media_key
        )

    return TestClient(app)


@pytest.fixture
def sample_file(tmp_path):
    fp = tmp_path / "myfile.bin"
    fp.write_bytes(BODY)
    return str(fp)


# --------------------------------------------------------------------------- #
# 3.1 — Range matrix against the FINAL FileResponse behavior.
# --------------------------------------------------------------------------- #
def test_no_range_returns_full_200(sample_file):
    c = _make_client(sample_file)
    r = c.get("/f")
    assert r.status_code == 200
    assert r.content == BODY
    assert r.headers["content-length"] == str(SIZE)
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["cache-control"] == "public, max-age=86400, immutable"
    # Content-Disposition: inline, filename set by FileResponse itself (no manual double-set).
    assert r.headers["content-disposition"].startswith("inline")
    assert 'filename="myfile.bin"' in r.headers["content-disposition"]
    # NEW vs old: FileResponse adds validators. Old streaming had neither.
    assert r.headers.get("etag")
    assert r.headers.get("last-modified")


def test_range_prefix_0_499(sample_file):
    c = _make_client(sample_file)
    r = c.get("/f", headers={"Range": "bytes=0-499"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-499/{SIZE}"
    assert r.headers["content-length"] == "500"
    assert r.content == BODY[:500]  # exact slice, identical to old behavior


def test_range_open_ended_500_to_eof(sample_file):
    c = _make_client(sample_file)
    r = c.get("/f", headers={"Range": "bytes=500-"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 500-{SIZE - 1}/{SIZE}"
    assert r.headers["content-length"] == str(SIZE - 500)
    assert r.content == BODY[500:]


def test_range_suffix_last_500(sample_file):
    c = _make_client(sample_file)
    r = c.get("/f", headers={"Range": "bytes=-500"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes {SIZE - 500}-{SIZE - 1}/{SIZE}"
    assert r.headers["content-length"] == "500"
    assert r.content == BODY[-500:]


def test_range_start_past_eof_returns_416(sample_file):
    c = _make_client(sample_file)
    r = c.get("/f", headers={"Range": "bytes=999999-"})
    assert r.status_code == 416
    # DELTA (RFC-acceptable): Starlette's 416 Content-Range is `*/size` (it omits the
    # `bytes ` unit prefix the old code emitted). Same information, permitted by RFC 7233.
    assert r.headers["content-range"] == f"*/{SIZE}"


def test_garbage_range_header_returns_400(sample_file):
    c = _make_client(sample_file)
    r = c.get("/f", headers={"Range": "somethinggarbage"})
    # DELTA (RFC-acceptable): the old hand-rolled parser returned 416 on an unparseable
    # header; Starlette rejects a malformed Range with 400 Bad Request. RFC 7233 permits a
    # server to reject/ignore a bad Range; this is a conscious accepted difference, not a
    # data regression (no bytes are served either way).
    assert r.status_code == 400


def test_multi_range_returns_multipart_206(sample_file):
    c = _make_client(sample_file)
    r = c.get("/f", headers={"Range": "bytes=0-9,20-29"})
    # DELTA (improvement): the old parser mis-split multi-ranges and returned 416.
    # FileResponse serves a proper multipart/byteranges 206 containing both slices.
    # Starlette 0.45.3 quirk: it advertises the multipart boundary via the `content-range`
    # header (rather than content-type, which stays the file's own media_type); the body is
    # a real multipart/byteranges document. We assert on the boundary marker + both slices.
    assert r.status_code == 206
    assert r.headers["content-range"].startswith("multipart/byteranges")
    body = r.content
    assert BODY[0:10] in body
    assert BODY[20:30] in body


# --------------------------------------------------------------------------- #
# 3.1 (stage-2 preserved) — temp_* mtime touch survives the FileResponse swap.
# --------------------------------------------------------------------------- #
def test_temp_file_mtime_refreshed_when_stale(tmp_path):
    # A temp_* file whose mtime is OLDER than the refresh interval gets touched, so the
    # 1h sweeper won't delete an actively-viewed video.
    temp_file = tmp_path / "temp_bigvid"
    temp_file.write_bytes(b"videodata")
    stale = time.time() - 10000  # >> TEMP_MTIME_REFRESH_INTERVAL (300s)
    os.utime(temp_file, (stale, stale))

    c = _make_client(str(temp_file))
    r = c.get("/f")
    assert r.status_code == 200
    assert r.content == b"videodata"
    assert os.path.getmtime(temp_file) > time.time() - 100  # refreshed by the serve


def test_temp_file_mtime_stable_when_fresh(tmp_path):
    # DEBOUNCE: a temp_* file touched RECENTLY (within the refresh interval) is NOT
    # re-touched — so FileResponse's mtime-derived ETag stays stable across the rapid
    # requests of a single resume/seek session and `If-Range` resume keeps working.
    temp_file = tmp_path / "temp_freshvid"
    temp_file.write_bytes(b"videodata")
    recent = time.time() - 30  # << TEMP_MTIME_REFRESH_INTERVAL (300s)
    os.utime(temp_file, (recent, recent))
    mtime_before = os.path.getmtime(temp_file)

    c = _make_client(str(temp_file))
    r1 = c.get("/f")
    r2 = c.get("/f")
    assert r1.status_code == 200 and r2.status_code == 200
    # mtime unchanged -> the ETag is identical across the two serves.
    assert os.path.getmtime(temp_file) == mtime_before
    assert r1.headers.get("etag") == r2.headers.get("etag")


def test_non_temp_file_mtime_untouched(tmp_path):
    normal = tmp_path / "regularfile"
    normal.write_bytes(b"data")
    stale = time.time() - 10000
    os.utime(normal, (stale, stale))

    c = _make_client(str(normal))
    c.get("/f")
    assert os.path.getmtime(normal) < time.time() - 5000  # left alone


# --------------------------------------------------------------------------- #
# 3.1 (stage-2 preserved) — delete_after still schedules the temp-file BackgroundTask.
# --------------------------------------------------------------------------- #
async def test_delete_after_attaches_background_task(tmp_path):
    fp = tmp_path / "temp_todelete"
    fp.write_bytes(b"x")

    class _Req:
        headers = {}

    resp = await api_server.prepare_file_response(str(fp), request=_Req(), delete_after=True)
    # FileResponse must carry a non-None background that deletes exactly this file.
    assert resp.background is not None
    assert resp.background.func is api_server.delayed_delete_file
    assert resp.background.args == (str(fp),)


async def test_no_delete_after_has_no_background(tmp_path):
    fp = tmp_path / "keepme"
    fp.write_bytes(b"x")

    class _Req:
        headers = {}

    resp = await api_server.prepare_file_response(str(fp), request=_Req(), delete_after=False)
    assert resp.background is None


def test_delete_after_background_runs_and_removes_file(tmp_path, monkeypatch):
    fp = tmp_path / "temp_gone"
    fp.write_bytes(BODY)

    # Patch the module-level deleter to remove immediately (real one sleeps 300s); the
    # BackgroundTask picks up this reference at prepare_file_response call time.
    async def _delete_now(path, delay=300):
        os.remove(path)

    monkeypatch.setattr(api_server, "delayed_delete_file", _delete_now)

    c = _make_client(str(fp), delete_after=True)
    r = c.get("/f")
    assert r.status_code == 200
    assert r.content == BODY
    # TestClient blocks until the background task has run.
    assert not os.path.exists(fp)


# --------------------------------------------------------------------------- #
# 3.1 (stage-2 preserved) — media_key MIME cache is consulted and populated.
# --------------------------------------------------------------------------- #
def test_media_key_mime_cache_hit_used(sample_file, monkeypatch):
    calls = {"get": 0, "set": 0, "magic": 0}

    def fake_get(db, ch, pid, fid):
        calls["get"] += 1
        return "video/mp4"  # cache HIT

    def fake_set(*a, **k):
        calls["set"] += 1

    def fake_magic(_path):
        calls["magic"] += 1
        return "application/octet-stream"

    monkeypatch.setattr(api_server, "get_mime_type_sync", fake_get)
    monkeypatch.setattr(api_server, "set_mime_type_sync", fake_set)
    monkeypatch.setattr(api_server.magic_mime, "from_file", fake_magic)

    c = _make_client(sample_file, media_key=("chan", 42, "fid"))
    r = c.get("/f")
    assert r.status_code == 200
    # Cache hit: python-magic never invoked, nothing re-written to the cache, MIME applied.
    assert calls["get"] == 1
    assert calls["magic"] == 0
    assert calls["set"] == 0
    assert r.headers["content-type"].startswith("video/mp4")


def test_media_key_mime_cache_miss_populates(sample_file, monkeypatch):
    written = {}

    def fake_get(db, ch, pid, fid):
        return None  # cache MISS

    def fake_set(db, ch, pid, fid, mime):
        written["mime"] = mime

    def fake_magic(_path):
        return "image/png"

    monkeypatch.setattr(api_server, "get_mime_type_sync", fake_get)
    monkeypatch.setattr(api_server, "set_mime_type_sync", fake_set)
    monkeypatch.setattr(api_server.magic_mime, "from_file", fake_magic)

    c = _make_client(sample_file, media_key=("chan", 42, "fid"))
    r = c.get("/f")
    assert r.status_code == 200
    # Miss -> python-magic result is both applied and persisted for next time.
    assert written.get("mime") == "image/png"
    assert r.headers["content-type"].startswith("image/png")


# --------------------------------------------------------------------------- #
# 3.1 — 404 pre-check kept.
# --------------------------------------------------------------------------- #
def test_missing_file_returns_404(tmp_path):
    c = _make_client(str(tmp_path / "does_not_exist"))
    r = c.get("/f")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# 3.2 — pure-ASGI RequestLoggingMiddleware: request returns intact and is logged.
# --------------------------------------------------------------------------- #
def test_asgi_logging_middleware_passes_body_and_logs(tmp_path, caplog):
    fp = tmp_path / "streamed.bin"
    fp.write_bytes(BODY)

    app = FastAPI()
    app.add_middleware(api_server.RequestLoggingMiddleware)

    @app.get("/f")
    async def _serve(request: Request):
        return await api_server.prepare_file_response(str(fp), request=request)

    client = TestClient(app)
    with caplog.at_level(logging.DEBUG, logger="api_server"):
        r = client.get("/f")

    # Body flows straight through the middleware — not buffered/truncated.
    assert r.status_code == 200
    assert r.content == BODY
    messages = " ".join(rec.getMessage() for rec in caplog.records)
    assert "Request: GET /f" in messages
    assert "Response status: 200" in messages


def test_asgi_logging_middleware_range_still_works(tmp_path):
    fp = tmp_path / "streamed.bin"
    fp.write_bytes(BODY)

    app = FastAPI()
    app.add_middleware(api_server.RequestLoggingMiddleware)

    @app.get("/f")
    async def _serve(request: Request):
        return await api_server.prepare_file_response(str(fp), request=request)

    client = TestClient(app)
    r = client.get("/f", headers={"Range": "bytes=0-99"})
    # 206 streaming still works through the pure-ASGI middleware (send not buffered).
    assert r.status_code == 206
    assert r.content == BODY[:100]


async def test_asgi_logging_middleware_passes_non_http_scope_through(caplog):
    # The `scope["type"] != "http"` branch (lifespan/websocket) only runs in a real
    # deploy, never through TestClient's plain request path — so pin it directly:
    # a non-http scope must delegate to the inner app untouched and log nothing.
    called = {}

    async def inner(scope, receive, send):
        called["scope_type"] = scope["type"]

    mw = api_server.RequestLoggingMiddleware(inner)
    with caplog.at_level(logging.DEBUG, logger="api_server"):
        await mw({"type": "lifespan"}, None, None)

    assert called["scope_type"] == "lifespan"  # delegated to the inner app
    # Nothing request/response-ish was logged for a non-http scope.
    messages = " ".join(rec.getMessage() for rec in caplog.records)
    assert "Request:" not in messages
    assert "Response status:" not in messages
