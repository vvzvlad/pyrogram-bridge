import logging
import json
import re

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from telegram_client import TelegramClient
from config import get_settings
import mimetypes
import os
from pyrogram import errors

logger = logging.getLogger(__name__)
client = TelegramClient()
settings = get_settings()

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Startup
    await client.start()
    yield
    # Shutdown
    await client.stop()

app = FastAPI(
    title="PyroTg Bridge",
    lifespan=lifespan
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api_server:app",
        host=settings["api_host"],
        port=settings["api_port"],
        reload=False
    )

@app.get("/html/{channel}/{post_id}", response_class=HTMLResponse)
async def get_post_html(channel: str, post_id: int):
    try:
        post = await client.get_post(channel, post_id)
        if post.get("error"):
            raise HTTPException(
                status_code=404,
                detail=post["details"]
            )
        if "error" in post:
            raise HTTPException(status_code=404, detail=post["details"])
        return post["html"]
    except Exception as e:
        logger.error(f"HTML endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e)) from e

@app.get("/json/{channel}/{post_id}")
async def get_post(channel: str, post_id: int):
    try:
        data = await client.get_post(channel, post_id)
        if data.get("error"):
            raise HTTPException(
                status_code=404,
                detail=data
            )
        return Response(
            content=json.dumps(data),
            media_type="application/json"
        )
    except Exception as e:
        logger.error(f"API error: {str(e)}")
        raise HTTPException(
            status_code=404,
            detail={
                "error": "post_retrieval_error",
                "message": str(e)
            }
        ) from e

@app.get("/status")
async def health_check():
    return {
        "status": "ok",
        "connected": client.client.is_connected
    }

@app.get("/media/{file_id}")
async def get_media(
    file_id: str,
    background_tasks: BackgroundTasks,
    response: Response
):
    try:
        file_path = await client.download_media_file(file_id)
        
        # Additional safety checks
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Temporary file not found")
        
        # Determine media type from file extension
        media_type, _ = mimetypes.guess_type(file_path)
        if not media_type:
            media_type = "application/octet-stream"
        
        # Add cleanup task
        background_tasks.add_task(lambda: os.remove(file_path) if os.path.exists(file_path) else None)
        
        return FileResponse(
            path=file_path,
            media_type=media_type,
            headers={
                "Content-Disposition": f"inline; filename={os.path.basename(file_path)}"
            }
        )
        
    except HTTPException:
        # Пробрасываем уже сформированные HTTPException как есть
        raise
    except errors.RPCError as e:
        logger.error(f"Media request RPC error {file_id}: {type(e).__name__} - {str(e)}")
        raise HTTPException(status_code=404, detail="File not found in Telegram")
    except Exception as e:
        logger.error(f"Media request failed {file_id}: {type(e).__name__} - {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Internal server error: {str(e)}"
        ) 