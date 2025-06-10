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
import time
from datetime import datetime, timedelta
from typing import Any, Optional, Union, List
from pyrogram import Client
from pyrogram.types import Chat, Message

logger = logging.getLogger(__name__)

# Path to cache directory
CACHE_DIR = os.path.join('data', 'tgcache')

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
        
        # Create cache metadata
        cache_data = {
            'timestamp': time.time(),
            'limit': limit,
            'messages': pickle.dumps(messages)
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
        
        # Restore message list from pickle
        messages = pickle.loads(cache_data['messages'])
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
    cached_messages = _get_history_from_cache(channel_id, limit)
    
    if cached_messages is not None:
        return cached_messages
    
    try:
        logger.info(f"history_cache_request: fetching fresh history for channel {channel_id}, limit {limit}")
        messages = []
        async for message in client.get_chat_history(channel_id, limit=limit):
            messages.append(message)
        _save_history_to_cache(channel_id, messages, limit)
        
        return messages
    except Exception as e:
        logger.error(f"history_cache_request_error: channel {channel_id}, limit {limit}, error {str(e)}")
        raise
