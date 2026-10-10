#!/usr/bin/env python3
# -*- coding: utf-8 -*-

def setup_logging(level_name: str = "INFO") -> None:
    """No-op logging setup for tests (mirrors config.setup_logging signature)."""
    return None

def get_settings():
    """
    Mock config for testing without requiring TG_API_ID and TG_API_HASH
    """
    return {
        "tg_api_id": 12345,
        "tg_api_hash": "test_hash",
        "api_port": 8080,
        "pyrogram_bridge_url": "http://test.example.com",
        "log_level": "DEBUG",
        "token": "test_token",
        "time_based_merge": False,
        "show_bridge_link": False,
        "show_post_flags": True,
        "proxy": None,
        "trusted_proxies": [],
        "tg_rpc_timeout": 60,
        "tg_rpc_concurrency": 1,
        "tg_rpc_min_interval_ms": 500,
        "tg_chat_cache_ttl_hours": 12,
        "tg_watchdog_enabled": True,
        "media_signing_secret": None,
        "media_url_ttl_days": 0,
        "media_allow_legacy_digest": True,
        "reply_quote_truncate_chars": 200,
    }
