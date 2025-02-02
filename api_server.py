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

@app.get("/media/{file_id}")
async def get_media(file_id: str, background_tasks: BackgroundTasks):
    try:
        file_path = await client.download_media_file(file_id) 
        logger.info(f"Downloaded media file {file_id} to {file_path}")
        if not os.path.exists(file_path): raise HTTPException(status_code=404, detail="Temporary file not found")
        
        media_type, _ = mimetypes.guess_type(file_path) # Determine media type from file extension
        if not media_type: media_type = "application/octet-stream"
        
        background_tasks.add_task(lambda: os.remove(file_path) if os.path.exists(file_path) else None)
        
        headers = { "Content-Disposition": f"inline; filename={os.path.basename(file_path)}" }
        return FileResponse( path=file_path, media_type=media_type, headers=headers)
    except HTTPException:
        raise
    except errors.RPCError as e:
        logger.error(f"Media request RPC error {file_id}: {type(e).__name__} - {str(e)}")
        raise HTTPException(status_code=404, detail="File not found in Telegram") from e
    except Exception as e:
        error_message = f"Failed to get media for file_id {file_id}: {str(e)}"
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
