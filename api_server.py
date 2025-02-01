from fastapi import FastAPI, HTTPException, Response
from telegram_client import TelegramClient
from config import get_settings
import logging
import json

logger = logging.getLogger(__name__)
app = FastAPI(title="PyroTg Bridge")
client = TelegramClient()
settings = get_settings()

@app.on_event("startup")
async def startup():
    await client.start()

@app.on_event("shutdown")
async def shutdown():
    await client.stop()

@app.get("/post/{channel}/{post_id}")
async def get_post(channel: str, post_id: int):
    try:
        data = await client.get_post(channel, post_id)
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