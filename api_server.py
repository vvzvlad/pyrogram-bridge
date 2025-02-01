from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
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

@app.get("/html/{channel}/{post_id}", response_class=HTMLResponse)
async def get_post_html(channel: str, post_id: int):
    try:
        post = await client.get_post(channel, post_id)
        if post.get("error"):
            raise HTTPException(
                status_code=404,
                detail=post["details"]
            )
        return f"""
        <html>
            <head>
                <title>Post {post_id}</title>
                <style>
                    body {{ font-family: Arial; margin: 20px; }}
                    .poll {{ 
                        border: 1px solid #ddd; 
                        padding: 15px; 
                        margin: 20px 0;
                        border-radius: 8px;
                    }}
                    .poll-option {{ 
                        margin: 10px 0;
                    }}
                    .progress-bar {{
                        height: 20px;
                        background: #4CAF50;
                        border-radius: 4px;
                        margin: 5px 0;
                    }}
                    .stats {{ 
                        color: #666;
                        font-size: 0.9em;
                    }}
                    .total {{
                        margin-top: 15px;
                        font-weight: bold;
                    }}
                </style>
            </head>
            <body>
                {post['text']}
            </body>
        </html>
        """
    except Exception as e:
        logger.error(f"HTML endpoint error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

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