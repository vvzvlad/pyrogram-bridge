# flake8: noqa
# pylint: disable=missing-function-docstring, redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""Startup validation of the environment variables in config.get_settings.

An invalid value of any variable stops the start: get_settings prints
`<NAME> ..., got: <value>` and exits with code 1. conftest.py replaces the `config` module
with tests/mock_config.py, so the real config.py is loaded here under a module name of its own.
"""
import importlib.util
import os

import pytest

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py")
_spec = importlib.util.spec_from_file_location("real_config", _CONFIG_PATH)
real_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_config)

# Every variable get_settings reads; each test starts with none of them set but the two
# required ones, so the runner's own environment cannot leak in.
VARIABLES = (
    "TG_API_ID", "TG_API_HASH", "LOG_LEVEL",
    "TG_PROXY_HOST", "TG_PROXY_PORT", "TG_PROXY_USERNAME", "TG_PROXY_PASSWORD",
    "API_PORT", "PYROGRAM_BRIDGE_URL", "TOKEN", "TRUSTED_PROXIES",
    "TIME_BASED_MERGE", "SHOW_BRIDGE_LINK", "SHOW_POST_FLAGS", "REPLY_QUOTE_TRUNCATE_CHARS",
    "TG_RPC_TIMEOUT", "TG_RPC_CONCURRENCY", "TG_RPC_MIN_INTERVAL_MS", "TG_CHAT_CACHE_TTL_HOURS",
    "MEDIA_SIGNING_SECRET", "MEDIA_URL_TTL_DAYS", "MEDIA_ALLOW_LEGACY_DIGEST",
    "TG_WATCHDOG_ENABLED",
)


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")


@pytest.mark.parametrize(("name", "value"), [
    ("TG_RPC_CONCURRENCY", "abc"),
    ("SHOW_POST_FLAGS", "maybe"),
    ("SHOW_POST_FLAGS", "1"),
    ("TIME_BASED_MERGE", "TRUE"),
    ("TG_WATCHDOG_ENABLED", "0"),
    ("LOG_LEVEL", "LOUD"),
    ("TRUSTED_PROXIES", "1.2.3"),
    ("MEDIA_URL_TTL_DAYS", "-1"),
    ("API_PORT", "70000"),
])
def test_invalid_value_stops_the_start(monkeypatch, capsys, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit) as exc:
        real_config.get_settings()

    assert exc.value.code == 1
    out = capsys.readouterr().out.strip()
    assert out.startswith(f"{name} ")
    assert out.endswith(f", got: {value!r}")


def test_valid_values_start(monkeypatch):
    for name, value in {
        "TIME_BASED_MERGE": "True",
        "SHOW_POST_FLAGS": "false",
        "TG_WATCHDOG_ENABLED": "False",
        "MEDIA_ALLOW_LEGACY_DIGEST": "true",
        "LOG_LEVEL": "warn",
        "TRUSTED_PROXIES": "10.0.0.1, ::1",
        "API_PORT": "65535",
        "TG_RPC_CONCURRENCY": "2",
        "MEDIA_URL_TTL_DAYS": "0",
        "TG_RPC_TIMEOUT": "",
    }.items():
        monkeypatch.setenv(name, value)

    settings = real_config.get_settings()

    assert settings["time_based_merge"] is True
    assert settings["show_post_flags"] is False
    assert settings["tg_watchdog_enabled"] is False
    assert settings["media_allow_legacy_digest"] is True
    assert settings["log_level"] == "WARN"
    assert settings["trusted_proxies"] == ["10.0.0.1", "::1"]
    assert settings["api_port"] == 65535
    assert settings["tg_rpc_concurrency"] == 2
    assert settings["media_url_ttl_days"] == 0
    assert settings["tg_rpc_timeout"] == 60  # blank means the default
