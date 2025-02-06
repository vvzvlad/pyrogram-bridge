import logging
import os
import mimetypes
import asyncio
import json
from datetime import datetime
import time
from contextlib import asynccontextmanager
import random

import magic
from pyrogram import errors
from starlette.background import BackgroundTask  # imported for background file deletion
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, FileResponse
from telegram_client import TelegramClient
from config import get_settings
from rss_generator import generate_channel_rss, generate_channel_html
from post_parser import PostParser
from url_signer import verify_media_digest

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
    await client.start()
    background_task = asyncio.create_task(cache_media_files()) # Start background task
    yield
    background_task.cancel() # Cleanup
    try:
        await background_task
    except asyncio.CancelledError:
        pass
    await client.stop()

app = FastAPI( title="Pyrogram Bridge", lifespan=lifespan)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host=Config["api_host"], port=Config["api_port"], reload=True)


async def find_file_id_in_message(message, file_unique_id: str):
    """Find file_id by checking all possible media types in message"""
    if message.media == "MessageMediaType.POLL":
        logger.debug(f"Message {message.id} is a poll, skipping media search")
        return None
        
    if message.photo and message.photo.file_unique_id == file_unique_id:
        return message.photo.file_id
    elif message.video and message.video.file_unique_id == file_unique_id:
        return message.video.file_id
    elif message.animation and message.animation.file_unique_id == file_unique_id:
        return message.animation.file_id
    elif message.video_note and message.video_note.file_unique_id == file_unique_id:
        return message.video_note.file_id
    elif message.audio and message.audio.file_unique_id == file_unique_id:
        return message.audio.file_id
    elif message.voice and message.voice.file_unique_id == file_unique_id:
        return message.voice.file_id
    elif message.sticker and message.sticker.file_unique_id == file_unique_id:
        return message.sticker.file_id
    elif message.web_page and message.web_page.photo and message.web_page.photo.file_unique_id == file_unique_id:
        return message.web_page.photo.file_id
    elif message.document and message.document.file_unique_id == file_unique_id:
        return message.document.file_id
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
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    # Try to determine MIME type using python-magic first
    try:
        mime = magic.Magic(mime=True)
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
    cache_dir = os.path.abspath("./data/cache")
    os.makedirs(cache_dir, exist_ok=True)
    
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
        # For large video, download without permanent caching; use a temporary file.
        temp_file_path = os.path.join(cache_dir, f"temp_{channel}-{post_id}-{file_unique_id}")
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
    cache_path = os.path.join(cache_dir, f"{channel}-{post_id}-{file_unique_id}")
    if os.path.exists(cache_path):
        logger.info(f"Found cached media file: {cache_path}")
        file_id = f"{channel}-{post_id}-{file_unique_id}"
        file_ids_path = os.path.join(os.path.abspath("./data"), 'media_file_ids.json')
        try:
            if os.path.exists(file_ids_path):
                with open(file_ids_path, 'r', encoding='utf-8') as f:
                    media_files = json.load(f)
                for file_data in media_files:
                    if file_data['file_id'] == file_id:
                        file_data['added'] = datetime.now().timestamp()
                        break
                with open(file_ids_path, 'w', encoding='utf-8') as f:
                    json.dump(media_files, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to update timestamp for {file_id}: {str(e)}")
        return cache_path, False

    file_id = await find_file_id_in_message(message, file_unique_id)
    if not file_id:
        logger.error(f"Media with file_unique_id {file_unique_id} not found in message {post_id}")
        raise HTTPException(status_code=404, detail="File not found in message")
    file_path = await client.client.download_media(file_id, file_name=cache_path)
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
            file_id = file_data.get('file_id')
            if not file_id:
                continue

            last_access_time = file_data.get('added', 0)
            days_since_access = (current_time - last_access_time) / (24 * 3600)

            if days_since_access > 20:
                channel, post_id_str, file_unique_id = file_id.split('-', 2)
                cache_path = os.path.join(cache_dir, f"{channel}-{post_id_str}-{file_unique_id}")
                
                if os.path.exists(cache_path):
                    try:
                        os.remove(cache_path)
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
    for file in os.listdir(cache_dir):
        file_path = os.path.join(cache_dir, file)
        if os.path.isfile(file_path) and file.startswith("temp_"):
            try:
                file_mod_time = os.path.getmtime(file_path)
                if time.time() - file_mod_time > temp_threshold:
                    os.remove(file_path)
                    files_removed += 1
                    logger.info(f"Removed temporary file: {file_path}")
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
            file_id = file_data['file_id']
            if not file_id:
                continue

            # Split by last two hyphens to handle negative channel IDs correctly
            last_hyphen_pos = file_id.rindex('-')
            second_last_hyphen_pos = file_id.rindex('-', 0, last_hyphen_pos)
            
            channel = file_id[:second_last_hyphen_pos]
            post_id_str = file_id[second_last_hyphen_pos + 1:last_hyphen_pos]
            file_unique_id = file_id[last_hyphen_pos + 1:]
                
            post_id = int(post_id_str)
            cache_path = os.path.join(cache_dir, f"{channel}-{post_id}-{file_unique_id}")
            
            if not os.path.exists(cache_path):
                files_to_download += 1
                logger.debug(f"Background download started for {file_id}")
                await download_media_file(channel, post_id, file_unique_id)
                await asyncio.sleep(1)  # Delay between downloads
        
        except Exception as e:
            logger.error(f"Background download failed for {file_id}: {str(e)}")
            continue

    if files_to_download == 0:
        logger.info("All media files are already in cache")


async def cache_media_files():
    """Background task for cache management: removes old files and downloads new ones"""
    delay = 60
    while True:
        try:
            file_path = os.path.join(os.path.abspath("./data"), 'media_file_ids.json')
            if not os.path.exists(file_path):
                await asyncio.sleep(delay)
                continue

            with open(file_path, 'r', encoding='utf-8') as f:
                media_files = json.load(f)

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
    Returns a dictionary with keys: 'cache_files_count', 'cache_total_size_mb', 'cache_time_diff_days'.
    """
    cache_dir = os.path.abspath("./data/cache")
    if os.path.isdir(cache_dir):
        files = [f for f in os.listdir(cache_dir) if os.path.isfile(os.path.join(cache_dir, f))]
        cache_files_count = len(files)
        cache_total_size_bytes = sum(os.path.getsize(os.path.join(cache_dir, f)) for f in files)
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
        "cache_time_diff_days": cache_time_diff_days
    }


@app.get("/html/{channel}/{post_id}", response_class=HTMLResponse)
@app.get("/post/html/{channel}/{post_id}", response_class=HTMLResponse)
@app.get("/html/{channel}/{post_id}/{token}", response_class=HTMLResponse)  
@app.get("/post/html/{channel}/{post_id}/{token}", response_class=HTMLResponse)
async def get_post_html(channel: str, post_id: int, token: str | None = None):
    if Config["token"]:
        if token != Config["token"]:
            logger.error(f"Invalid token for HTML post: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for HTML post: {token}")
            
    try:
        parser = PostParser(client.client)
        html_content = await parser.get_post(channel, post_id, 'html')
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
async def get_post(channel: str, post_id: int, token: str | None = None):
    if Config["token"]:
        if token != Config["token"]:
            logger.error(f"Invalid token for JSON post: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for JSON post: {token}")
            
    try:
        parser = PostParser(client.client)
        json_content = await parser.get_post(channel, post_id, 'json')
        if not json_content:
            raise HTTPException(status_code=404, detail="Post not found")
        return json_content
    except Exception as e:
        error_message = f"Failed to get JSON post for channel {channel}, post_id {post_id}: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e


@app.get("/health")
@app.get("/health/{token}")
async def health_check(token: str | None = None):
    if Config["token"]:
        if token != Config["token"]:
            logger.error(f"Invalid token for health check: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for health check: {token}")
            
    try:
        me = await client.client.get_me()

        cache_stats = calculate_cache_stats()

        return {
            "status": "ok",
            "tg_connected": client.client.is_connected,
            "tg_name": me.username,
            "tg_id": me.id,
            "tg_phone": me.phone_number,
            "tg_first_name": me.first_name,
            "tg_last_name": me.last_name,
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
            logger.error(f"Invalid digest for media {url}: {digest}, expected: {digest}")
            #raise HTTPException(status_code=403, detail="Invalid URL signature")
        else:
            logger.info(f"Valid digest for media {url}: {digest}")   
            
        file_path, delete_after = await download_media_file(channel, post_id, file_unique_id)
        return await prepare_file_response(file_path, delete_after=delete_after)
    except HTTPException:
        raise
    except errors.RPCError as e:
        logger.error(f"Media request RPC error for file_unique_id {file_unique_id} in message {post_id}: {type(e).__name__} - {str(e)}")
        raise HTTPException(status_code=404, detail="File not found in Telegram") from e
    except Exception as e:
        error_message = f"Failed to get media for file_unique_id {file_unique_id} in message {post_id}: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e

@app.get("/rss/{channel}", response_class=Response)
@app.get("/rss/{channel}/{token}", response_class=Response)
async def get_rss_feed(channel: str, token: str | None = None, limit: int = 20, output_type: str = 'rss'):
    if Config["token"]:
        if token != Config["token"]:
            logger.error(f"Invalid token for RSS feed: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for RSS feed: {token}")
    
    while True:
        try:
            if output_type == 'rss':
                rss_content = await generate_channel_rss(channel, client=client.client, limit=limit)
                return Response(content=rss_content, media_type="application/xml")
            elif output_type == 'html':
                rss_content = await generate_channel_html(channel, client=client.client, limit=limit)
                return Response(content=rss_content, media_type="text/html")
        except ValueError as e:
            error_message = f"Invalid parameters for RSS feed generation: {str(e)}"
            logger.error(error_message)
            raise HTTPException(status_code=400, detail=error_message) from e
        except errors.FloodWait as e:
            wait_time = e.value
            random_additional_wait = random.uniform(0, wait_time * 1.5)
            total_wait_time = wait_time + random_additional_wait
            logger.warning(f"FloodWait detected for channel {channel}, waiting {total_wait_time:.1f} seconds (base: {wait_time}s, random: {random_additional_wait:.1f}s)")
            await asyncio.sleep(total_wait_time)
            logger.info(f"FloodWait finished for channel {channel}, retrying RSS feed generation")
            continue
        except Exception as e:
            error_message = f"Failed to generate RSS feed for channel {channel}: {str(e)}"
            logger.error(error_message)
            raise HTTPException(status_code=500, detail=error_message) from e 
