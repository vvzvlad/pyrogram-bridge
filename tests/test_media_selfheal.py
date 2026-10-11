# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Media self-healing.

Kurigram serializes downloads through a single get_file slot; a zombie media-DC connection
makes every download time out while the main-DC watchdog stays green. Covered: (a) the
negative-cache backoff so a repeatedly-failing file is not hammered, and (b) the
consecutive-timeout streak that exits the process, so the container restart rebuilds the
media connection.

Also covers the main-DC side of the same recovery: the watchdog exits the process after
N failed probes or when its loop crashes, while disconnect events are only logged.
"""
import asyncio
import logging

import pytest

import api_server
import telegram_client
from telegram_client import TelegramClient


class _ProcessExited(BaseException):
    """Raised by the fake _exit_process in place of os._exit. Not an Exception, so, like the
    real exit, it cannot be swallowed by the code's `except Exception` handlers."""


def _patch_exit_process(monkeypatch):
    """Replace TelegramClient._exit_process: the real os._exit(1) would kill pytest without a
    report. Returns the list of reasons it was called with."""
    reasons = []

    def fake_exit_process(self, reason):
        reasons.append(reason)
        raise _ProcessExited(reason)

    monkeypatch.setattr(TelegramClient, "_exit_process", fake_exit_process)
    return reasons


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
# Download-timeout streak -> single process exit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_timeout_streak_triggers_single_restart(monkeypatch):
    c = TelegramClient()
    reasons = _patch_exit_process(monkeypatch)
    threshold = telegram_client.MEDIA_TIMEOUT_EXIT_THRESHOLD

    # One short of the threshold: no exit yet.
    for _ in range(threshold - 1):
        c.note_download_timeout()
    assert reasons == []
    assert c._download_timeout_streak == threshold - 1

    # The threshold-th timeout resets the streak and exits exactly once.
    with pytest.raises(_ProcessExited):
        c.note_download_timeout()
    assert c._download_timeout_streak == 0
    assert len(reasons) == 1
    assert reasons[0].startswith("media download:")


@pytest.mark.asyncio
async def test_success_resets_streak():
    c = TelegramClient()
    c.note_download_timeout()
    c.note_download_timeout()
    assert c._download_timeout_streak == 2
    c.note_download_ok()
    assert c._download_timeout_streak == 0


@pytest.mark.asyncio
async def test_no_exit_while_shutting_down(monkeypatch):
    c = TelegramClient()
    reasons = _patch_exit_process(monkeypatch)
    c._shutting_down = True  # an intentional shutdown is underway

    for _ in range(telegram_client.MEDIA_TIMEOUT_EXIT_THRESHOLD):
        c.note_download_timeout()

    # Streak reached the threshold but the process does not exit during a shutdown.
    assert reasons == []


# --------------------------------------------------------------------------- #
# Integration: a hung download times out and frees its transmission slot
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


# --------------------------------------------------------------------------- #
# Backoff is capped at _DOWNLOAD_BACKOFF_MAX (min() cap at api_server.py:163)
# --------------------------------------------------------------------------- #
def test_backoff_capped_at_max():
    """The exponential backoff grows as BASE * 2**(fails-1), but must never exceed
    _DOWNLOAD_BACKOFF_MAX. After enough consecutive failures for the raw exponential to
    overtake the cap, the effective backoff must stay pinned at the cap. This test FAILS if
    the min(MAX, ...) cap is removed (the raw exponential would then blow past MAX). The
    failure count is derived from the configured cap so it holds regardless of
    MEDIA_BACKOFF_MAX_S."""
    key = ("selfheal_chan", 99, "fid_cap")
    api_server._download_failures.pop(key, None)

    # Record well past the point where the raw exponential exceeds the cap so the effective
    # backoff is pinned: pick fails such that BASE*2**(fails-1) is an order of magnitude above
    # MAX. math.log2(MAX/BASE) is where the raw exponential first crosses the cap.
    import math
    n_fail = int(math.log2(api_server._DOWNLOAD_BACKOFF_MAX / api_server._DOWNLOAD_BACKOFF_BASE)) + 4
    for _ in range(n_fail):
        api_server._record_download_failure(key)

    assert api_server._download_failures[key][0] == n_fail  # counter really climbed that high
    remaining = api_server._download_backoff_remaining(key)
    # The cap holds: remaining is bounded by MAX (with a tiny slack for monotonic drift since
    # retry_not_before was stamped). Without the min() cap this would be ~7680s.
    assert remaining <= api_server._DOWNLOAD_BACKOFF_MAX
    assert remaining > api_server._DOWNLOAD_BACKOFF_MAX - 5  # and it IS pinned near the cap

    api_server._clear_download_failure(key)


