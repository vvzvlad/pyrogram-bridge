#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation
# pylance: disable=reportMissingImports, reportMissingModuleSource


import os
import json
import pickle
import logging
import random
import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Optional, Union, List
from pyrogram import Client
from pyrogram.types import Chat, Message
from tg_throttle import tg_rpc

logger = logging.getLogger(__name__)

# Path to cache directory
CACHE_DIR = os.path.join('data', 'tgcache')

# TTL (hours) for the channel-info cache. Channel title/username/id change rarely,
# so a long TTL removes the rate-limited GetFullChannel from the feed-poll hot path.
try:
    CHAT_CACHE_TTL_HOURS = int(os.getenv("TG_CHAT_CACHE_TTL_HOURS", "12"))
    if CHAT_CACHE_TTL_HOURS < 1:
        CHAT_CACHE_TTL_HOURS = 12
except ValueError:
    CHAT_CACHE_TTL_HOURS = 12

def _get_history_cache_file_path(channel_id: Union[str, int]) -> str:
    """Returns path to the message history cache file for the channel"""
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
        logger.info(f"cache_dir_created: path {CACHE_DIR}")
    # Convert to string for uniformity
    channel_id_str = str(channel_id)
    # Replace potentially problematic characters
    safe_filename = channel_id_str.replace('/', '_').replace('\\', '_')
    return os.path.join(CACHE_DIR, f"{safe_filename}.cache")

def _save_history_to_cache(channel_id: Union[str, int], messages: List[Message], limit: int) -> None:
    """Saves message history to cache"""
    try:
        cache_file = _get_history_cache_file_path(channel_id)
        
        # Create cache metadata — store messages directly (no inner pickle.dumps)
        cache_data = {
            'timestamp': time.time(),
            'limit': limit,
            'messages': messages
        }
        
        with open(cache_file, 'wb') as f:
            pickle.dump(cache_data, f)
        
        logger.info(f"history_cache_saved: channel {channel_id}, limit {limit}, messages {len(messages)}, file {cache_file}")
    except Exception as e:
        logger.error(f"history_cache_save_error: channel {channel_id}, limit {limit}, error {str(e)}")

def _get_history_from_cache(channel_id: Union[str, int], limit: int, max_age_hours: int = 8) -> Optional[List[Message]]:
    """
    Retrieves message history from cache if not older than specified age and matches the limit
    
    Args:
        channel_id: Channel ID or username
        limit: Required message limit
        max_age_hours: Maximum cache age in hours (default 8 hours)
        
    Returns:
        List of messages or None if cache not found, expired or limit doesn't match
    """
    try:
        cache_file = _get_history_cache_file_path(channel_id)
        
        if not os.path.exists(cache_file):
            logger.info(f"history_cache_miss: channel {channel_id}, limit {limit}, cache file not found")
            return None
        
        with open(cache_file, 'rb') as f:
            cache_data = pickle.load(f)
        
        # Check cache age with randomization
        cache_age = time.time() - cache_data['timestamp']
        # Add randomness up to 20% of max_age_seconds
        random_factor = 1 - random.uniform(0, 0.2)
        adjusted_max_age = max_age_hours * 3600 * random_factor
        
        if cache_age > adjusted_max_age:
            logger.info(f"history_cache_expired: channel {channel_id}, limit {limit}, age {cache_age:.1f}s > adjusted max {adjusted_max_age:.1f}s (random factor: {random_factor:.2f})")
            return None
        
        # Check if limit matches
        cached_limit = cache_data.get('limit', 0)
        if cached_limit != limit:
            logger.info(f"history_cache_limit_mismatch: channel {channel_id}, cached limit {cached_limit}, requested limit {limit}")
            return None
        
        # Restore message list; handle old cache files that used double-pickle (bytes = old format)
        raw = cache_data['messages']
        if isinstance(raw, bytes):
            messages = pickle.loads(raw)
        else:
            messages = raw
        logger.info(f"history_cache_hit: channel {channel_id}, limit {limit}, messages {len(messages)}, age {cache_age:.1f}s")
        return messages
    
    except Exception as e:
        logger.error(f"history_cache_read_error: channel {channel_id}, limit {limit}, error {str(e)}")
        # In case of cache read error, better return None and request fresh data
        return None

