import logging
import os
import mimetypes
import asyncio
import json
import magic
from datetime import datetime

from contextlib import asynccontextmanager
from pyrogram import errors
from fastapi import FastAPI, HTTPException, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from telegram_client import TelegramClient
from config import get_settings
from rss_generator import generate_channel_rss
from post_parser import PostParser

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
    
    # Start background task
    background_task = asyncio.create_task(cache_media_files())
    
    yield
    
    # Cleanup
    background_task.cancel()
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

async def prepare_file_response(file_path: str):
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
    return FileResponse(path=file_path, media_type=media_type, headers=headers)

async def download_media_file(channel: str, post_id: int, file_unique_id: str) -> str:
    """
    Download media file from Telegram and save to cache
    Returns path to downloaded file
    """
    cache_dir = os.path.abspath("./data/cache")
    cache_path = os.path.join(cache_dir, f"{channel}-{post_id}-{file_unique_id}")
    os.makedirs(cache_dir, exist_ok=True)

    if os.path.exists(cache_path):
        logger.info(f"Found cached media file: {cache_path}")
        # Update timestamp for existing file
        file_id = f"{channel}-{post_id}-{file_unique_id}"
        file_path = os.path.join(os.path.abspath("./data"), 'media_file_ids.json')
        try:
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    media_files = json.load(f)
                for file_data in media_files:
                    if file_data['file_id'] == file_id:
                        file_data['added'] = datetime.now().timestamp()
                        break
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(media_files, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to update timestamp for {file_id}: {str(e)}")
        return cache_path

    message = await client.client.get_messages(channel, post_id)
    file_id = await find_file_id_in_message(message, file_unique_id)
            
    if not file_id:
        logger.error(f"Media with file_unique_id {file_unique_id} not found in message {post_id}")
        raise HTTPException(status_code=404, detail="File not found in message")
        
    file_path = await client.client.download_media(file_id, file_name=cache_path)
    logger.info(f"Downloaded media file {file_unique_id} to {cache_path}")
    return file_path


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

            channel, post_id_str, file_unique_id = file_id.split('-', 2)
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



@app.get("/html/{channel}/{post_id}", response_class=HTMLResponse)
@app.get("/post/html/{channel}/{post_id}", response_class=HTMLResponse)
async def get_post_html(channel: str, post_id: int):
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
async def get_post(channel: str, post_id: int):
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
async def health_check():
    try:
        me = await client.client.get_me()
        return {
            "status": "ok",
            "tg_connected": client.client.is_connected,
            "tg_name": me.username,
            "tg_id": me.id,
            "tg_phone": me.phone_number,
            "tg_first_name": me.first_name,
            "tg_last_name": me.last_name,
        }
    except Exception as e:
        error_message = f"Failed to get health check: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e

@app.get("/media/{channel}/{post_id}/{file_unique_id}")
async def get_media(channel: str, post_id: int, file_unique_id: str):
    try:
        file_path = await download_media_file(channel, post_id, file_unique_id)
        return await prepare_file_response(file_path)
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
async def get_rss_feed(channel: str, limit: int = 20):
    try:
        rss_content = await generate_channel_rss(channel, client=client.client, limit=limit)
        return Response(content=rss_content, media_type="application/xml")
    except ValueError as e:
        error_message = f"Invalid parameters for RSS feed generation: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=400, detail=error_message) from e
    except Exception as e:
        error_message = f"Failed to generate RSS feed for channel {channel}: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e 
