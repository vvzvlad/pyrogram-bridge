#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name, wrong-import-position
# pylance: disable=reportMissingImports, reportMissingModuleSource, reportGeneralTypeIssues
# type: ignore

import logging
import os
import mimetypes
from typing import List

import json
from datetime import datetime
import time
from contextlib import asynccontextmanager
import random
import asyncio
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.background import BackgroundTask
import json_repair

import magic
from pyrogram import errors
from fastapi import FastAPI, HTTPException, Response, Request
from fastapi.responses import HTMLResponse, FileResponse
from telegram_client import TelegramClient
from config import get_settings
from rss_generator import generate_channel_rss, generate_channel_html
from post_parser import PostParser
from url_signer import verify_media_digest, generate_media_digest

# Define custom exception for zero-size files
class ZeroSizeFileError(Exception):
    """Custom exception for zero-size files found or downloaded."""

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Log request details
        logger.info(f"Request: {request.method} {request.url}")
        logger.info(f"Headers: {dict(request.headers)}")
        logger.info(f"Query params: {dict(request.query_params)}")
        
        try:
            response = await call_next(request)
            # Log response details
            logger.info(f"Response status: {response.status_code}")
            return response
        except Exception as e:
            logger.error(f"Request processing error: {str(e)}")
            raise

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)

client = TelegramClient()
Config = get_settings()

@asynccontextmanager
async def lifespan(_: FastAPI):
    base_cache_dir = os.path.abspath("./data/cache")
    os.makedirs(base_cache_dir, exist_ok=True) # Create cache directory
    
    await client.start()
    background_task = asyncio.create_task(cache_media_files()) # Start background task
    yield
    background_task.cancel() # Cleanup
    try:
        await background_task
    except asyncio.CancelledError:
        pass
    await client.stop()

app = FastAPI(title="Pyrogram Bridge", lifespan=lifespan)
app.add_middleware(RequestLoggingMiddleware)

def mask_sensitive_value(input_str: str) -> str:
    """Mask middle part of sensitive value, showing only first and last 4 characters"""
    if not input_str or len(input_str) < 8:
        return '***'
    visible_chars = 4
    return f"{input_str[:visible_chars]}{'*' * (len(input_str) - visible_chars * 2)}{input_str[-visible_chars:]}"


if __name__ == "__main__":
    import uvicorn
    
    logger.info("Starting server with configuration:")
    for key, value in Config.items():
        if any(sensitive in key.lower() for sensitive in ['token', 'tg_api_id', 'tg_api_hash']):
            logger.info(f"    {key}: {mask_sensitive_value(str(value))}")
        else:
            logger.info(f"    {key}: {value}")
    
    # Log uvloop status
    logger.info("    uvloop: enabled (asyncio speedup active)")
            
    uvicorn.run(
        "api_server:app", 
        host=Config["api_host"], 
        port=Config["api_port"], 
        reload=True,
        loop="uvloop"
    )

async def find_file_id_in_message(message, file_unique_id: str):
    """Find file_id by checking all possible media types in message"""
    if message.media == "MessageMediaType.POLL":
        logger.debug(f"Message {message.id} is a poll, skipping media search")
        return None
        
    media_found = []
    if message.photo:
        media_found.append(f"photo ({message.photo.file_unique_id})")
        if message.photo.file_unique_id == file_unique_id:
            return message.photo.file_id
    if message.video:
        media_found.append(f"video ({message.video.file_unique_id})")
        if message.video.file_unique_id == file_unique_id:
            return message.video.file_id
    if message.animation:
        media_found.append(f"animation ({message.animation.file_unique_id})")
        if message.animation.file_unique_id == file_unique_id:
            return message.animation.file_id
    if message.video_note:
        media_found.append(f"video_note ({message.video_note.file_unique_id})")
        if message.video_note.file_unique_id == file_unique_id:
            return message.video_note.file_id
    if message.audio:
        media_found.append(f"audio ({message.audio.file_unique_id})")
        if message.audio.file_unique_id == file_unique_id:
            return message.audio.file_id
    if message.voice:
        media_found.append(f"voice ({message.voice.file_unique_id})")
        if message.voice.file_unique_id == file_unique_id:
            return message.voice.file_id
    if message.sticker:
        media_found.append(f"sticker ({message.sticker.file_unique_id})")
        if message.sticker.file_unique_id == file_unique_id:
            return message.sticker.file_id
    if message.web_page and message.web_page.photo:
        media_found.append(f"web_page.photo ({message.web_page.photo.file_unique_id})")
        if message.web_page.photo.file_unique_id == file_unique_id:
            return message.web_page.photo.file_id
    if message.document:
        media_found.append(f"document ({message.document.file_unique_id})")
        if message.document.file_unique_id == file_unique_id:
            return message.document.file_id
            
    # If we reached here, the file_unique_id was not found
    logger.warning(f"Could not find media with file_unique_id '{file_unique_id}' in message {message.id} (channel: {message.chat.id}). Found media: {', '.join(media_found) or 'None'}")
    return None


