# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Regression tests for issue #50 — permanent vs transient media-download error classification.

A PERMANENT failure (deleted post / genuinely-gone media -> 404) must be negative-cached as
'permanent' so a later request in the backoff window returns a clean 404 (no Retry-After) and
the reader stops retrying. A TRANSIENT failure (FloodWait, RPC 5xx, timeout, zero-size) must
NOT be turned into a permanent 404: a FloodWait must not arm the per-file backoff at all, and
an RPC 5xx must surface as 503 + Retry-After, never blacken a live file forever.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pyrogram import errors

import api_server


# --------------------------------------------------------------------------- #
# _download_failures structure carries a kind
# --------------------------------------------------------------------------- #
def test_record_failure_stores_kind_and_defaults_transient():
    key = ("i50chan", 1, "fid_kind")
    api_server._download_failures.pop(key, None)
    # Default kind is transient (background_download_worker path relies on this default).
    api_server._record_download_failure(key)
    assert api_server._download_failure_kind(key) == "transient"
    # An explicit permanent classification is recorded and readable.
    api_server._record_download_failure(key, "permanent")
    assert api_server._download_failure_kind(key) == "permanent"
    # Freshest failure wins: a later transient failure overwrites the kind.
    api_server._record_download_failure(key, "transient")
    assert api_server._download_failure_kind(key) == "transient"
    # Unknown key -> no kind.
    api_server._clear_download_failure(key)
    assert api_server._download_failure_kind(key) is None


# --------------------------------------------------------------------------- #
# _runner (via _download_deduped): classify permanent vs transient; FloodWait never arms.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_deduped_404_records_permanent(monkeypatch):
    key = ("i50chan", 10, "fid_gone")
    api_server._download_failures.pop(key, None)
    api_server._inflight.pop(key, None)

    async def fake_dl(channel, post_id, fid):
        raise HTTPException(status_code=404, detail="Post not found or deleted")

    monkeypatch.setattr(api_server, "download_media_file", fake_dl)

    with pytest.raises(HTTPException) as ei:
        await api_server._download_deduped("i50chan", 10, "fid_gone", asyncio.Semaphore(1))
    assert ei.value.status_code == 404
    # A 404 download failure is negative-cached as PERMANENT.
    assert api_server._download_failure_kind(key) == "permanent"
    api_server._clear_download_failure(key)


@pytest.mark.asyncio
async def test_deduped_timeout_records_transient(monkeypatch):
    key = ("i50chan", 11, "fid_slow")
    api_server._download_failures.pop(key, None)
    api_server._inflight.pop(key, None)

    async def fake_dl(channel, post_id, fid):
        raise HTTPException(status_code=504, detail="Download timeout")

    monkeypatch.setattr(api_server, "download_media_file", fake_dl)

    with pytest.raises(HTTPException) as ei:
        await api_server._download_deduped("i50chan", 11, "fid_slow", asyncio.Semaphore(1))
    assert ei.value.status_code == 504
    # A non-404 failure (504 timeout) is negative-cached as TRANSIENT.
    assert api_server._download_failure_kind(key) == "transient"
    api_server._clear_download_failure(key)


@pytest.mark.asyncio
async def test_deduped_zero_size_records_transient(monkeypatch):
    key = ("i50chan", 12, "fid_zero")
    api_server._download_failures.pop(key, None)
    api_server._inflight.pop(key, None)

    async def fake_dl(channel, post_id, fid):
        raise api_server.ZeroSizeFileError("zero size")

    monkeypatch.setattr(api_server, "download_media_file", fake_dl)

    with pytest.raises(api_server.ZeroSizeFileError):
        await api_server._download_deduped("i50chan", 12, "fid_zero", asyncio.Semaphore(1))
    assert api_server._download_failure_kind(key) == "transient"
    api_server._clear_download_failure(key)


@pytest.mark.asyncio
async def test_deduped_floodwait_does_not_arm_backoff(monkeypatch):
    """A FloodWait during download is a GLOBAL throttle, not a per-file fault: it must NOT
    arm the per-file negative cache (parity with background_download_worker). The next request
    for the same file must therefore be allowed straight into a download attempt."""
    key = ("i50chan", 13, "fid_flood")
    api_server._download_failures.pop(key, None)
    api_server._inflight.pop(key, None)

    async def fake_dl(channel, post_id, fid):
        raise errors.FloodWait(value=42)

    monkeypatch.setattr(api_server, "download_media_file", fake_dl)

    with pytest.raises(errors.FloodWait):
        await api_server._download_deduped("i50chan", 13, "fid_flood", asyncio.Semaphore(1))
    # No backoff armed -> the file is not poisoned and the next request goes to download.
    assert key not in api_server._download_failures
    assert api_server._download_backoff_remaining(key) == 0.0


# --------------------------------------------------------------------------- #
# get_media backoff-hit response: permanent -> clean 404; transient -> 503 + Retry-After.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_media_permanent_backoff_returns_404_no_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)

    key = ("perm_chan", 5, "fid_del")
    api_server._download_failures.pop(key, None)
    api_server._record_download_failure(key, "permanent")

    req = SimpleNamespace(headers={})
    with pytest.raises(HTTPException) as ei:
        await api_server.get_media("perm_chan", 5, "fid_del", request=req, digest="x")
    # Permanent failure in the backoff window -> a clean 404 so the reader stops retrying.
    assert ei.value.status_code == 404
    api_server._clear_download_failure(key)


