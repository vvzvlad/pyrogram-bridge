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
        text = message.text.html if message.text else ""  # Get HTML formatted text
        
        # Try to get caption for media messages
        if not text and hasattr(message, "caption") and message.caption:
            text = message.caption.html
        
        # Try to get text from other attributes
        if not text and hasattr(message, "action"):
            text = str(message.action)
        elif not text and hasattr(message, "entities"):
            # Extract HTML from entities
            text = message.text.html if message.text else ""
        
        # Parse media and add video markers
        media = []
        if message.media:
            # Handle animations directly from message object
            if hasattr(message, "animation") and message.animation:
                animation = message.animation
                media.append(await self._parse_animation(animation))
            # Handle videos directly from message object
            elif hasattr(message, "video") and message.video:
                video = message.video
                media.append(await self._parse_media(video))
            # Handle photos directly from message object
            elif hasattr(message, "photo") and message.photo:
                photo = message.photo
                media.append(await self._parse_media(photo))
            else:
                media_obj = message.media
                parsed_media = await self._parse_media(media_obj)
                if parsed_media["type"] not in ["webpage", "nonetype"]:
                    media.append(parsed_media)
                    logger.debug(f"Parsed media: {parsed_media}")

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

        # Add media previews to text
        if media:
            previews = []
            for m in media:
                if m.get('thumbnail'):
                    previews.append(f"<img src='data:image/jpeg;base64,{m['thumbnail']}' style='max-width: 600px; margin: 10px 0;'>")
            if previews:
                text = "<br>".join(previews) + "<br><br>" + text

        # Convert newlines to HTML breaks AFTER adding reactions
        text = text.replace('\n', '<br>')

        return {
            "id": message.id,
            "date": message.date.isoformat(),
            "text": text,
            "views": message.views or 0
        }

    async def _parse_media(self, media_obj) -> dict:
        # Handle different media types
        media_type = media_obj.__class__.__name__.lower()
        logger.debug(f"Parsing media type: {media_type}")
        
        # Get file_id directly from media object
        file_id = ""
        if hasattr(media_obj, "file_id"):
            file_id = media_obj.file_id
        
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
        elif media_type == "animation":
            # Process animation (GIF/Video)
            logger.debug(f"Animation details: {media_obj}")
            logger.debug(f"Animation file_id: {media_obj.file_id}")
            logger.debug(f"Animation thumbs: {media_obj.thumbs}")
            if media_obj.thumbs:
                try:
                    thumb_path = await self.client.download_media(media_obj.thumbs[0].file_id)
                    with open(thumb_path, "rb") as image_file:
                        thumbnail_base64 = base64.b64encode(image_file.read()).decode("utf-8")
                    os.remove(thumb_path)
                except Exception as e:
                    logger.error(f"Animation thumbnail error: {e.__class__.__name__} - {str(e)}")
            
            return {
                "type": "animation",
                "url": media_obj.file_id,
                "size": media_obj.file_size,
                "duration": media_obj.duration,
                "thumbnail": thumbnail_base64
            }
        elif media_type == "messagemediatype":
            # Handle Telegram's internal media type
            actual_type = str(media_obj).split('.')[-1].lower()
            logger.debug(f"Resolved MessageMediaType: {actual_type}")
            return {"type": actual_type}
        elif media_type == "photo":
            # Download and encode full photo
            if hasattr(media_obj, "file_id"):
                try:
                    # Download full size photo
                    photo_path = await self.client.download_media(media_obj.file_id)
                    with open(photo_path, "rb") as image_file:
                        thumbnail_base64 = base64.b64encode(image_file.read()).decode("utf-8")
                    os.remove(photo_path)
                except Exception as e:
                    logger.error(f"Photo download error: {e.__class__.__name__} - {str(e)}")
            
            return {
                "type": "photo",
                "url": file_id,
                "size": media_obj.file_size,
                "thumbnail": thumbnail_base64
            }
        
        return {
            "type": media_type,
            "url": file_id,
            "size": getattr(media_obj, "file_size", None),
            "thumbnail": None
        }

    async def _parse_animation(self, animation_obj) -> dict:
        logger.debug(f"Parsing animation: {animation_obj}")
        if not animation_obj or not hasattr(animation_obj, 'thumbs'):
            logger.error("Invalid animation object")
            return {"type": "animation", "error": "invalid_object"}
        
        try:
            # Check if thumbs exist and have items
            if not animation_obj.thumbs or len(animation_obj.thumbs) == 0:
                raise ValueError("No thumbnails available")
            
            thumb_file_id = animation_obj.thumbs[0].file_id
            thumb_path = await self.client.download_media(thumb_file_id)
            with open(thumb_path, "rb") as f:
                thumbnail = base64.b64encode(f.read()).decode()
            os.remove(thumb_path)
        except Exception as e:
            logger.error(f"Animation error: {str(e)}")
            thumbnail = None
        
        return {
            "type": "animation",
            "url": getattr(animation_obj, "file_id", ""),
            "size": animation_obj.file_size,
            "duration": animation_obj.duration,
            "thumbnail": thumbnail
        }

    async def get_post(self, channel: str, post_id: int) -> dict:
        try:
            message = await self.client.get_messages(
                chat_id=channel,
                message_ids=post_id
            )
            if not message or message.empty:
                return {
                    "error": "message_not_found",
                    "details": f"Message {post_id} in {channel} does not exist"
                }
            
            # Debug raw message structure
            print("Raw message object: %s", message)
            
            return await self._parse_message(message)
        except errors.RPCError as e:
            logger.error(f"Telegram API error: {e.__class__.__name__} - {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in get_post: {str(e)}")
            raise 