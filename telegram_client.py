import logging
from pyrogram import Client, errors
from pyrogram.types import Message
from config import get_settings
import base64
import os
import re

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
        # Debug raw message structure
        print(f"Raw message object: \n{message}")

        # Extract text from different sources
        raw_text = message.text.html if message.text else ""
        
        # Handle polls
        poll = getattr(message, "poll", None)
        if poll:
            try:
                question = getattr(poll, "question", "No question text")
                raw_text += f"\n📊 Poll: {poll.question}\n"
                if hasattr(poll, "options") and poll.options:
                    for i, option in enumerate(poll.options, 1):
                        raw_text += f"{i}. {getattr(option, 'text', '')}\n"
                raw_text += "\n→ Vote in Telegram 🔗\n"
            except Exception as e:
                logger.error(f"Error parsing poll: {str(e)}")
                raw_text += "\n[Error displaying poll]"
        
        # Try to get caption for media messages
        if not raw_text and hasattr(message, "caption") and message.caption:
            raw_text = message.caption.html
        
        # Try to get text from other attributes
        if not raw_text and hasattr(message, "action"):
            raw_text = str(message.action)
        elif not raw_text and hasattr(message, "entities"):
            # Extract HTML from entities
            raw_text = message.text.html if message.text else ""
        
        # Process text entities for links
        text_parts = []
        last_offset = 0
        entities = getattr(message, "entities", None)
        if entities and isinstance(entities, list):
            # Sort entities by offset
            entities = sorted(entities, key=lambda x: x.offset)
            
            for entity in entities:
                if entity.offset > last_offset:
                    text_parts.append(raw_text[last_offset:entity.offset])
                
                entity_text = raw_text[entity.offset:entity.offset+entity.length]
                
                if entity.type == "text_link":
                    text_parts.append(f"<a href='{entity.url}'>{entity_text}</a>")
                elif entity.type == "url":
                    text_parts.append(f"<a href='{entity_text}'>{entity_text}</a>")
                else:
                    text_parts.append(entity_text)
                
                last_offset = entity.offset + entity.length
            
            # Add remaining text
            if last_offset < len(raw_text):
                text_parts.append(raw_text[last_offset:])
            
            processed_text = "".join(text_parts)
        else:
            processed_text = raw_text

        # Replace only URLs not already in <a> tags
        processed_text = re.sub(
            r'(?<!href=")(https?://\S+)(?!">)',
            r'<a href="\1">\1</a>',
            processed_text
        )
        
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

        # Always add reactions if present
        reactions_text = ""
        if getattr(message, "reactions", None):
            reactions_text = "\nReactions: " + ", ".join(
                f"{r.emoji}({r.count})"
                for r in message.reactions.reactions
            )
        processed_text += reactions_text

        # Add media previews to text
        if media:
            previews = []
            for m in media:
                if m.get('thumbnail'):
                    media_type = m.get('type', 'media').upper()
                    overlay = f"<div style='position:absolute; top:5px; left:5px; background:rgba(0,0,0,0.7); color:white; padding:2px 5px; border-radius:3px; font-size:0.8em;'>{media_type}</div>"
                    previews.append(
                        f"<div style='position:relative; display:inline-block; margin:5px;'>"
                        f"<img src='data:image/jpeg;base64,{m['thumbnail']}' style='max-width:600px; max-height:600px; object-fit: contain;'>"
                        f"{overlay}"
                        f"</div>"
                    )
            if previews:
                previews_container = f"<div class='media-preview' style='white-space: nowrap; overflow-x: auto;'>{''.join(previews)}</div>"
                processed_text = previews_container + processed_text

        # Convert newlines to HTML breaks AFTER adding reactions
        processed_text = processed_text.replace('\n', '<br>')

        return {
            "id": message.id,
            "date": message.date.isoformat(),
            "text": processed_text,
            "views": message.views or 0,
            "media_group_id": message.media_group_id
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

    def _generate_title(self, raw_text: str) -> str:
        if not raw_text:
            return ""
        # Берем первую непустую строку
        first_line = next((line.strip() for line in raw_text.split('\n') if line.strip()), "")
        # Обрезаем до 50 символов и убираем HTML-теги
        clean_line = re.sub('<[^<]+?>', '', first_line)
        
        max_length = 50
        if len(clean_line) <= max_length:
            return clean_line.strip()
        
        # Обрезаем до последнего пробела
        trimmed = clean_line[:max_length]
        last_space = trimmed.rfind(' ')
        if last_space > 0:
            trimmed = trimmed[:last_space]
        
        return f"{trimmed.strip()}..." if trimmed else ""

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
            
            # Check for media group
            media_group = []
            if message.media_group_id:
                logger.debug(f"Processing media group {message.media_group_id} for message {post_id}")
                current_id = post_id
                # Collect previous messages in group
                while True:
                    prev_msg = await self.client.get_messages(
                        chat_id=channel,
                        message_ids=current_id - 1
                    )
                    if prev_msg and prev_msg.media_group_id == message.media_group_id:
                        logger.debug(f"Found previous message in group: {prev_msg.id}")
                        media_group.insert(0, prev_msg)
                        current_id -= 1
                    else:
                        break

                # Collect next messages in group
                current_id = post_id
                while True:
                    next_msg = await self.client.get_messages(
                        chat_id=channel,
                        message_ids=current_id + 1
                    )
                    if next_msg and next_msg.media_group_id == message.media_group_id:
                        logger.debug(f"Found next message in group: {next_msg.id}")
                        media_group.append(next_msg)
                        current_id += 1
                    else:
                        break

            # Process all messages in group
            all_messages = media_group + [message]
            logger.debug(f"Combined {len(all_messages)} messages in media group")
            parsed_messages = [await self._parse_message(msg) for msg in all_messages]

            # Combine results
            # Берем исходный текст из первого сообщения до обработки
            raw_text = message.text or message.caption or ""
            title = self._generate_title(raw_text)

            combined = {
                "id": post_id,
                "date": message.date.isoformat(),
                "title": title,
                "text": "".join([m["text"] for m in parsed_messages if m["text"]]),
                "views": max(m["views"] for m in parsed_messages),
                "media_group_id": message.media_group_id
            }
            logger.debug(f"Combined result: {combined}")

            return combined
        except errors.RPCError as e:
            logger.error(f"Telegram API error: {e.__class__.__name__} - {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in get_post: {str(e)}")
            raise 