# --------------------------------------------------------------------------- #
# Main-DC watchdog and disconnect handler -> process exit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_watchdog_exits_after_n_failed_probes(monkeypatch):
    c = TelegramClient()
    reasons = _patch_exit_process(monkeypatch)
    monkeypatch.setattr(telegram_client, "WATCHDOG_INTERVAL_S", 0)
    probes = []

    async def failing_get_me():
        probes.append(True)
        raise ConnectionError("probe failed")

    monkeypatch.setattr(c.client, "get_me", failing_get_me)

    with pytest.raises(_ProcessExited):
        # Bounded: with interval 0 a broken exit would spin this loop forever.
        await asyncio.wait_for(c._watchdog_loop(), timeout=5)

    # The N-th consecutive failed probe exits the process, not an earlier one.
    assert len(probes) == telegram_client.WATCHDOG_FAILURES
    assert reasons == [f"watchdog: {telegram_client.WATCHDOG_FAILURES} consecutive failed probes"]


@pytest.mark.asyncio
async def test_watchdog_loop_crash_exits(monkeypatch):
    c = TelegramClient()
    reasons = _patch_exit_process(monkeypatch)
    monkeypatch.setattr(telegram_client, "WATCHDOG_INTERVAL_S", 0)

    class _BrokenClient:
        """A failed probe whose diagnostics raise: an error outside the per-probe handling."""
        @property
        def is_connected(self):
            raise RuntimeError("boom")

        async def get_me(self):
            raise ConnectionError("probe failed")

    monkeypatch.setattr(c, "client", _BrokenClient())

    with pytest.raises(_ProcessExited):
        # Bounded: with interval 0 a broken exit would spin this loop forever.
        await asyncio.wait_for(c._watchdog_loop(), timeout=5)

    assert reasons == ["watchdog loop crashed: RuntimeError: boom"]


@pytest.mark.asyncio
async def test_watchdog_saves_session_and_save_error_is_not_a_probe_failure(monkeypatch):
    c = TelegramClient()
    reasons = _patch_exit_process(monkeypatch)
    monkeypatch.setattr(telegram_client, "WATCHDOG_INTERVAL_S", 0)
    saves = []
    probes = []

    async def ok_get_me():
        probes.append(True)
        return type("Me", (), {"id": 1})()

    async def failing_save():
        saves.append(True)
        if len(saves) > telegram_client.WATCHDOG_FAILURES:
            c._shutting_down = True  # ends the loop after one more probe than the exit threshold
        raise OSError("disk full")

    monkeypatch.setattr(c.client, "get_me", ok_get_me)
    monkeypatch.setattr(c.client.storage, "save", failing_save)

    await asyncio.wait_for(c._watchdog_loop(), timeout=5)

    # Every successful probe saved the session, and a failed save never counted as a failed probe.
    assert len(saves) == len(probes) == telegram_client.WATCHDOG_FAILURES + 1
    assert c._wd_probe_fail_count == 0
    assert reasons == []


def test_exit_process_exits_with_code_1(monkeypatch, caplog):
    import telegram_client
    codes = []

    def fake_exit(code):
        codes.append(code)
        raise _ProcessExited()

    monkeypatch.setattr(telegram_client.os, "_exit", fake_exit)
    with caplog.at_level(logging.CRITICAL, logger="telegram_client"), pytest.raises(_ProcessExited):
        TelegramClient()._exit_process("why")

    # Code 1: an `on-failure` restart policy restarts the container only on a non-zero code.
    assert codes == [1]
    assert any("reason='why'" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_disconnects_are_only_logged(monkeypatch, caplog):
    c = TelegramClient()
    reasons = _patch_exit_process(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="telegram_client"):
        for _ in range(10):
            await c._on_disconnect(c.client, None)

    lost = [r for r in caplog.records if r.getMessage() == "connection_handler: connection lost"]
    assert len(lost) == 10
    assert reasons == []