def delayed_delete_file(file_path: str, delay: int = 300):
    """
    Delete temporary file after a delay to ensure complete file delivery.
    Delay is set to 5 minutes by default.
    """
    time.sleep(delay)
    try:
        os.remove(file_path)
        logger.info(f"Deleted temporary file {file_path} after delay of {delay} seconds")
    except Exception as e:
        logger.error(f"Failed to delete temporary file {file_path}: {str(e)}")


async def prepare_file_response(file_path: str, delete_after: bool = False):
    """Prepare file response with proper headers"""
    if not os.path.exists(file_path): raise HTTPException(status_code=404, detail="File not found")
    
    try:
        mime = magic.Magic(mime=True) # Try to determine MIME type using python-magic first
        media_type = mime.from_file(file_path)
    except Exception as e:
        logger.warning(f"Failed to determine MIME type using python-magic: {str(e)}")
        media_type = None
    
    if not media_type: media_type, _ = mimetypes.guess_type(file_path) # Fallback to mimetypes if python-magic failed
    if not media_type: media_type = "application/octet-stream" # Final fallback to octet-stream
    
    logger.debug(f"Determined media type for {os.path.basename(file_path)}: {media_type}")
    headers = {"Content-Disposition": f"inline; filename={os.path.basename(file_path)}"}
    if delete_after:
        return FileResponse(path=file_path, media_type=media_type, headers=headers, background=BackgroundTask(delayed_delete_file, file_path))
    else:
        return FileResponse(path=file_path, media_type=media_type, headers=headers)

