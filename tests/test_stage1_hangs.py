# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Stage 1 (anti-hang) regression tests:
- RPC gate timeout: a hung RPC times out and the gate permit is NOT leaked; a
  subsequent call still succeeds.
- Background worker: a download that raises Exception / FloodWait does not kill the
  worker, and task_done stays balanced so queue.join() completes.
- Gate cancellation during the spacing wait does not lose the permit.
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

import tg_throttle
import tg_cache
from pyrogram import errors


# --------------------------------------------------------------------------- #
# 1.1 — RPC gate timeout releases the permit; second call succeeds.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_gate_timeout_releases_permit(monkeypatch):
    # Short timeout so the hung RPC fails fast, and no min-interval spacing to slow the test.
    monkeypatch.setitem(tg_cache.Config, "tg_rpc_timeout", 0.1)
    monkeypatch.setattr(tg_throttle, "_MIN_INTERVAL", 0.0)
    # Bypass the on-disk chat cache so we always hit the live RPC path.
    monkeypatch.setattr(tg_cache, "_get_chat_from_cache", lambda *a, **k: None)
    monkeypatch.setattr(tg_cache, "_save_chat_to_cache", lambda *a, **k: None)

    permits_before = tg_throttle._sem._value
    never = asyncio.Event()  # never set -> RPC hangs forever

    class HungClient:
        async def get_chat(self, channel_id):
            await never.wait()

    # First call: the RPC hangs and must time out (not hang the whole app).
    with pytest.raises(asyncio.TimeoutError):
        await tg_cache.cached_get_chat(HungClient(), "hung_channel")

    # The permit was released on timeout (gate fully available again) — no leak.
    assert tg_throttle._sem._value == permits_before

    # A second call still goes through the gate and succeeds.
    class OkClient:
        async def get_chat(self, channel_id):
            return SimpleNamespace(id=42, title="ok", username="okchan")

    res = await tg_cache.cached_get_chat(OkClient(), "ok_channel")
    assert res.id == 42
    # Permit released again after the successful call.
    assert tg_throttle._sem._value == permits_before


# --------------------------------------------------------------------------- #
# 1.4 — Gate cancellation during the spacing wait does not lose the permit.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_gate_cancel_during_spacing_releases_permit(monkeypatch):
    # Force a spacing wait long enough to cancel inside it.
    monkeypatch.setattr(tg_throttle, "_MIN_INTERVAL", 1.0)
    monkeypatch.setattr(tg_throttle, "_last_start", time.monotonic())

    permits_before = tg_throttle._sem._value

    async def enter_gate():
        async with tg_throttle.tg_rpc():
            pass

    task = asyncio.create_task(enter_gate())
    # Let it acquire the semaphore and start sleeping for the spacing interval.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Cancelled mid-spacing: the acquired permit must be returned.
    assert tg_throttle._sem._value == permits_before


# --------------------------------------------------------------------------- #
# 1.3 — Background worker survives download errors; task_done stays balanced.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_worker_survives_errors_and_balances_task_done(monkeypatch):
    import api_server

    # Speed up the worker's post-download / flood-wait sleeps.
    async def _fast_sleep(*_a, **_k):
        return
    monkeypatch.setattr(api_server.asyncio, "sleep", _fast_sleep)

    # Fresh queue so we don't interfere with (or depend on) module state.
    q = asyncio.Queue(maxsize=100)
    monkeypatch.setattr(api_server, "download_queue", q)

    processed = []

    async def fake_download(channel, post_id, file_unique_id):
        processed.append(file_unique_id)
        if file_unique_id == "boom":
            raise RuntimeError("simulated download failure")
        if file_unique_id == "flood":
            raise errors.FloodWait(value=1)
        # "ok" succeeds

    monkeypatch.setattr(api_server, "download_media_file", fake_download)

    worker = asyncio.create_task(api_server.background_download_worker())
    try:
        for fid in ("boom", "flood", "ok"):
            q.put_nowait(("chan", 1, fid))

        # If the worker died on the first error, or task_done() were unbalanced,
        # join() would never complete and this wait_for would time out.
        await asyncio.wait_for(q.join(), timeout=5)

        # The worker stayed alive across the Exception and the FloodWait and drained all items.
        assert processed == ["boom", "flood", "ok"]
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


# --------------------------------------------------------------------------- #
# 1.4 — _supervised: restart on crash (rate-limited), restart on unexpected
# return, and clean cancellation propagation on shutdown.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_supervised_restarts_crashing_task_rate_limited():
    import api_server

    starts = []

    async def crashing():
        starts.append(time.monotonic())
        raise RuntimeError("always dies")

    # Tiny min interval so the test is fast, but non-zero so we can assert spacing.
    sup = asyncio.create_task(
        api_server._supervised(crashing, "crasher", min_restart_interval=0.05)
    )
    # Let it crash-and-restart a few times.
    await asyncio.sleep(0.28)
    sup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sup

    # It restarted several times (did not give up after the first crash)...
    assert len(starts) >= 3
    # ...but restarts were spaced at least ~min_restart_interval apart (no spin).
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= 0.045 for g in gaps), gaps


@pytest.mark.asyncio
async def test_supervised_restarts_on_unexpected_return():
    import api_server

    runs = []

    async def returns_immediately():
        runs.append(1)
        # A supervised background loop is not meant to return; _supervised must
        # log CRITICAL and restart it rather than stop supervising.
        return

    sup = asyncio.create_task(
        api_server._supervised(returns_immediately, "returner", min_restart_interval=0.02)
    )
    await asyncio.sleep(0.1)
    sup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sup

    assert len(runs) >= 2  # restarted after the unexpected return


@pytest.mark.asyncio
async def test_supervised_cancellation_propagates_to_child():
    import api_server

    child_cancelled = asyncio.Event()

    async def long_running():
        try:
            await asyncio.Event().wait()  # runs until cancelled
        except asyncio.CancelledError:
            child_cancelled.set()
            raise

    sup = asyncio.create_task(
        api_server._supervised(long_running, "longrun")
    )
    await asyncio.sleep(0.05)  # let the child start
    sup.cancel()
    # Cancelling the supervisor must propagate CancelledError out (clean shutdown)...
    with pytest.raises(asyncio.CancelledError):
        await sup
    # ...and the child task must have been cancelled too (no leaked background task).
    assert child_cancelled.is_set()
