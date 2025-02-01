import logging
from pyrogram import Client, errors
from pyrogram.types import Message
from config import get_settings
import base64
import os

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

    async def _parse_message(self, message: Message) -> dict:
        # Extract text from different sources
        text = message.text or ""
        
        # Try to get caption for media messages
        if not text and hasattr(message, "caption"):
            text = message.caption or ""
        
        # Try to get text from other attributes
        if not text and hasattr(message, "action"):
            text = str(message.action)  # For service messages
        
        # Parse media and add video markers
        media = []
        if message.media:
            media_obj = message.media
            if hasattr(message, "video"):
                media.append(await self._parse_media(message.video))
            else:
                if isinstance(media_obj, list):
                    for m in media_obj:
                        media.append(await self._parse_media(m))
                elif hasattr(media_obj, "photo"):
                    media.append(await self._parse_media(media_obj.photo))
                else:
                    media.append(await self._parse_media(media_obj))

        video_count = sum(1 for m in media if m.get("type") == "video")
        if video_count > 0:
            text = f"[{'Video' if video_count == 1 else f'{video_count} Videos'}] {text}" if text else f"[{'Video' if video_count == 1 else f'{video_count} Videos'}]"
        
        # Always add reactions if present
        reactions_text = ""
        if getattr(message, "reactions", None):
            reactions_text = "\nReactions: " + ", ".join(
                f"{r.emoji}({r.count})"
                for r in message.reactions.reactions
            )
        text += reactions_text

        return {
            "id": message.id,
            "date": message.date.isoformat(),
            "text": text,
            "media": media,
            "views": message.views or 0,
            "reactions": [r.emoji for r in message.reactions.reactions] if getattr(message, "reactions", None) else []
        }

    async def _parse_media(self, media_obj) -> dict:
        # Handle different media types
        media_type = media_obj.__class__.__name__.lower()
        file_id = getattr(media_obj, "file_id", "")
        thumbnail_base64 = None
        
        # Special cases
        if media_type == "photofile":
            file_id = getattr(media_obj, "photo_file_id", file_id)
        elif media_type == "webpage":
            return {"type": "webpage", "url": getattr(media_obj, "url", "")}
        elif media_type == "video":
            # Download and encode thumbnail
            if media_obj.thumbs:
                try:
                    thumb_path = await self.client.download_media(media_obj.thumbs[0].file_id)
                    with open(thumb_path, "rb") as image_file:
                        thumbnail_base64 = base64.b64encode(image_file.read()).decode("utf-8")
                    os.remove(thumb_path)
                except Exception as e:
                    logger.error(f"Thumbnail error: {e.__class__.__name__} - {str(e)}")
            
            return {
                "type": "video",
                "url": file_id,
                "size": media_obj.file_size,
                "duration": media_obj.duration,
                "thumbnail": thumbnail_base64
            }
        
        return {
            "type": media_type,
            "url": str(file_id),
            "size": getattr(media_obj, "file_size", None),
            "thumbnail": thumbnail_base64
        }

    async def get_post(self, channel: str, post_id: int) -> dict:
        try:
            message = await self.client.get_messages(
                chat_id=channel,
                message_ids=post_id
            )
            if not message:
                raise ValueError("Message not found")
            
            # Debug raw message structure
            print("Raw message object: %s", message)
            print("Message attributes: %s", dir(message))
            print("Message media type: %s", type(message.media) if message.media else None)
            
            return await self._parse_message(message)
        except errors.RPCError as e:
            logger.error(f"Telegram API error: {e.__class__.__name__} - {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in get_post: {str(e)}")
            raise 