async def cached_get_chat_history(client: Client, channel_id: Union[str, int], limit: int = 20) -> List[Message]:
    """
    Gets chat message history with caching.
    
    Args:
        client: Pyrogram client
        channel_id: Channel ID or username
        limit: Maximum number of messages to retrieve
        
    Returns:
        List of messages, same as original client.get_chat_history()
    """
    cached_messages = await asyncio.to_thread(_get_history_from_cache, channel_id, limit)
    
    if cached_messages is not None:
        return cached_messages
    
    try:
        logger.info(f"history_cache_request: fetching fresh history for channel {channel_id}, limit {limit}")
        messages = []
        async with tg_rpc():
            async for message in client.get_chat_history(channel_id, limit=limit):
                messages.append(message)
        await asyncio.to_thread(_save_history_to_cache, channel_id, messages, limit)
        
        return messages
    except Exception as e:
        logger.error(f"history_cache_request_error: channel {channel_id}, limit {limit}, error {str(e)}")
        raise


def _get_chat_cache_file_path(channel_id: Union[str, int]) -> str:
    """Returns path to the channel-info cache file for the channel."""
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
        logger.info(f"cache_dir_created: path {CACHE_DIR}")
    channel_id_str = str(channel_id)
    safe_filename = channel_id_str.replace('/', '_').replace('\\', '_')
    # Distinct suffix so channel-info files never collide with history (.cache) files.
    return os.path.join(CACHE_DIR, f"{safe_filename}.chatinfo")


def _save_chat_to_cache(channel_id: Union[str, int], data: dict) -> None:
    """Saves channel-info (id/title/username) to cache."""
    try:
        cache_file = _get_chat_cache_file_path(channel_id)
        cache_data = {'timestamp': time.time(), 'data': data}
        with open(cache_file, 'wb') as f:
            pickle.dump(cache_data, f)
        logger.info(f"chatinfo_cache_saved: channel {channel_id}, file {cache_file}")
    except Exception as e:
        logger.error(f"chatinfo_cache_save_error: channel {channel_id}, error {str(e)}")


def _get_chat_from_cache(channel_id: Union[str, int], max_age_hours: int = CHAT_CACHE_TTL_HOURS) -> Optional[dict]:
    """Retrieves channel-info from cache if not older than max_age_hours (with up-to-20% randomization)."""
    try:
        cache_file = _get_chat_cache_file_path(channel_id)
        if not os.path.exists(cache_file):
            logger.info(f"chatinfo_cache_miss: channel {channel_id}, cache file not found")
            return None
        with open(cache_file, 'rb') as f:
            cache_data = pickle.load(f)
        cache_age = time.time() - cache_data['timestamp']
        # Add randomness up to 20% of max age, same approach as the history cache.
        random_factor = 1 - random.uniform(0, 0.2)
        adjusted_max_age = max_age_hours * 3600 * random_factor
        if cache_age > adjusted_max_age:
            logger.info(f"chatinfo_cache_expired: channel {channel_id}, age {cache_age:.1f}s > adjusted max {adjusted_max_age:.1f}s (random factor: {random_factor:.2f})")
            return None
        data = cache_data.get('data')
        if not isinstance(data, dict):
            logger.warning(f"chatinfo_cache_invalid: channel {channel_id}, unexpected payload type {type(data).__name__}")
            return None
        logger.info(f"chatinfo_cache_hit: channel {channel_id}, age {cache_age:.1f}s")
        return data
    except Exception as e:
        logger.error(f"chatinfo_cache_read_error: channel {channel_id}, error {str(e)}")
        return None


async def cached_get_chat(client: Client, channel_id: Union[str, int]) -> SimpleNamespace:
    """Gets channel info (id/title/username) with disk TTL caching.

    Avoids the rate-limited channels.GetFullChannel on every feed poll. Mirrors
    cached_get_chat_history. On a cache miss the live get_chat is throttled and its
    exceptions (FloodWait, UsernameInvalid, ...) propagate to the caller unchanged;
    only successful lookups are cached. The returned object exposes .id/.title/.username.
    """
    cached = await asyncio.to_thread(_get_chat_from_cache, channel_id)
    if cached is not None:
        return SimpleNamespace(**cached)

    logger.info(f"chatinfo_cache_request: fetching fresh chat info for channel {channel_id}")
    async with tg_rpc():
        chat = await client.get_chat(channel_id)

    data = {
        'id': getattr(chat, 'id', None),
        'title': getattr(chat, 'title', None),
        'username': getattr(chat, 'username', None),
    }
    await asyncio.to_thread(_save_chat_to_cache, channel_id, data)
    return SimpleNamespace(**data)
