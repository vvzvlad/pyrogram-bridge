#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation, global-statement
# pylance: disable=reportMissingImports, reportMissingModuleSource

import ipaddress
import os
import sys
import logging
from typing import Any, NoReturn

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

def _exit_invalid(name: str, requirement: str, raw: str) -> NoReturn:
    """Print which variable holds an invalid value and stop the start."""
    print(f"{name} {requirement}, got: {raw!r}", flush=True)
    sys.exit(1)

def _env(name: str) -> str | None:
    """Return the variable's value, or None when it is unset or blank: both mean the default."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw

def _int_env(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        _exit_invalid(name, "must be a valid integer", raw)
    if value < minimum or (maximum is not None and value > maximum):
        bound = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        _exit_invalid(name, f"must be {bound}", raw)
    return value

def _bool_env(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    if raw.strip() in ("true", "True"):
        return True
    if raw.strip() in ("false", "False"):
        return False
    _exit_invalid(name, "must be one of true, True, false, False", raw)

def _log_level_env() -> str:
    raw = _env("LOG_LEVEL")
    if raw is None:
        return "INFO"
    level = raw.strip().upper()
    names = logging.getLevelNamesMapping()
    if level not in names:
        _exit_invalid("LOG_LEVEL", f"must be one of {', '.join(names)} (any case)", raw)
    return level

def _trusted_proxies_env() -> list[str]:
    raw = _env("TRUSTED_PROXIES")
    if raw is None:
        return []
    proxies = [ip.strip() for ip in raw.split(",") if ip.strip()]
    for ip in proxies:
        # A subnet is rejected: is_local_request compares the client address with this
        # list as a string, so a subnet would never match anything.
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            _exit_invalid("TRUSTED_PROXIES", "must be a comma-separated list of IP addresses (no subnets)", raw)
    return proxies

def get_settings() -> dict[str, Any]:
    """Read and validate every environment variable of the bridge.

    An unset or blank variable takes its default. An invalid value prints
    `<NAME> ..., got: <value>` and exits with code 1, so the bridge does not start.
    """
    tg_api_id = os.getenv("TG_API_ID")
    tg_api_hash = os.getenv("TG_API_HASH")
    if not tg_api_id or not tg_api_hash:
        print("TG_API_ID and TG_API_HASH must be set", flush=True)
        sys.exit(1)

    # Validate and convert TG_API_ID to integer
    try:
        tg_api_id_int = int(tg_api_id)
    except ValueError:
        _exit_invalid("TG_API_ID", "must be a valid integer", tg_api_id)

    # Build MTProto proxy config if proxy host is provided.
    # Telegram MTProto proxy (telegrammessenger/proxy) exposes a SOCKS5 interface,
    # so Pyrogram connects to it via SOCKS5 scheme.
    proxy_host = os.getenv("TG_PROXY_HOST")
    proxy_port = _int_env("TG_PROXY_PORT", 1080, minimum=1, maximum=65535)
    proxy: dict[str, Any] | None = None
    if proxy_host:
        proxy = {
            "scheme": "SOCKS5",
            "hostname": proxy_host,
            "port": proxy_port,
            "username": os.getenv("TG_PROXY_USERNAME") or None,
            "password": os.getenv("TG_PROXY_PASSWORD") or None,
        }

    return {
        "tg_api_id": tg_api_id_int,
        "tg_api_hash": tg_api_hash,
        "api_port": _int_env("API_PORT", 8000, minimum=1, maximum=65535),
        "pyrogram_bridge_url": os.getenv("PYROGRAM_BRIDGE_URL", ""),
        "log_level": _log_level_env(),
        "token": os.getenv("TOKEN", ""),
        "trusted_proxies": _trusted_proxies_env(),
        "time_based_merge": _bool_env("TIME_BASED_MERGE", False),
        "show_bridge_link": _bool_env("SHOW_BRIDGE_LINK", False),
        "show_post_flags": _bool_env("SHOW_POST_FLAGS", False),
        "proxy": proxy,
        # Hard cap (seconds) on any single live Telegram RPC held under the global RPC gate,
        # so a hung MTProto call can never pin the gate (and the whole app) indefinitely.
        "tg_rpc_timeout": _int_env("TG_RPC_TIMEOUT", 60, minimum=1),
        "tg_rpc_concurrency": _int_env("TG_RPC_CONCURRENCY", 1, minimum=1),
        "tg_rpc_min_interval_ms": _int_env("TG_RPC_MIN_INTERVAL_MS", 500, minimum=0),
        "tg_chat_cache_ttl_hours": _int_env("TG_CHAT_CACHE_TTL_HOURS", 12, minimum=1),
        "tg_watchdog_enabled": _bool_env("TG_WATCHDOG_ENABLED", True),
        "media_signing_secret": os.getenv("MEDIA_SIGNING_SECRET") or None,
        "media_url_ttl_days": _int_env("MEDIA_URL_TTL_DAYS", 0, minimum=0),
        "media_allow_legacy_digest": _bool_env("MEDIA_ALLOW_LEGACY_DIGEST", True),
        # A post replying to a NEIGHBOURING post of its own channel quotes it in full, so two
        # adjacent feed entries read as half the same text — Telegram itself only shows a short
        # preview there. Such a quote is cut to this many VISIBLE characters (200 ≈ the preview
        # Telegram renders). 0 disables the truncation entirely (quotes stay full, as before).
        "reply_quote_truncate_chars": _int_env("REPLY_QUOTE_TRUNCATE_CHARS", 200, minimum=0),
    }