async def download_media_file(channel: str, post_id: int, file_unique_id: str) -> str:
    """
    Download media file from Telegram and save to cache
    Returns path to downloaded file
    """
    base_cache_dir = os.path.abspath("./data/cache")
    
    # Create nested cache structure
    channel_dir = os.path.join(base_cache_dir, str(channel))
    post_dir = os.path.join(channel_dir, str(post_id))
    os.makedirs(post_dir, exist_ok=True)
    
    # Convert numeric channel ID to int if needed
    channel_id = channel
    if isinstance(channel, str) and channel.startswith('-100'):
        channel_id = int(channel)
    
    message = await client.client.get_messages(channel_id, post_id)
    if message.media == "MessageMediaType.POLL":
        return None, False
    
    # Check if it is a video and if its size exceeds 100 MB
    is_large_video = False
    if message.video:
        try:
            if message.video.file_size > 100 * 1024 * 1024:
                is_large_video = True
        except Exception as e:
            logger.error(f"Failed to get video file size for message {post_id}: {str(e)}")
    
    if is_large_video:
        # For large video, download without permanent caching; use a temporary file
        temp_file_path = os.path.join(post_dir, f"temp_{file_unique_id}")
        if os.path.exists(temp_file_path):
            logger.info(f"Temporary file {temp_file_path} already exists, serving cached large video")
            return temp_file_path, False
        file_id = await find_file_id_in_message(message, file_unique_id)
        if not file_id:
            logger.error(f"Media with file_unique_id {file_unique_id} not found in message {post_id}")
            raise HTTPException(status_code=404, detail="File not found in message")
        logger.info(f"Downloading large video file {file_unique_id} to temporary path {temp_file_path}")
        file_path = await client.client.download_media(file_id, file_name=temp_file_path)
        logger.info(f"Downloaded large video file {file_unique_id} to temporary path {temp_file_path}")
        return file_path, False

    # Normal caching flow
    cache_path = os.path.join(post_dir, file_unique_id)
    if os.path.exists(cache_path):
        # Check if the cached file is zero size
        if os.path.getsize(cache_path) == 0:
            logger.warning(f"zero_size_cache_found: Found zero-size cached file: {cache_path}. Deleting and attempting redownload.")
            try:
                os.remove(cache_path)
                logger.info(f"Removed zero-size cached file: {cache_path}")
            except OSError as e:
                logger.error(f"cleanup_error: Failed to remove zero-size cached file {cache_path}: {e}")
            # Do not raise error here, proceed to download below
        else:
            # File exists and is not zero size, update timestamp and return
            logger.info(f"Found cached media file: {cache_path}")
            # Update access timestamp in media_file_ids.json
            file_ids_path = os.path.join(os.path.abspath("./data"), 'media_file_ids.json')
            try:
                if os.path.exists(file_ids_path):
                    with open(file_ids_path, 'r', encoding='utf-8') as f:
                        media_files = json.load(f)
                    for file_data in media_files:
                        if (file_data.get('channel') == str(channel) and  # Ensure channel is string for comparison
                            file_data.get('post_id') == post_id and 
                            file_data.get('file_unique_id') == file_unique_id):
                            file_data['added'] = datetime.now().timestamp()
                            break
                    with open(file_ids_path, 'w', encoding='utf-8') as f:
                        json.dump(media_files, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"Failed to update timestamp for {channel}/{post_id}/{file_unique_id}: {str(e)}")
            return cache_path, False

    file_id = await find_file_id_in_message(message, file_unique_id)
    if not file_id:
        error_message = f"Media with file_unique_id {file_unique_id} not found in message {post_id} for channel {channel}"
        logger.error(error_message)
        
        # Attempt to remove the invalid entry from media_file_ids.json
        file_ids_path = os.path.join(os.path.abspath("./data"), 'media_file_ids.json')
        try:
            if os.path.exists(file_ids_path):
                with open(file_ids_path, 'r', encoding='utf-8') as f:
                    media_files = json.load(f)
                
                initial_count = len(media_files)
                media_files_updated = [
                    f_data for f_data in media_files 
                    if not (
                        f_data.get('channel') == str(channel) and 
                        f_data.get('post_id') == post_id and 
                        f_data.get('file_unique_id') == file_unique_id
                    )
                ]
                
                if len(media_files_updated) < initial_count:
                    with open(file_ids_path, 'w', encoding='utf-8') as f:
                        json.dump(media_files_updated, f, ensure_ascii=False, indent=2)
                    logger.info(f"Removed invalid entry for {channel}/{post_id}/{file_unique_id} from media_file_ids.json")
                else:
                    logger.warning(f"Entry for {channel}/{post_id}/{file_unique_id} not found in media_file_ids.json for removal")

        except Exception as e:
            logger.error(f"Failed to remove entry for {channel}/{post_id}/{file_unique_id} from media_file_ids.json: {str(e)}")
            
        raise HTTPException(status_code=404, detail="File not found in message")
        
    file_path = await client.client.download_media(file_id, file_name=cache_path)
    
    # Check if the downloaded file exists and has a size greater than zero
    if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        logger.error(f"download_failed_zero_size: Downloaded file {file_unique_id} for {channel}/{post_id} is zero size or missing.")
        # Attempt to clean up the invalid file
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Removed zero-size file: {file_path}")
            except OSError as e:
                logger.error(f"cleanup_error: Failed to remove zero-size file {file_path}: {e}")
        # Raise an error to indicate download failure
        # Raise specific error
        raise ZeroSizeFileError(f"Downloaded file {file_unique_id} for {channel}/{post_id} is zero size or missing after download attempt.")

    logger.info(f"Downloaded media file {file_unique_id} to {cache_path}")
    return file_path, False


