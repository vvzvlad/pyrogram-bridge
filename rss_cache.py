#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation
# pylance: disable=reportMissingImports, reportMissingModuleSource

import logging
import os
import json
import time
import asyncio
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Union, Optional
from config import get_settings

logger = logging.getLogger(__name__)
Config = get_settings()

def get_cache_path(channel: Union[str, int], content_type: str = 'rss') -> Path:
    """
    Get the path to the cache file for a given channel
    Args:
        channel: Telegram channel name or id
        content_type: Type of content (rss or html)
    Returns:
        Path to the cache file
    """
    if content_type == 'html':
        base_dir = './data/html-cache'
        file_ext = 'html'
    else:
        base_dir = './data/rss-cache'
        file_ext = 'xml'
        
    cache_dir = Path(Config.get('cache_dir', base_dir))
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Create a unique filename based on the channel identifier
    if isinstance(channel, int):
        filename = f"channel_{channel}.{file_ext}"
    else:
        # Use hash for channel names with special characters
        if any(c in channel for c in '/<>:"|?*\\'):
            channel_hash = hashlib.md5(channel.encode()).hexdigest()
            filename = f"channel_{channel_hash}.{file_ext}"
        else:
            filename = f"channel_{channel}.{file_ext}"
    
    return cache_dir / filename

def is_cache_valid(cache_path: Path, max_age_hours: int = 2) -> bool:
    """
    Check if the cache file exists and is recent enough
    Args:
        cache_path: Path to the cache file
        max_age_hours: Maximum age of the cache in hours
    Returns:
        True if cache exists and is recent enough, False otherwise
    """
    if not cache_path.exists():
        return False
    
    file_mod_time = datetime.fromtimestamp(cache_path.stat().st_mtime)
    max_age = timedelta(hours=max_age_hours)
    
    return datetime.now() - file_mod_time < max_age

def read_cache(channel: Union[str, int], 
            content_type: str = 'rss', 
            max_age_hours: int = 2) -> Union[str, None]:
    """
    Read content from cache if it exists and is valid
    Args:
        channel: Channel name or ID
        content_type: Type of content (rss or html)
        max_age_hours: Maximum age of the cache in hours
    Returns:
        Cached content as string or None if cache is invalid/missing
    """
    cache_path = get_cache_path(channel, content_type)
    
    if not is_cache_valid(cache_path, max_age_hours):
        return None
    
    try:
        logger.debug(f"using_cached_{content_type}: channel {channel}, cache_file {cache_path}")
        with open(cache_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"cache_read_error: channel {channel}, error {str(e)}")
        return None

