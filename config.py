#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation, global-statement
# pylance: disable=reportMissingImports, reportMissingModuleSource

import os
import sys
import logging
from typing import Any

_LOGGING_INITIALIZED = False

def setup_logging(level_name: str = "INFO") -> None:
    global _LOGGING_INITIALIZED 
    
    if _LOGGING_INITIALIZED:  return
        
    level = getattr(logging, level_name.upper(), logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if not root_logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
    else:
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)

    logging.getLogger('uvicorn').setLevel(logging.WARNING)
    logging.getLogger('fastapi').setLevel(logging.WARNING)
    
    _LOGGING_INITIALIZED = True
    logging.info("Logging system initialized")

def get_settings() -> dict[str, Any]:
    tg_api_id = os.getenv("TG_API_ID")
    tg_api_hash = os.getenv("TG_API_HASH")
    if not tg_api_id or not tg_api_hash:
        print("TG_API_ID and TG_API_HASH must be set", flush=True)
        sys.exit(1)

    # Validate and convert TG_API_ID to integer
    try:
        tg_api_id_int = int(tg_api_id)
    except ValueError:
        print(f"TG_API_ID must be a valid integer, got: {tg_api_id!r}", flush=True)
        sys.exit(1)

    log_level = os.getenv("LOG_LEVEL", "INFO")

    # Build MTProto proxy config if proxy host is provided.
    # Telegram MTProto proxy (telegrammessenger/proxy) exposes a SOCKS5 interface,
    # so Pyrogram connects to it via SOCKS5 scheme.
    proxy_host = os.getenv("TG_PROXY_HOST")
    proxy: dict[str, Any] | None = None
    if proxy_host:
        # Validate and convert TG_PROXY_PORT to integer
        try:
            proxy_port = int(os.getenv("TG_PROXY_PORT") or 1080)
        except ValueError:
            print(f"TG_PROXY_PORT must be a valid integer, got: {os.getenv('TG_PROXY_PORT')!r}", flush=True)
            sys.exit(1)
        proxy = {
            "scheme": "SOCKS5",
            "hostname": proxy_host,
            "port": proxy_port,
            "username": os.getenv("TG_PROXY_USERNAME") or None,
            "password": os.getenv("TG_PROXY_PASSWORD") or None,
        }

    # Validate and convert API_PORT to integer
    try:
        api_port = int(os.getenv("API_PORT") or 8000)
    except ValueError:
        print(f"API_PORT must be a valid integer, got: {os.getenv('API_PORT')!r}", flush=True)
        sys.exit(1)

    # Local helper to parse int env vars with a default and exit on a bad value
    def _parse_int_env(name: str, default: int, minimum: int = 1) -> int:
        raw = os.getenv(name)
        if raw is None or raw.strip() == "":
            return default
        try:
            value = int(raw)
        except ValueError:
            print(f"{name} must be a valid integer, got: {raw!r}", flush=True)
            sys.exit(1)
        if value < minimum:
            print(f"{name} must be >= {minimum}, got: {value}", flush=True)
            sys.exit(1)
        return value

    return {
        "tg_api_id": tg_api_id_int,
        "tg_api_hash": tg_api_hash,
        "session_path": os.getenv("SESSION_PATH", "data") or "data",
        "api_host": os.getenv("API_HOST", "0.0.0.0"),
        "api_port": api_port,
        "pyrogram_bridge_url": os.getenv("PYROGRAM_BRIDGE_URL", ""),
        "log_level": log_level,
        "debug": os.getenv("DEBUG", "False").strip() in ["True", "true"],
        "token": os.getenv("TOKEN", ""),
        "trusted_proxies": [ip.strip() for ip in os.getenv("TRUSTED_PROXIES", "").split(",") if ip.strip()],
        "time_based_merge": os.getenv("TIME_BASED_MERGE", "False").strip() in ["True", "true"],
        "show_bridge_link": os.getenv("SHOW_BRIDGE_LINK", "False").strip() in ["True", "true"],
        "show_post_flags": os.getenv("SHOW_POST_FLAGS", "False").strip() in ["True", "true"],
        "proxy": proxy,
        # Hard cap (seconds) on any single live Telegram RPC held under the global RPC gate,
        # so a hung MTProto call can never pin the gate (and the whole app) indefinitely.
        "tg_rpc_timeout": _parse_int_env("TG_RPC_TIMEOUT", 60),
        "tg_watchdog_enabled": os.getenv("TG_WATCHDOG_ENABLED", "true").strip().lower() not in ["false", "0", "no", "off", "disable", "disabled"],
        "tg_watchdog_interval": _parse_int_env("TG_WATCHDOG_INTERVAL", 60),
        "tg_watchdog_timeout": _parse_int_env("TG_WATCHDOG_TIMEOUT", 10),
        "tg_watchdog_failures": _parse_int_env("TG_WATCHDOG_FAILURES", 3),
        "tg_watchdog_restart_timeout": _parse_int_env("TG_WATCHDOG_RESTART_TIMEOUT", 90),
        "tg_watchdog_heartbeat_every": _parse_int_env("TG_WATCHDOG_HEARTBEAT_EVERY", 30),
        "tg_disconnect_flap_limit": _parse_int_env("TG_DISCONNECT_FLAP_LIMIT", 3),
        "tg_disconnect_flap_window": _parse_int_env("TG_DISCONNECT_FLAP_WINDOW", 120),
        # /ping reports TG as unhealthy once the last successful watchdog probe is older than
        # this many seconds. Default is derived from the watchdog cadence: it is roughly how
        # long the watchdog itself would take to give up and trigger a restart —
        # interval * (failures + 1) + timeout. With the defaults (60,3,10) that is 250s, so a
        # transient slow probe never flaps /ping, but a genuinely stuck session (no successful
        # probe for ~4 min) surfaces as 503 before/around the time the watchdog restarts.
        "tg_ping_unhealthy_after": _parse_int_env(
            "TG_PING_UNHEALTHY_AFTER",
            _parse_int_env("TG_WATCHDOG_INTERVAL", 60) * (_parse_int_env("TG_WATCHDOG_FAILURES", 3) + 1)
            + _parse_int_env("TG_WATCHDOG_TIMEOUT", 10),
        ),
        # Media download timeout scales with file size (large videos): the per-download
        # timeout is clamped to [min, max] seconds, with an effective floor of
        # `media_download_min_speed` bytes/s (timeout ≈ file_size / min_speed).
        "media_download_timeout_min": _parse_int_env("MEDIA_DOWNLOAD_TIMEOUT_MIN", 120),
        "media_download_timeout_max": _parse_int_env("MEDIA_DOWNLOAD_TIMEOUT_MAX", 1800),
        "media_download_min_speed": _parse_int_env("MEDIA_DOWNLOAD_MIN_SPEED", 256 * 1024),
        # Size of the asyncio default ThreadPoolExecutor. SQLite/python-magic/pickle/os.walk
        # all run via asyncio.to_thread; the interpreter default (min(32, cpu+4)) is only 5-6
        # on a 1-2 CPU container, which starves those under load. 32 gives ample headroom.
        "io_thread_pool_size": _parse_int_env("IO_THREAD_POOL_SIZE", 32),
    }
