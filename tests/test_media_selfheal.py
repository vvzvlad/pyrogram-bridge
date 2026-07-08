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
