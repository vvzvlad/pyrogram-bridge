# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Stage 6 (lightweight /ping healthcheck) regression tests.

Covers:
- /ping returns 200 "ok" when connected and the last probe is recent (age < threshold).
- /ping returns 503 "degraded" when connected but the last probe is stale (age > threshold).
- /ping returns 503 "degraded" when disconnected, regardless of probe age.
- /ping returns 200 "ok" on a fresh boot (age is None) while connected — a freshly-started
  container must NOT be killed before the watchdog's first probe.
- ANTI-REGRESSION (the critical invariant): /ping issues ZERO Telegram RPC. A spy on the
  fake client's get_me / safe_get_messages proves neither is ever called.
- TelegramClient.watchdog_last_ok_age(): None when never probed; a positive float afterwards.
"""
import os
import sys
import time

import pytest

# Add project root to sys.path and mock the config module (same pattern as the other tests).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.modules['config'] = __import__('tests.mock_config', fromlist=['get_settings'])

from fastapi.testclient import TestClient

import api_server
from telegram_client import TelegramClient


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeKurigramClient:
    """Stands in for TelegramClient.client — exposes is_connected and RPC spies."""
    def __init__(self, is_connected=True):
        self.is_connected = is_connected
        self.get_me_calls = 0

    async def get_me(self):
        # If /ping ever touches this, the whole point of the endpoint is defeated.
        self.get_me_calls += 1
        raise AssertionError("/ping must never call get_me()")


class _FakeTelegramClient:
    """Stands in for api_server.client with a controllable probe age + RPC spies."""
    def __init__(self, age, is_connected=True):
        self._age = age
        self.client = _FakeKurigramClient(is_connected=is_connected)
        self.safe_get_messages_calls = 0

    def watchdog_last_ok_age(self):
        return self._age

    async def safe_get_messages(self, *args, **kwargs):
        self.safe_get_messages_calls += 1
        raise AssertionError("/ping must never call safe_get_messages()")


@pytest.fixture
def patch_client(monkeypatch):
    """Return a factory that installs a fake api_server.client and yields a TestClient."""
    def _install(age, is_connected=True):
        fake = _FakeTelegramClient(age=age, is_connected=is_connected)
        monkeypatch.setattr(api_server, "client", fake)
        return fake, TestClient(api_server.app)
    return _install


THRESHOLD = api_server.Config["tg_ping_unhealthy_after"]  # 250 in mock_config


# --------------------------------------------------------------------------- #
# /ping endpoint behavior
# --------------------------------------------------------------------------- #
def test_ping_healthy_connected_recent(patch_client):
    fake, tc = patch_client(age=THRESHOLD - 10, is_connected=True)
    r = tc.get("/ping")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["connected"] is True
    assert body["last_probe_age_s"] == round(THRESHOLD - 10, 1)
    assert body["threshold_s"] == THRESHOLD
    assert fake.client.get_me_calls == 0


def test_ping_degraded_stale_probe(patch_client):
    fake, tc = patch_client(age=THRESHOLD + 100, is_connected=True)
    r = tc.get("/ping")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["connected"] is True
    assert fake.client.get_me_calls == 0


def test_ping_degraded_disconnected(patch_client):
    # Even with a fresh probe age, a disconnected client is unhealthy.
    fake, tc = patch_client(age=1.0, is_connected=False)
    r = tc.get("/ping")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["connected"] is False


def test_ping_degraded_disconnected_even_when_age_none(patch_client):
    fake, tc = patch_client(age=None, is_connected=False)
    r = tc.get("/ping")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


def test_ping_fresh_boot_age_none_is_healthy(patch_client):
    # Right after boot the watchdog hasn't probed yet (age None); connected => healthy.
    fake, tc = patch_client(age=None, is_connected=True)
    r = tc.get("/ping")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["last_probe_age_s"] is None
    assert body["threshold_s"] == THRESHOLD


def test_ping_pre_start_connected_none_is_degraded_bool(patch_client):
    # Before client.start(), Kurigram's is_connected is None. /ping must not 500: it coerces
    # to a bool, so "connected" is false (never null) and the endpoint reports 503 degraded.
    fake, tc = patch_client(age=None, is_connected=None)
    r = tc.get("/ping")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["connected"] is False  # bool, not null
    assert fake.client.get_me_calls == 0


def test_ping_issues_no_tg_rpc(patch_client):
    """The critical invariant: /ping never issues any Telegram RPC in any branch."""
    for age, connected in [(1.0, True), (THRESHOLD + 500, True), (None, True), (1.0, False)]:
        fake, tc = patch_client(age=age, is_connected=connected)
        tc.get("/ping")
        assert fake.client.get_me_calls == 0, f"get_me called (age={age}, connected={connected})"
        assert fake.safe_get_messages_calls == 0, f"safe_get_messages called (age={age}, connected={connected})"


def test_ping_route_needs_no_token(patch_client):
    # /ping is unauthenticated by design (no token path variant); it just works.
    fake, tc = patch_client(age=1.0, is_connected=True)
    assert tc.get("/ping").status_code == 200


# --------------------------------------------------------------------------- #
# TelegramClient.watchdog_last_ok_age accessor
# --------------------------------------------------------------------------- #
def test_watchdog_last_ok_age_none_when_never_probed():
    tgc = TelegramClient()
    assert tgc._wd_last_ok_monotonic is None
    assert tgc.watchdog_last_ok_age() is None


def test_watchdog_last_ok_age_positive_after_probe():
    tgc = TelegramClient()
    tgc._wd_last_ok_monotonic = time.monotonic() - 5
    age = tgc.watchdog_last_ok_age()
    assert age is not None
    assert age >= 5.0
    # Sanity: a plausible upper bound so we didn't accidentally read the wrong field.
    assert age < 60.0