async def remove_old_cached_files(media_files: list, cache_dir: str) -> tuple[list, int]:
    """
    Remove files that haven't been accessed for more than 20 days
    Returns tuple of (updated media files list, number of removed files)
    """
    current_time = datetime.now().timestamp()
    updated_media_files = []
    files_removed = 0

    for file_data in media_files:
        try:
            channel = file_data.get('channel')
            post_id = file_data.get('post_id')
            file_unique_id = file_data.get('file_unique_id')
            
            if not all([channel, post_id, file_unique_id]):
                continue

            last_access_time = file_data.get('added', 0)
            days_since_access = (current_time - last_access_time) / (24 * 3600)

            if days_since_access > 20:
                cache_path = os.path.join(cache_dir, str(channel), str(post_id), file_unique_id)
                
                if os.path.exists(cache_path):
                    try:
                        os.remove(cache_path)
                        # try to remove empty parent directories
                        post_dir = os.path.dirname(cache_path)
                        channel_dir = os.path.dirname(post_dir)
                        if not os.listdir(post_dir):
                            os.rmdir(post_dir)
                        if not os.listdir(channel_dir):
                            os.rmdir(channel_dir)
                            
                        files_removed += 1
                        logger.info(f"Removed old cached file: {cache_path}, last access {days_since_access:.1f} days ago")
                    except Exception as e:
                        logger.error(f"Failed to remove cached file {cache_path}: {str(e)}")
                        updated_media_files.append(file_data)
                continue

            updated_media_files.append(file_data)

        except Exception as e:
            logger.error(f"Error processing cache entry {file_data}: {str(e)}")
            continue

    # Additionally, remove temporary files with prefix "temp_" if they are older than a threshold (1 hour)
    temp_threshold = 3600  # 1 hour in seconds
    for root, _, files in os.walk(cache_dir):
        for file in files:
            if file.startswith("temp_"):
                file_path = os.path.join(root, file)
                try:
                    file_mod_time = os.path.getmtime(file_path)
                    if time.time() - file_mod_time > temp_threshold:
                        # Get channel/post/file_id from path
                        rel_path = os.path.relpath(os.path.dirname(file_path), cache_dir)
                        channel, post_id = rel_path.split(os.sep)
                        file_unique_id = file[5:]  # Remove 'temp_' prefix
                        
                        # Remove file
                        os.remove(file_path)
                        files_removed += 1
                        logger.info(f"Removed temporary file: {file_path}")
                        
                        # Also remove entry from media_file_ids.json if exists
                        updated_media_files = [
                            f for f in updated_media_files 
                            if not (f.get('channel') == channel and 
                                f.get('post_id') == int(post_id) and 
                                f.get('file_unique_id') == file_unique_id)
                        ]
                        
                except Exception as e:
                    logger.error(f"Failed to remove temporary file {file_path}: {str(e)}")

    return updated_media_files, files_removed


async def download_new_files(media_files: list, cache_dir: str):
    """
    Download files that are not in cache yet
    """
    if not media_files:
        logger.info("No media files found for download")
        return

    files_to_download = 0
    for file_data in media_files:
        try:
            channel = file_data.get('channel')
            post_id = file_data.get('post_id')
            file_unique_id = file_data.get('file_unique_id')
            
            if not all([channel, post_id, file_unique_id]):
                logger.error(f"Invalid file data: {file_data}")
                continue
                
            channel_dir = os.path.join(cache_dir, str(channel))
            post_dir = os.path.join(channel_dir, str(post_id))
            os.makedirs(post_dir, exist_ok=True)
            
            # Skip if temp file exists (large videos)
            temp_path = os.path.join(post_dir, f"temp_{file_unique_id}")
            if os.path.exists(temp_path):
                continue
                
            cache_path = os.path.join(post_dir, file_unique_id)
            if not os.path.exists(cache_path):
                files_to_download += 1
                logger.debug(f"Background download started for {channel}/{post_id}/{file_unique_id}")
                await download_media_file(channel, post_id, file_unique_id)
                await asyncio.sleep(1)  # Delay between downloads
        
        except Exception as e:
            logger.error(f"Background download failed for {channel}/{post_id}/{file_unique_id}: {str(e)}")
            continue

    if files_to_download == 0:
        logger.info("All media files are already in cache")