def write_cache(channel: Union[str, int], 
             content: str, 
             content_type: str = 'rss') -> bool:
    """
    Write content to cache
    Args:
        channel: Channel name or ID
        content: Content to cache
        content_type: Type of content (rss or html)
    Returns:
        True if cache was written successfully, False otherwise
    """
    cache_path = get_cache_path(channel, content_type)
    
    try:
        with open(cache_path, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.debug(f"{content_type}_cache_saved: channel {channel}, cache_file {cache_path}")
        return True
    except Exception as e:
        logger.error(f"cache_write_error: channel {channel}, error {str(e)}")
        return False

def get_access_file_path() -> str:
    """
    Get path to the cache access file
    Returns:
        Path to the cache access file
    """
    return os.path.join(os.path.abspath("./data"), 'cache_access.json')

def update_channel_access_time(channel: Union[str, int]) -> bool:
    """
    Update access time for a channel in cache_access.json
    Args:
        channel: Channel name or ID
    Returns:
        True if successful, False otherwise
    """
    try:
        access_file_path = get_access_file_path()
        cache_access_data = {}
        
        # Load existing data if file exists
        if os.path.exists(access_file_path):
            try:
                with open(access_file_path, 'r', encoding='utf-8') as f:
                    cache_access_data = json.load(f)
            except json.JSONDecodeError:
                logger.error(f"Error decoding cache_access.json, creating new file")
                cache_access_data = {}
        
        # Update channel access time
        channel_key = str(channel)
        cache_access_data[channel_key] = time.time()
        
        # Save updated data
        with open(access_file_path, 'w', encoding='utf-8') as f:
            json.dump(cache_access_data, f, indent=2)
        
        logger.debug(f"Updated access time for channel {channel} in cache_access.json")
        return True
    except Exception as e:
        logger.error(f"Failed to update cache_access.json: {str(e)}")
        return False

async def clean_old_channels(max_age_days: int = 30) -> int:
    """
    Remove channels that haven't been accessed for more than specified days
    
    Args:
        max_age_days: Maximum age of channels in days
    
    Returns:
        Number of removed channels
    """
    access_file_path = get_access_file_path()
    
    if not os.path.exists(access_file_path):
        logger.info("access_file_not_found: Creating new file")
        with open(access_file_path, 'w', encoding='utf-8') as f:
            json.dump({}, f)
        return 0
    
    try:
        with open(access_file_path, 'r', encoding='utf-8') as f:
            cache_access = json.load(f)
        
        now = time.time()
        max_age_seconds = max_age_days * 86400  # days to seconds
        
        old_channels = []
        for channel, timestamp in list(cache_access.items()):
            age = now - timestamp
            if age > max_age_seconds:
                old_channels.append(channel)
                del cache_access[channel]
        
        # Save updated data
        if old_channels:
            with open(access_file_path, 'w', encoding='utf-8') as f:
                json.dump(cache_access, f, indent=2)
            logger.info(f"removed_old_channels: {len(old_channels)} channels removed (older than {max_age_days} days)")
        
        return len(old_channels)
    
    except Exception as e:
        logger.error(f"clean_old_channels_error: {str(e)}")
        return 0

async def update_rss_cache(client, channel: str, limit: int = 50) -> bool:
    """
    Update RSS cache for specified channel
    
    Args:
        client: Telegram client
        channel: Channel identifier
        limit: Maximum number of posts
    
    Returns:
        True if cache was successfully updated, False otherwise
    """
    try:
        logger.info(f"updating_rss_cache: channel {channel}")
        
        # Import here to avoid circular imports
        from rss_generator import generate_channel_rss
        
        # Force generate new RSS without using cache
        await generate_channel_rss(
            channel=channel,
            client=client,
            use_cache=False,  # Force generate new RSS
            limit=limit
        )
        
        logger.info(f"updated_rss_cache: channel {channel}")
        return True
    except Exception as e:
        logger.error(f"rss_cache_update_error: channel {channel}, error {str(e)}")
        return False

async def start_cache_updater(client) -> None:
    """
    Background task for updating RSS cache periodically
    
    Args:
        client: Telegram client instance
    """
    # Settings from configuration
    update_interval = Config.get("rss_cache_update_interval", 3600)  # seconds
    update_delay = Config.get("rss_cache_update_delay", 60)  # seconds
    max_age_days = Config.get("rss_cache_max_age_days", 30)
    
    logger.info(f"starting_rss_cache_updater: interval={update_interval}s, delay={update_delay}s, max_age={max_age_days}d")
    
    try:
        while True:
            # Clean old channels
            cleaned = await clean_old_channels(max_age_days)
            
            # Load channels for update
            access_file_path = get_access_file_path()
            try:
                if os.path.exists(access_file_path):
                    with open(access_file_path, 'r', encoding='utf-8') as f:
                        cache_access = json.load(f)
                    channels = list(cache_access.keys())
                    
                    if channels:
                        logger.info(f"starting_cache_update: updating {len(channels)} channels")
                        
                        # Sort channels by last access time (newest first)
                        channels_sorted = sorted(channels, key=lambda c: cache_access[c], reverse=True)
                        
                        # Update cache for each channel with delay
                        for channel in channels_sorted:
                            success = await update_rss_cache(client, channel)
                            # Sleep between updates to avoid rate limits
                            await asyncio.sleep(update_delay)
                        
                        logger.info(f"finished_cache_update: updated {len(channels)} channels")
                    else:
                        logger.info("no_channels_to_update: Waiting for next interval")
                else:
                    logger.info("no_access_file: Waiting for next interval")
            
            except Exception as e:
                logger.error(f"rss_cache_update_cycle_error: {str(e)}")
            
            # Wait until next update cycle
            logger.info(f"waiting_for_next_update: next update in {update_interval} seconds")
            await asyncio.sleep(update_interval)
    
    except asyncio.CancelledError:
        logger.info("rss_cache_updater_cancelled")
    except Exception as e:
        logger.error(f"rss_cache_updater_error: {str(e)}") 