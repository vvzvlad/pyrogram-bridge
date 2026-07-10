# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Regression tests for the media self-healing fix (post 'static refactor' outage):

Root cause recap: Kurigram serializes downloads through a single get_file slot; a
zombie media-DC connection makes every download time out while the main-DC watchdog
stays green, so downloads jam forever. The fix adds (a) a negative-cache backoff so a
repeatedly-failing file is not hammered, and (b) a consecutive-timeout streak that
reuses the existing in-process restart to rebuild the media connection.
"""
import asyncio

import pytest

import api_server
from telegram_client import TelegramClient


# --------------------------------------------------------------------------- #
# Negative-cache backoff helpers
# --------------------------------------------------------------------------- #
def test_backoff_arms_and_clears():
    key = ("selfheal_chan", 1, "fid_a")
    api_server._download_failures.pop(key, None)
    assert api_server._download_backoff_remaining(key) == 0.0  # never failed -> allowed
    api_server._record_download_failure(key)
    assert api_server._download_backoff_remaining(key) > 0.0   # armed -> blocked
    api_server._clear_download_failure(key)
    assert api_server._download_backoff_remaining(key) == 0.0  # recovered -> allowed


def test_backoff_failure_counter_increments():
    key = ("selfheal_chan", 2, "fid_b")
    api_server._download_failures.pop(key, None)
    api_server._record_download_failure(key)
    first_fails = api_server._download_failures[key][0]
    api_server._record_download_failure(key)
    second_fails = api_server._download_failures[key][0]
    assert first_fails == 1
    assert second_fails == 2  # consecutive-failure counter grows -> longer backoff
    api_server._clear_download_failure(key)


# --------------------------------------------------------------------------- #
# Download-timeout streak -> single media-connection restart
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_timeout_streak_triggers_single_restart(monkeypatch):
    c = TelegramClient()
    calls = []

    async def fake_restart(reason: str = "unspecified"):
        calls.append(reason)

    monkeypatch.setattr(c, "_restart_client", fake_restart)
    threshold = c.media_timeout_restart_threshold

    # One short of the threshold: no restart scheduled yet.
    for _ in range(threshold - 1):
        c.note_download_timeout()
    assert c._media_recovery_task is None
    assert c._download_timeout_streak == threshold - 1

    # The threshold-th timeout schedules exactly one restart and resets the streak.
    c.note_download_timeout()
    assert c._download_timeout_streak == 0
    assert c._media_recovery_task is not None
    await c._media_recovery_task
    assert calls == ["media download timeout streak"]


@pytest.mark.asyncio
async def test_success_resets_streak():
    c = TelegramClient()
    c.note_download_timeout()
    c.note_download_timeout()
    assert c._download_timeout_streak == 2
    c.note_download_ok()
    assert c._download_timeout_streak == 0


@pytest.mark.asyncio
async def test_no_restart_while_already_restarting(monkeypatch):
    c = TelegramClient()
    calls = []

    async def fake_restart(reason: str = "unspecified"):
        calls.append(reason)

    monkeypatch.setattr(c, "_restart_client", fake_restart)
    c._restarting = True  # a restart is already underway

    for _ in range(c.media_timeout_restart_threshold):
        c.note_download_timeout()

    # Streak reached the threshold but no new restart is scheduled during a restart.
    assert c._media_recovery_task is None
    assert calls == []


# --------------------------------------------------------------------------- #
# Integration: jam -> auto-recovery -> negative cache cleared (Fix 1)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hung_download_times_out_and_frees_slot(monkeypatch):
    """A hung download must be cancelled by asyncio.wait_for, freeing its transmission slot
    for the next download (and counting toward the restart streak)."""
    c = TelegramClient()

    # A single-permit semaphore stands in for Pyrogram's get_file transmission slot: the mock
    # holds it for the whole (never-completing) download and releases it in the async-with
    # __aexit__, which runs when wait_for cancels the coroutine.
    slot = asyncio.Semaphore(1)
    entered = asyncio.Event()

    async def hung_download(file_id, file_name=None):
        async with slot:
            entered.set()
            await asyncio.sleep(3600)  # never completes within the test's timeout

    monkeypatch.setattr(c.client, "download_media", hung_download)

    with pytest.raises(asyncio.TimeoutError):
        await c.safe_download_media("fid", "/tmp/x", max_retries=1, timeout=0.05)

    # (a) the download actually started and its slot was released by the cancellation, so a
    # fresh acquire for the "next" download succeeds immediately.
    assert entered.is_set()
    await asyncio.wait_for(slot.acquire(), timeout=1.0)
    slot.release()
    # The timeout was counted toward the restart streak.
    assert c._download_timeout_streak == 1


@pytest.mark.asyncio
async def test_streak_escalates_once_and_verified_restart_clears_cache(monkeypatch):
    """Full self-heal chain: a timeout streak reaching the threshold escalates to EXACTLY ONE
    restart, and the verified restart CLEARS the download negative cache (Fix 1)."""
    c = TelegramClient()

    # Pre-arm the negative cache with a persistently-failing file.
    key = ("selfheal_chan", 7, "fid_jam")
    api_server._download_failures.pop(key, None)
    api_server._record_download_failure(key)
    assert api_server._download_backoff_remaining(key) > 0.0

    # Wire the REAL self-heal hook and drive a REAL _restart_client with only the network
    # layer mocked, so the whole chain (streak -> one restart -> verify ok -> cache clear) runs.
    c.set_restart_callback(api_server._clear_all_download_failures)
    monkeypatch.setattr(c, "_start_watchdog", lambda: None)
    monkeypatch.setattr(c.client, "is_connected", True)

    restart_calls = []

    async def fake_client_restart():
        restart_calls.append(True)

    async def fake_get_me():
        return type("Me", (), {"id": 42})()

    monkeypatch.setattr(c.client, "restart", fake_client_restart)
    monkeypatch.setattr(c.client, "get_me", fake_get_me)

    threshold = c.media_timeout_restart_threshold
    for _ in range(threshold - 1):
        c.note_download_timeout()
    assert c._media_recovery_task is None  # one short of threshold: nothing scheduled yet

    c.note_download_timeout()  # threshold-th timeout schedules exactly one restart
    assert c._media_recovery_task is not None
    await c._media_recovery_task

    # (b) escalated to EXACTLY ONE underlying client.restart().
    assert restart_calls == [True]
    # (c) the verified restart cleared the negative cache -> the file loads again immediately.
    assert api_server._download_backoff_remaining(key) == 0.0
    assert len(api_server._download_failures) == 0


@pytest.mark.asyncio
async def test_failed_verify_does_not_clear_cache(monkeypatch):
    """The cache-clear hook must fire ONLY on a verified restart: if verify_get_me fails, the
    backoff must survive (a still-broken media DC should not drop the protective backoff)."""
    c = TelegramClient()
    key = ("selfheal_chan", 8, "fid_still_broken")
    api_server._download_failures.pop(key, None)
    api_server._record_download_failure(key)

    c.set_restart_callback(api_server._clear_all_download_failures)
    monkeypatch.setattr(c, "_start_watchdog", lambda: None)
    monkeypatch.setattr(c.client, "is_connected", True)

    async def fake_client_restart():
        return None

    async def failing_get_me():
        raise RuntimeError("media DC still dead")

    monkeypatch.setattr(c.client, "restart", fake_client_restart)
    monkeypatch.setattr(c.client, "get_me", failing_get_me)

    await c._restart_client(reason="test failed verify")

    # verify_get_me failed -> callback NOT fired -> backoff still armed.
    assert api_server._download_backoff_remaining(key) > 0.0
    api_server._clear_download_failure(key)


# --------------------------------------------------------------------------- #
# Cross-path negative-cache key consistency
# --------------------------------------------------------------------------- #
def test_download_key_consistent_across_paths():
    """The negative-cache key must be byte-identical across every producer/consumer path so a
    failure recorded on one path is seen by the guard on another. Reproduce each path's key
    expression for the same logical file and assert they coincide, then verify end-to-end that
    a worker-recorded failure is visible to the get_media backoff check."""
    channel_from_db = "durov"   # background_download_worker / download_new_files: str(channel)
    post_id_from_db = 123       # ...                                              int(post_id)
    fid = "AgADfid"

    # get_media receives the raw (possibly differently-cased) URL channel, canonicalizes it,
    # and uses the int path param post_id.
    raw_url_channel = "Durov"
    fs_channel = api_server.canonical_channel_key(raw_url_channel)
    get_media_key = (fs_channel, 123, fid)

    worker_key = (str(channel_from_db), int(post_id_from_db), fid)      # background_download_worker
    new_files_key = (str(channel_from_db), int(post_id_from_db), fid)   # download_new_files
    deduped_key = (str(fs_channel), 123, fid)                          # _download_deduped(fs_channel, post_id, fid)

    assert worker_key == new_files_key == get_media_key == deduped_key

    # End-to-end: a failure recorded by the worker path is seen by the get_media guard.
    api_server._download_failures.pop(worker_key, None)
    api_server._record_download_failure(worker_key)
    try:
        assert api_server._download_backoff_remaining(get_media_key) > 0.0
    finally:
        api_server._clear_download_failure(worker_key)


# --------------------------------------------------------------------------- #
# Fix 2: negative cache is LRU-bounded
# --------------------------------------------------------------------------- #
def test_download_failures_lru_bounded(monkeypatch):
    """The negative cache must not grow unbounded (permanently-404 files leak entries).
    Once over the cap the oldest entry is evicted."""
    monkeypatch.setattr(api_server, "_DOWNLOAD_FAILURES_MAX", 3)
    api_server._download_failures.clear()
    try:
        for i in range(5):
            api_server._record_download_failure(("chan", i, "fid"))
        assert len(api_server._download_failures) == 3
        # Oldest two (i=0,1) evicted; newest three retained.
        remaining = list(api_server._download_failures.keys())
        assert remaining == [("chan", 2, "fid"), ("chan", 3, "fid"), ("chan", 4, "fid")]
    finally:
        api_server._download_failures.clear()
