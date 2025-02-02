import logging
from pyrogram import Client
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

class TelegramClient:
    def __init__(self):
        if settings["session_string"]:
            self.client = Client(
                name="pyro_bridge",
                session_string=settings["session_string"],
                api_id=settings["tg_api_id"],
                api_hash=settings["tg_api_hash"],
                in_memory=True
            )
        else:
            self.client = Client(
                name="pyro_bridge",
                api_id=settings["tg_api_id"],
                api_hash=settings["tg_api_hash"],
                workdir=settings["session_path"],
                in_memory=True
            )

    async def start(self):
        if not self.client.is_connected:
            await self.client.start()
            logger.info("Telegram client connected")

    async def stop(self):
        if self.client.is_connected:
            await self.client.stop()
            logger.info("Telegram client disconnected")
