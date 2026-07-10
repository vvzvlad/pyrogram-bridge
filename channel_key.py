#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=missing-function-docstring

"""Canonical channel key.

Dependency-free helper shared by tg_cache, post_parser and api_server. Kept free of
any project imports so it can be imported by all three layers without introducing a
circular import.
"""

from typing import Union


def canonical_channel_key(channel: Union[str, int]) -> str:
    """Canonical cache/DB key for a channel.

    Telegram usernames are case-insensitive -> lowercase them.
    Numeric '-100...' ids keep their exact string form. The '@' prefix is stripped.
    The canonical form is also SAFE for Telegram API calls (usernames case-insensitive
    on the API side; numeric unchanged) -- callers may thread one value through both
    filesystem paths and API calls.
    """
    s = str(channel).strip().lstrip('@')
    if s.startswith('-100') and s[4:].isdigit():
        return s
    return s.lower()
