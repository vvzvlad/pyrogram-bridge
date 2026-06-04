#!/usr/bin/env python3
# -*- coding: utf-8 -*-

def get_settings():
    """
    Mock config for testing without requiring TG_API_ID and TG_API_HASH
    """
    return {
        "tg_api_id": 12345,
        "tg_api_hash": "test_hash",
        "session_path": "tests/test_data",
        "api_host": "127.0.0.1",
        "api_port": 8080,
        "pyrogram_bridge_url": "http://test.example.com",
        "log_level": "DEBUG",
        "debug": False,
        "token": "test_token",
        "time_based_merge": False,
        "show_bridge_link": False,
        "show_post_flags": True,
        "proxy": None,
        "trusted_proxies": [],
        "tg_watchdog_enabled": True,
        "tg_watchdog_interval": 60,
        "tg_watchdog_timeout": 10,
        "tg_watchdog_failures": 3,
        "tg_watchdog_restart_timeout": 90,
        "tg_disconnect_flap_limit": 3,
        "tg_disconnect_flap_window": 120,
    }
