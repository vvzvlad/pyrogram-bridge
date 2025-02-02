import logging
import os
import mimetypes

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse
from telegram_client import TelegramClient
from config import get_settings
from rss_generator import generate_channel_rss
from pyrogram import errors
from post_parser import PostParser

logger = logging.getLogger(__name__)
client = TelegramClient()
Config = get_settings()

@asynccontextmanager
async def lifespan(_: FastAPI):
    await client.start()
    yield
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

async def prepare_file_response(file_path: str, background_tasks: BackgroundTasks):
    """Prepare file response with proper headers and cleanup task"""
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Temporary file not found")
    
    media_type, _ = mimetypes.guess_type(file_path)
    if not media_type:
        media_type = "application/octet-stream"
    
    background_tasks.add_task(lambda: os.remove(file_path) if os.path.exists(file_path) else None)
    
    headers = {"Content-Disposition": f"inline; filename={os.path.basename(file_path)}"}
    return FileResponse(path=file_path, media_type=media_type, headers=headers)


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
async def get_media(channel: str, post_id: int, file_unique_id: str, background_tasks: BackgroundTasks):
    try:
        message = await client.client.get_messages(channel, post_id)
        file_id = await find_file_id_in_message(message, file_unique_id)
                
        if not file_id:
            logger.error(f"Media with file_unique_id {file_unique_id} not found in message {post_id}")
            raise HTTPException(status_code=404, detail="File not found in message")
        file_path = await client.client.download_media(file_id)
        logger.info(f"Downloaded media file {file_unique_id} to {file_path}")
        return await prepare_file_response(file_path, background_tasks)
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