def fix_corrupted_json(file_path: str) -> list:
    """
    Attempt to fix corrupted JSON file using json-repair library
    Returns list of valid entries
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # Try to repair and parse the JSON
        fixed_data = json_repair.loads(content)
        
        # Validate entries
        valid_entries = []
        for entry in fixed_data:
            if isinstance(entry, dict) and all(key in entry for key in ['channel', 'post_id', 'file_unique_id']):
                valid_entries.append(entry)
                
        logger.info(f"Fixed JSON file: {file_path}, found {len(valid_entries)} valid entries")
        return valid_entries
        
    except Exception as e:
        logger.error(f"Failed to fix JSON file {file_path}: {str(e)}")
        return []

async def cache_media_files():
    """Background task for cache management: removes old files and downloads new ones"""
    delay = 60
    while True:
        try:
            file_path = os.path.join(os.path.abspath("./data"), 'media_file_ids.json')
            if not os.path.exists(file_path):
                await asyncio.sleep(delay)
                continue

            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    media_files = json.load(f)
            except json.JSONDecodeError:
                logger.error(f"JSON decode error in {file_path}, attempting to fix")
                media_files = fix_corrupted_json(file_path)
                if media_files:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(media_files, f, ensure_ascii=False, indent=2)
                else:
                    logger.error("Failed to fix JSON file, skipping cache update")
                    await asyncio.sleep(delay)
                    continue

            cache_dir = os.path.abspath("./data/cache")
            updated_media_files, files_removed = await remove_old_cached_files(media_files, cache_dir)

            if files_removed > 0:
                try:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump(updated_media_files, f, ensure_ascii=False, indent=2)
                    logger.info(f"Removed {files_removed} old files from cache")
                except Exception as e:
                    logger.error(f"Failed to update media_file_ids.json: {str(e)}")

            await download_new_files(updated_media_files, cache_dir)
            await asyncio.sleep(delay)  # Check every delay seconds
            
        except Exception as e:
            logger.error(f"Cache media files error: {str(e)}")
            await asyncio.sleep(delay)


def calculate_cache_stats():
    """
    Calculate cache statistics including file count, total size in MB, and time difference in days.
    Returns a dictionary with keys: 'cache_files_count', 'cache_total_size_mb', 'cache_time_diff_days', 'channels'.
    """
    base_cache_dir = os.path.abspath("./data/cache")
    cache_files_count = 0
    cache_total_size_bytes = 0
    channels_stats = {}
    
    if os.path.isdir(base_cache_dir):
        # Recursively walk through all subdirectories
        for root, _, files in os.walk(base_cache_dir):
            for f in files:
                file_path = os.path.join(root, f)
                file_size = os.path.getsize(file_path)
                cache_files_count += 1
                cache_total_size_bytes += file_size
                
                # Calculate per-channel statistics
                rel_path = os.path.relpath(root, base_cache_dir)
                channel = rel_path.split(os.sep, maxsplit=1)[0]  # First directory is channel
                
                if channel not in channels_stats:
                    channels_stats[channel] = {
                        'files_count': 0,
                        'size_mb': 0
                    }
                
                channels_stats[channel]['files_count'] += 1
                channels_stats[channel]['size_mb'] = round(
                    channels_stats[channel]['size_mb'] + (file_size / (1024 * 1024)), 2
                )
                    
        cache_total_size_mb = round(cache_total_size_bytes / (1024 * 1024), 2)  # rounded size in MB
    else:
        cache_files_count = 0
        cache_total_size_mb = 0

    media_file_ids_path = os.path.join(os.path.abspath("./data"), "media_file_ids.json")
    cache_times = []
    if os.path.exists(media_file_ids_path):
        try:
            with open(media_file_ids_path, "r", encoding="utf-8") as f:
                media_files = json.load(f)
            for entry in media_files:
                if "added" in entry:
                    cache_times.append(entry["added"])
        except Exception as e:
            logger.error(f"Error reading media_file_ids.json: {str(e)}")
    if cache_times:
        cache_time_diff_seconds = max(cache_times) - min(cache_times)
        cache_time_diff_days = round(cache_time_diff_seconds / 86400, 2)  # rounded to two decimals
    else:
        cache_time_diff_days = 0

    return {
        "cache_files_count": cache_files_count,
        "cache_total_size_mb": cache_total_size_mb,
        "cache_time_diff_days": cache_time_diff_days,
        "channels": channels_stats
    }


@app.get("/html/{channel}/{post_id}", response_class=HTMLResponse)
@app.get("/post/html/{channel}/{post_id}", response_class=HTMLResponse)
@app.get("/html/{channel}/{post_id}/{token}", response_class=HTMLResponse)  
@app.get("/post/html/{channel}/{post_id}/{token}", response_class=HTMLResponse)
async def get_post_html(channel: str, post_id: int, token: str | None = None, debug: bool = False, request: Request = None):
    if Config["token"] and request and request.client.host not in ["127.0.0.1", "localhost"]:
        if token != Config["token"]:
            logger.error(f"Invalid token for HTML post: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for HTML post: {token}")
            
    try:
        parser = PostParser(client.client)
        html_content = await parser.get_post(channel, post_id, 'html', debug)
        if not html_content:
            raise HTTPException(status_code=404, detail="Post not found")
        return html_content
    except Exception as e:
        error_message = f"Failed to get HTML post for channel {channel}, post_id {post_id}: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e


@app.get("/json/{channel}/{post_id}")
@app.get("/post/json/{channel}/{post_id}")
@app.get("/json/{channel}/{post_id}/{token}")
@app.get("/post/json/{channel}/{post_id}/{token}")
async def get_post(channel: str, post_id: int, token: str | None = None, debug: bool = False, request: Request = None):
    if Config["token"] and request and request.client.host not in ["127.0.0.1", "localhost"]:
        if token != Config["token"]:
            logger.error(f"Invalid token for JSON post: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for JSON post: {token}")
            
    try:
        parser = PostParser(client.client)
        json_content = await parser.get_post(channel, post_id, 'json', debug)
        if not json_content:
            raise HTTPException(status_code=404, detail="Post not found")
        return json_content
    except Exception as e:
        error_message = f"Failed to get JSON post for channel {channel}, post_id {post_id}: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e


@app.get("/raw_json/{channel}/{post_id}")
@app.get("/raw_json/{channel}/{post_id}/{token}")
async def get_raw_post_json(channel: str, post_id: int, token: str | None = None, request: Request = None):
    if Config["token"] and request and request.client.host not in ["127.0.0.1", "localhost"]:
        if token != Config["token"]:
            logger.error(f"Invalid token for raw JSON post: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for raw JSON post: {token}")
            
    try:
        # Convert numeric channel ID to int if needed
        channel_id = channel
        if isinstance(channel, str) and channel.startswith('-100'):
            channel_id = int(channel)
            
        message = await client.client.get_messages(channel_id, post_id)
        if not message:
            raise HTTPException(status_code=404, detail="Post not found")
            
        # Return message as plain text using Pyrogram's built-in string representation
        return Response(content=str(message), media_type="text/plain")
    except Exception as e:
        error_message = f"Failed to get raw JSON post for channel {channel}, post_id {post_id}: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e


@app.get("/health")
@app.get("/health/{token}")
async def health_check(token: str | None = None, request: Request = None):
    if Config["token"] and request and request.client.host not in ["127.0.0.1", "localhost"]:
        if token != Config["token"]:
            logger.error(f"Invalid token for health check: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for health check: {token}")
            
    try:
        me = await client.client.get_me()

        cache_stats = calculate_cache_stats()
        
        config_info = {}    
        for config_key, config_value in Config.items():
            if any(sensitive in config_key.lower() for sensitive in ['token', 'tg_api_id', 'tg_api_hash']):
                config_info[config_key] = mask_sensitive_value(str(config_value)) if config_value else None
            else:
                config_info[config_key] = config_value

        return {
            "status": "ok",
            "tg_connected": client.client.is_connected,
            "tg_name": me.username,
            "tg_id": me.id,
            "tg_phone": me.phone_number,
            "tg_first_name": me.first_name,
            "tg_last_name": me.last_name,
            "config": config_info,
            **cache_stats
        }
    except Exception as e:
        error_message = f"Failed to get health check: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e

@app.get("/media/{channel}/{post_id}/{file_unique_id}/{digest}")
@app.get("/media/{channel}/{post_id}/{file_unique_id}")
async def get_media(channel: str, post_id: int, file_unique_id: str, digest: str | None = None):
    try:
        url = f"{channel}/{post_id}/{file_unique_id}"
        if not verify_media_digest(url, digest):
            expected_digest = generate_media_digest(url)
            logger.error(f"Invalid digest for media {url}: {digest}, expected: {expected_digest}")
            raise HTTPException(status_code=403, detail="Invalid URL signature")
        else:
            logger.info(f"Valid digest for media {url}: {digest}")   
            
        # Convert numeric channel ID to int if needed
        channel_id = channel
        if isinstance(channel, str) and channel.startswith('-100'):
            channel_id = int(channel)
            
        try: # Wrap the download and prepare call
            file_path, delete_after = await download_media_file(channel_id, post_id, file_unique_id)
            return await prepare_file_response(file_path, delete_after=delete_after)
        except ZeroSizeFileError as e: # Catch zero-size file errors
            logger.warning(f"zero_size_file_encountered: {str(e)}. Instructing client to retry.")
            return Response(
                status_code=503, # Service Unavailable
                content="File processing resulted in zero size, please try again in 10 seconds.",
                headers={"Retry-After": "10"}
            )
            
    except HTTPException:
        raise
    except errors.RPCError as e:
        logger.error(f"Media request RPC error for {channel}/{post_id}/{file_unique_id}: {type(e).__name__} - {str(e)}")
        raise HTTPException(status_code=404, detail="File not found in Telegram") from e
    except ZeroSizeFileError as e: # Catch zero-size file errors FROM DOWNLOAD
        error_message = f"Failed to obtain valid media file for {channel}/{post_id}/{file_unique_id} after download attempt: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e
    except Exception as e:
        error_message = f"Failed to get media for {channel}/{post_id}/{file_unique_id}: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e

@app.get("/rss/{channel}", response_class=Response)
@app.get("/rss/{channel}/{token}", response_class=Response)
async def get_rss_feed(channel: str, 
                        token: str | None = None, 
                        limit: int = 50, 
                        output_type: str = 'rss', 
                        exclude_flags: str | None = None,
                        exclude_text: str | None = None,
                        merge_seconds: int = 5,
                        request: Request = None
                        ):
    if Config["token"] and request and request.client.host not in ["127.0.0.1", "localhost"]:
        if token != Config["token"]:
            logger.error(f"invalid_token_error: token {token}, expected {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
    while True:
        try:

            if output_type == 'rss':
                rss_content = await generate_channel_rss(channel,
                                                        client=client.client, 
                                                        limit=limit, 
                                                        exclude_flags=exclude_flags,
                                                        exclude_text=exclude_text,
                                                        merge_seconds=merge_seconds)
                return Response(content=rss_content, media_type="application/xml")
            elif output_type == 'html':
                rss_content = await generate_channel_html(channel,
                                                        client=client.client, 
                                                        limit=limit, 
                                                        exclude_flags=exclude_flags,
                                                        exclude_text=exclude_text,
                                                        merge_seconds=merge_seconds)
                return Response(content=rss_content, media_type="text/html")
        except ValueError as e:
            error_message = f"invalid_parameters_error: {str(e)}"
            logger.error(error_message)
            raise HTTPException(status_code=400, detail=error_message) from e
        except errors.FloodWait as e:
            wait_time = e.value
            random_additional_wait = random.uniform(0, wait_time * 2.5)
            total_wait_time = wait_time + random_additional_wait
            if total_wait_time > 190: total_wait_time = 190

            logger.warning(f"flood_wait_error: channel {channel}, retry after {total_wait_time:.1f} seconds (base: {wait_time}s, random: {random_additional_wait:.1f}s)")
            return Response(
                status_code=429,
                headers={"Retry-After": str(int(total_wait_time))},
                content="Too many requests, please try again later"
            )
        except Exception as e:
            error_message = f"rss_generation_error: channel {channel}, error {str(e)}"
            logger.error(error_message)
            raise HTTPException(status_code=500, detail=error_message) from e 

@app.get("/flags", response_model=List[str])
@app.get("/flags/{token}", response_model=List[str])
async def get_available_flags(token: str | None = None, request: Request = None):
    """Returns a list of all possible flags that can be assigned to posts."""
    if Config["token"] and request and request.client.host not in ["127.0.0.1", "localhost"]:
        if token != Config["token"]:
            logger.error(f"Invalid token for flags endpoint: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for flags endpoint: {token}")

    try:
        flags = PostParser.get_all_possible_flags()
        return flags
    except Exception as e:
        error_message = f"Failed to get flags list: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e 
