# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Stage 2 (static-media stability) regression tests.

Covers the four scenarios from the plan plus the subtle in-flight-dedup lifecycle:
- _download_atomic: success publishes atomically and leaves no `.part.`; timeout / zero-size
  / race-loser all remove the partial and never leave a stub at the final name.
- Concurrent large-video downloads never expose a partial file (atomic rename).
- _download_deduped: one shared download for concurrent requests; a cancelled waiter
  (client disconnect) can't hang the others; a failed download propagates to all waiters
  and frees the key (no stuck registry entry).
- FloodWait in /media returns a retryable 429 (not a permanent 404) — ordering pinned.
- A served temp_* file gets its mtime refreshed (sweeper won't delete it under a viewer).
- The sweeper removes both `.part.` and legacy `.tmp.` stubs (and stale temp_*) but keeps
  fresh partials and non-temp files.
"""
import os
import sys
import time
import asyncio
from types import SimpleNamespace

import pytest

# Add project root to sys.path and mock the config module (same pattern as the other tests).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.modules['config'] = __import__('tests.mock_config', fromlist=['get_settings'])

import api_server
from pyrogram import errors
from pyrogram.enums import MessageMediaType


def _part_files(dir_path):
    return [p for p in os.listdir(dir_path) if ".part." in p or ".tmp." in p]


# --------------------------------------------------------------------------- #
# 2.1 — _download_atomic: publish-on-success, always clean the partial.
# --------------------------------------------------------------------------- #
async def test_download_atomic_success_publishes_and_cleans(monkeypatch, tmp_path):
    final = str(tmp_path / "final")

    async def fake(file_id, file_name, timeout=None, **kw):
        with open(file_name, "wb") as f:
            f.write(b"hello")
        return file_name

    monkeypatch.setattr(api_server.client, "safe_download_media", fake)

    res = await api_server._download_atomic("fid", final, timeout=10)
    assert res == final
    with open(final, "rb") as f:
        assert f.read() == b"hello"
    assert _part_files(tmp_path) == []  # no leftover partial


async def test_download_atomic_timeout_removes_part_no_final(monkeypatch, tmp_path):
    final = str(tmp_path / "final")

    async def fake(file_id, file_name, timeout=None, **kw):
        # A partial write, then the download times out mid-flight.
        with open(file_name, "wb") as f:
            f.write(b"partial")
        raise asyncio.TimeoutError()

    monkeypatch.setattr(api_server.client, "safe_download_media", fake)

    with pytest.raises(asyncio.TimeoutError):
        await api_server._download_atomic("fid", final, timeout=10)

    assert not os.path.exists(final)       # no stub at the final name
    assert _part_files(tmp_path) == []     # partial cleaned by the finally


async def test_download_atomic_zero_size_raises_and_cleans(monkeypatch, tmp_path):
    final = str(tmp_path / "final")

    async def fake(file_id, file_name, timeout=None, **kw):
        open(file_name, "wb").close()  # zero-size result
        return file_name

    monkeypatch.setattr(api_server.client, "safe_download_media", fake)

    with pytest.raises(api_server.ZeroSizeFileError):
        await api_server._download_atomic("fid", final, timeout=10)

    assert not os.path.exists(final)
    assert _part_files(tmp_path) == []


async def test_download_atomic_race_loser_keeps_existing_final(monkeypatch, tmp_path):
    final = str(tmp_path / "final")
    with open(final, "wb") as f:
        f.write(b"WINNER")  # a concurrent request already published the final file

    async def fake(file_id, file_name, timeout=None, **kw):
        with open(file_name, "wb") as f:
            f.write(b"loser")
        return file_name

    monkeypatch.setattr(api_server.client, "safe_download_media", fake)

    res = await api_server._download_atomic("fid", final, timeout=10)
    assert res == final
    with open(final, "rb") as f:
        assert f.read() == b"WINNER"  # not clobbered by the race loser
    assert _part_files(tmp_path) == []  # loser's partial removed


# --------------------------------------------------------------------------- #
# 2.1 — Concurrent large-video downloads never expose a partial file.
# --------------------------------------------------------------------------- #
async def test_concurrent_large_video_no_partial_served(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # download_media_file writes under ./data/cache

    msg = SimpleNamespace(
        media=MessageMediaType.VIDEO,
        video=SimpleNamespace(file_size=200 * 1024 * 1024),  # > 100 MB -> large-video path
        empty=False,
    )

    async def fake_get(channel_id, post_id):
        return msg

    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get)

    async def fake_find(message, fid):
        return "fileid"

    monkeypatch.setattr(api_server, "find_file_id_in_message", fake_find)

    started = []

    async def fake_dl(file_id, file_name, timeout=None, **kw):
        started.append(file_name)
        await asyncio.sleep(0.1)  # slow download -> real overlap between the two requests
        with open(file_name, "wb") as f:
            f.write(b"VIDEODATA")
        return file_name

    monkeypatch.setattr(api_server.client, "safe_download_media", fake_dl)

    t1 = asyncio.create_task(api_server.download_media_file("chan", 7, "vidfid"))
    t2 = asyncio.create_task(api_server.download_media_file("chan", 7, "vidfid"))
    (p1, _), (p2, _) = await asyncio.gather(t1, t2)

    assert p1 == p2
    assert os.path.basename(p1) == "temp_vidfid"  # large videos keep the TTL-cleaned name
    with open(p1, "rb") as f:
        assert f.read() == b"VIDEODATA"  # a complete file, never a partial

    post_dir = os.path.dirname(p1)
    assert _part_files(post_dir) == []  # both partials resolved, none left on disk
    # Both requests really downloaded to distinct `.part.` paths (a genuine race that the
    # atomic rename resolved), so neither ever saw the other's partial.
    assert len(started) == 2 and started[0] != started[1]


# --------------------------------------------------------------------------- #
# 2.2 — In-flight dedup: one shared download for concurrent requests.
# --------------------------------------------------------------------------- #
async def test_dedup_single_download_for_concurrent_requests(monkeypatch):
    api_server._inflight.clear()
    calls = []

    async def fake_dl(channel, post_id, fid):
        calls.append(fid)
        await asyncio.sleep(0.05)
        return (f"/final/{fid}", False)

    monkeypatch.setattr(api_server, "download_media_file", fake_dl)

    tasks = [asyncio.create_task(api_server._download_deduped("c", 1, "f")) for _ in range(5)]
    results = await asyncio.gather(*tasks)

    assert all(r == ("/final/f", False) for r in results)
    assert calls == ["f"]              # exactly one real download, shared by all 5
    assert not api_server._inflight    # key popped after completion


# --------------------------------------------------------------------------- #
# 2.2 — A cancelled waiter (client disconnect) must not hang the others, and the
# detached download runs to completion regardless.
# --------------------------------------------------------------------------- #
async def test_dedup_waiter_cancel_does_not_hang_others(monkeypatch):
    api_server._inflight.clear()
    gate = asyncio.Event()
    calls = []

    async def fake_dl(channel, post_id, fid):
        calls.append(fid)
        await gate.wait()  # hold the download open until the test releases it
        return (f"/path/{fid}", False)

    monkeypatch.setattr(api_server, "download_media_file", fake_dl)

    t1 = asyncio.create_task(api_server._download_deduped("c", 1, "fidX"))
    await asyncio.sleep(0.05)  # let t1 register the future + start the detached task
    t2 = asyncio.create_task(api_server._download_deduped("c", 1, "fidX"))
    await asyncio.sleep(0.05)

    # First client disconnects -> its request coroutine is cancelled.
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1

    # The detached download is unaffected; complete it and the surviving waiter resolves.
    gate.set()
    res = await asyncio.wait_for(t2, timeout=2)
    assert res == ("/path/fidX", False)
    assert calls == ["fidX"]           # download ran exactly once (shared, not restarted)
    assert not api_server._inflight     # key popped


# --------------------------------------------------------------------------- #
# 2.2 — A failed download propagates to all waiters and frees the key (not stuck).
# --------------------------------------------------------------------------- #
async def test_dedup_exception_propagates_and_frees_key(monkeypatch):
    api_server._inflight.clear()
    state = {"mode": "boom"}

    async def fake_dl(channel, post_id, fid):
        if state["mode"] == "boom":
            raise RuntimeError("kaboom")
        return ("/ok", False)

    monkeypatch.setattr(api_server, "download_media_file", fake_dl)

    t1 = asyncio.create_task(api_server._download_deduped("c", 1, "f"))
    t2 = asyncio.create_task(api_server._download_deduped("c", 1, "f"))
    results = await asyncio.gather(t1, t2, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results), results
    assert not api_server._inflight  # failed future did NOT leave the key stuck

    # A subsequent request with a working download succeeds (proves the key was freed).
    state["mode"] = "ok"
    res = await api_server._download_deduped("c", 1, "f")
    assert res == ("/ok", False)


# --------------------------------------------------------------------------- #
# 2.3 — FloodWait in /media -> 429 with Retry-After (ordering pinned: not a 404).
# --------------------------------------------------------------------------- #
async def test_media_floodwait_returns_429_not_404(monkeypatch, tmp_path):
    api_server._inflight.clear()
    monkeypatch.chdir(tmp_path)  # so the pre-semaphore cache check misses -> download path
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)

    async def fake_dl(channel, post_id, fid):
        raise errors.FloodWait(value=42)

    monkeypatch.setattr(api_server, "download_media_file", fake_dl)

    req = SimpleNamespace(headers={})
    resp = await api_server.get_media("chan", 5, "floodfid", request=req, digest="x")

    assert resp.status_code == 429
    retry_after = resp.headers.get("retry-after")
    assert retry_after is not None
    assert 43 <= int(retry_after) <= 72  # 42 + random(1..30)


# --------------------------------------------------------------------------- #
# 2.5 — HTTP_DOWNLOAD_SEMAPHORE balance: released exactly once on success/error,
# never released when the acquire itself timed out (503). It is a plain
# asyncio.Semaphore, so an over-release would SILENTLY inflate the permit count
# and disable the limiter — assert the count, mirroring the stage-1 gate test.
# --------------------------------------------------------------------------- #
async def test_media_semaphore_released_on_download_error(monkeypatch, tmp_path):
    api_server._inflight.clear()
    monkeypatch.chdir(tmp_path)  # pre-semaphore cache miss -> the acquire path runs
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)

    permits_before = api_server.HTTP_DOWNLOAD_SEMAPHORE._value

    async def boom(channel_id, post_id, fid):
        raise RuntimeError("download blew up")

    monkeypatch.setattr(api_server, "_download_deduped", boom)

    req = SimpleNamespace(headers={})
    # get_media swallows the error into a 4xx/5xx response; the point is the permit.
    try:
        await api_server.get_media("chan", 5, "errfid", request=req, digest="x")
    except Exception:
        pass

    # Permit released exactly once (back to baseline) — not leaked, not double-released.
    assert api_server.HTTP_DOWNLOAD_SEMAPHORE._value == permits_before


async def test_media_semaphore_not_released_on_acquire_timeout(monkeypatch, tmp_path):
    api_server._inflight.clear()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)

    permits_before = api_server.HTTP_DOWNLOAD_SEMAPHORE._value

    # Make the bounded acquire time out: the first wait_for (wrapping the acquire)
    # raises TimeoutError; we close the passed coroutine to avoid a "never awaited"
    # warning. The 503 short-circuits before _download_deduped, so only this one
    # wait_for is hit.
    real_wait_for = asyncio.wait_for

    async def fake_wait_for(coro, timeout=None):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(api_server.asyncio, "wait_for", fake_wait_for)

    req = SimpleNamespace(headers={})
    resp = await api_server.get_media("chan", 5, "busyfid", request=req, digest="x")

    assert resp.status_code == 503
    assert resp.headers.get("retry-after") == "30"
    # A timed-out acquire never held a permit, so nothing must have been released:
    # the count is UNCHANGED (an erroneous release would inflate it above baseline).
    assert api_server.HTTP_DOWNLOAD_SEMAPHORE._value == permits_before

    monkeypatch.setattr(api_server.asyncio, "wait_for", real_wait_for)


async def test_download_atomic_reraises_floodwait_and_cleans_part(monkeypatch, tmp_path):
    # Stage-2 review: prove a FloodWait raised by the downloader propagates OUT of
    # _download_atomic (its finally must NOT swallow it) AND the partial is cleaned.
    # This is the path the wholesale-mocked 429 test above skips.
    final = str(tmp_path / "temp_vid")
    seen_part = {}

    async def fake(file_id, part_path, timeout=120):
        # The partial path exists mid-download, then FloodWait strikes.
        with open(part_path, "wb") as fh:
            fh.write(b"partial")
        seen_part["path"] = part_path
        raise errors.FloodWait(value=7)

    monkeypatch.setattr(api_server.client, "safe_download_media", fake)

    with pytest.raises(errors.FloodWait):
        await api_server._download_atomic("fid", final, timeout=10)

    # The finally cleaned the partial, and no final file was published.
    assert not os.path.exists(seen_part["path"])
    assert not os.path.exists(final)


# --------------------------------------------------------------------------- #
# 2.4 — Serving a temp_* file refreshes its mtime; non-temp files are untouched.
# --------------------------------------------------------------------------- #
async def test_temp_file_mtime_touched_on_serve(tmp_path):
    temp_file = tmp_path / "temp_abc"
    temp_file.write_bytes(b"videodata")
    stale = time.time() - 10000
    os.utime(temp_file, (stale, stale))

    req = SimpleNamespace(headers={})
    resp = await api_server.prepare_file_response(str(temp_file), request=req)
    assert resp is not None
    assert os.path.getmtime(temp_file) > time.time() - 100  # refreshed


async def test_non_temp_file_mtime_not_touched(tmp_path):
    normal = tmp_path / "regularfile"
    normal.write_bytes(b"data")
    stale = time.time() - 10000
    os.utime(normal, (stale, stale))

    req = SimpleNamespace(headers={})
    await api_server.prepare_file_response(str(normal), request=req)
    assert os.path.getmtime(normal) < time.time() - 5000  # left alone


# --------------------------------------------------------------------------- #
# 2.1/2.4 — Sweeper cleans both `.part.` and legacy `.tmp.` stubs and stale temp_*,
# but keeps fresh partials and ordinary cached files.
# --------------------------------------------------------------------------- #
def test_sweeper_cleans_part_and_tmp_not_fresh(tmp_path):
    cache = tmp_path / "cache"
    post = cache / "chan" / "10"
    post.mkdir(parents=True)

    stale = time.time() - 4000  # older than the 1h (3600s) threshold
    hexid = "0" * 32

    old_part = post / f"file.part.{hexid}"; old_part.write_bytes(b"x"); os.utime(old_part, (stale, stale))
    old_tmp = post / f"file.tmp.{hexid}"; old_tmp.write_bytes(b"x"); os.utime(old_tmp, (stale, stale))
    old_tempvid = post / "temp_bigvid"; old_tempvid.write_bytes(b"x"); os.utime(old_tempvid, (stale, stale))
    fresh_part = post / f"file2.part.{hexid}"; fresh_part.write_bytes(b"x")  # fresh mtime
    normal = post / "realfile"; normal.write_bytes(b"x")  # not a temp file at all

    _, removed = api_server.remove_old_cached_files_sync([], str(cache))

    assert not old_part.exists()      # new-suffix stub swept
    assert not old_tmp.exists()       # legacy-suffix stub swept (transition period)
    assert not old_tempvid.exists()   # stale large-video temp swept
    assert fresh_part.exists()        # too new to sweep
    assert normal.exists()            # ordinary cached file untouched
    assert removed >= 3