@pytest.mark.asyncio
async def test_get_media_transient_backoff_returns_503_with_retry_after(monkeypatch, tmp_path):
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)

    key = ("tran_chan", 6, "fid_slow")
    api_server._download_failures.pop(key, None)
    api_server._record_download_failure(key, "transient")

    req = SimpleNamespace(headers={})
    resp = await api_server.get_media("tran_chan", 6, "fid_slow", request=req, digest="x")
    # Transient failure -> retryable 503 with a Retry-After (the reader keeps retrying).
    assert resp.status_code == 503
    assert "Retry-After" in resp.headers
    api_server._clear_download_failure(key)


# --------------------------------------------------------------------------- #
# get_media RPCError mapping: CODE==400 -> 404 (permanent); else -> 503 + Retry-After: 60.
# FloodWait (a RPCError subclass) must be caught BEFORE the RPCError branch (429, not 404/503).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_media_rpcerror_400_maps_to_404(monkeypatch, tmp_path):
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)
    api_server._download_failures.pop(("rpc_chan", 1, "fid"), None)

    async def raise_400(*a, **k):
        raise errors.MsgIdInvalid()  # CODE == 400

    monkeypatch.setattr(api_server, "_download_deduped", raise_400)

    req = SimpleNamespace(headers={})
    with pytest.raises(HTTPException) as ei:
        await api_server.get_media("rpc_chan", 1, "fid", request=req, digest="x")
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_get_media_rpcerror_500_maps_to_503_retry_after_60(monkeypatch, tmp_path):
    """A 5xx-class RPC error is Telegram-side and transient: it must return 503 + Retry-After,
    NOT a 404 that blackens a live file forever."""
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)

    async def raise_500(*a, **k):
        raise errors.InternalServerError()  # CODE == 500

    monkeypatch.setattr(api_server, "_download_deduped", raise_500)

    req = SimpleNamespace(headers={})
    resp = await api_server.get_media("rpc5_chan", 2, "fid", request=req, digest="x")
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "60"


@pytest.mark.asyncio
async def test_get_media_floodwait_precedes_rpcerror(monkeypatch, tmp_path):
    """Regression: FloodWait subclasses RPCError; its except MUST precede the RPCError branch,
    so a throttle returns a retryable 429 — never falling through to the 404/503 RPCError map."""
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)

    async def raise_flood(*a, **k):
        raise errors.FloodWait(value=7)

    monkeypatch.setattr(api_server, "_download_deduped", raise_flood)

    req = SimpleNamespace(headers={})
    resp = await api_server.get_media("flood_chan", 3, "fid", request=req, digest="x")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


# --------------------------------------------------------------------------- #
# download_media_file: the "deleted / empty post" branch removes the SQLite row.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_deleted_post_removes_db_row(monkeypatch, tmp_path):
    """A deleted/empty post (404) must drop its media row so the 60s sweep stops re-fetching it
    forever — mirroring the fid-not-found branch."""
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(api_server, "DB_PATH", str(tmp_path / "media.db"))

    async def fake_get_messages(channel_id, post_id):
        return None  # post deleted

    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get_messages)

    removed = []

    def fake_remove(db_path, entries):
        removed.extend(entries)

    monkeypatch.setattr(api_server, "remove_media_file_ids_sync", fake_remove)

    with pytest.raises(HTTPException) as ei:
        await api_server.download_media_file("DelChan", 77, "fid_del")
    assert ei.value.status_code == 404
    # The row was removed, keyed in canonical (str(channel), post_id, fid) form.
    assert removed == [("DelChan", 77, "fid_del")]


@pytest.mark.asyncio
async def test_deduped_rpcerror_400_records_permanent(monkeypatch):
    # Review round: a raw RPCError with CODE==400 (MsgIdInvalid/ChannelInvalid/PeerIdInvalid)
    # is a genuinely-gone resource. get_media maps RPC-400 -> 404, so _runner MUST cache it
    # 'permanent' too; caching it 'transient' would flap 404<->503 (the exact bug #50 kills).
    from pyrogram import errors

    class _Fake400(errors.RPCError):
        CODE = 400
        ID = "FAKE_400"
        NAME = "Fake bad request"

        def __init__(self):
            Exception.__init__(self, "fake rpc 400")

    key = ("i50chan", 20, "fid_rpc400")
    api_server._download_failures.pop(key, None)
    api_server._inflight.pop(key, None)

    async def fake_dl(channel, post_id, fid):
        raise _Fake400()

    monkeypatch.setattr(api_server, "download_media_file", fake_dl)

    with pytest.raises(errors.RPCError):
        await api_server._download_deduped("i50chan", 20, "fid_rpc400", asyncio.Semaphore(1))
    # Must be PERMANENT so get_media's 404 and the negative cache stay consistent (no flap).
    assert api_server._download_failure_kind(key) == "permanent"
    api_server._clear_download_failure(key)
