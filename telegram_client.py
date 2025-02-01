import logging
from pyrogram import Client, errors
from pyrogram.types import Message
from config import get_settings
import os
import re


#tests
#http://127.0.0.1:8000/html/DragorWW_space/114 — video
#http://127.0.0.1:8000/html/DragorWW_space/20 - many photos
#http://127.0.0.1:8000/html/DragorWW_space/58 - photos+video
#http://127.0.0.1:8000/html/DragorWW_space/44 - poll
#http://127.0.0.1:8000/html/DragorWW_space/46 - photo
#http://127.0.0.1:8000/html/DragorWW_space/49, http://127.0.0.1:8000/html/DragorWW_space/63  — webpage
#http://127.0.0.1:8000/html/deckru/826 - animation
#http://127.0.0.1:8000/html/DragorWW_space/61 — links


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
        
        # Replace only URLs not already in <a> tags
        processed_text = re.sub(
            r'(?<!href=")(https?://\S+)(?!">)',
            r'<a href="\1">\1</a>',
            raw_text
        )
        
        # Parse media and add video markers
        media = []
        if message.media:
            # Process web page with photo
            web_page = getattr(message, "web_page", None)
            if web_page and web_page.photo:
                # Add photo from web page regardless of type
                media.append({
                    "type": "webpage_photo",
                    "url": web_page.photo.file_id,
                    "thumbnail_file_id": web_page.photo.file_id
                })
            elif not (getattr(message, "poll", None)):
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
        raw_text += reactions_text

        # Get base URL from config
        base_url = settings["pyrogram_bridge_url"].rstrip('/')

        # Add media previews to text
        if media:
            previews = []
            for m in media:
                media_type = m.get('type', 'media')
                file_id = m.get('url')
                
                if media_type in ['video', 'animation']:
                    # Direct video/animation embedding
                    previews.append(
                        f"<div style='margin:5px;'>"
                        f"<video controls style='max-width:600px; max-height:600px;'>"
                        f"<source src='{base_url}/media/{file_id}' type='video/mp4'>"
                        f"Your browser does not support the video tag."
                        f"</video>"
                        f"</div>"
                    )
                elif m.get('thumbnail_file_id'):
                    # Image preview without type overlay
                    previews.append(
                        f"<div style='position:relative; display:inline-block; margin:5px;'>"
                        f"<img src='{base_url}/media/{m['thumbnail_file_id']}' style='max-width:600px; max-height:600px; object-fit: contain;'>"
                        f"</div>"
                    )

            if previews:
                previews_container = f"<div class='media-preview' style='white-space: nowrap; overflow-x: auto;'>{''.join(previews)}</div>"
                processed_text = previews_container + processed_text

        # Convert newlines to HTML breaks AFTER adding reactions
        processed_text = processed_text.replace('\n', '<br>')

        # Extract author information
        #author_str = ""
        #if message.sender_chat:
        #    title = getattr(message.sender_chat, "title", "").strip()
        #    username = getattr(message.sender_chat, "username", "").strip()
        #    author_str = f"{title} by @{username}" if username else title
        #elif message.from_user:
        #    first = getattr(message.from_user, "first_name", "").strip()
        #    last = getattr(message.from_user, "last_name", "").strip()
        #    username = getattr(message.from_user, "username", "").strip()
        #    name = " ".join(filter(None, [first, last]))
        #    author_str = f"{name} by @{username}" if username else name
        #else:
        #    author_str = "Unknown author"

        return {
            "text": re.sub('<[^<]+?>', '', raw_text).strip(),
            "views": message.views or 0,
            "media_group_id": message.media_group_id,
            "id": message.id,
            "date": message.date,
            "html": processed_text,
            "raw_text": raw_text,
            "media": media,
            "author": self._get_author_info(message)
        }

    async def _parse_media(self, media_obj) -> dict:
        # Handle different media types
        media_type = media_obj.__class__.__name__.lower()
        logger.debug(f"Parsing media type: {media_type}")
        
        # Get file_id directly from media object
        file_id = ""
        if hasattr(media_obj, "file_id"):
            file_id = media_obj.file_id
        
        thumbnail_file_id = None
        if media_obj.thumbs:
            thumbnail_file_id = media_obj.thumbs[0].file_id
        
        # Special cases
        if media_type == "photofile":
            file_id = getattr(media_obj, "photo_file_id", file_id)
        elif media_type == "webpage":
            # Handle web page with photo
            photo = getattr(media_obj, "photo", None)
            if photo and hasattr(photo, "file_id"):
                return {
                    "type": "webpage_photo",
                    "url": photo.file_id,
                    "thumbnail_file_id": photo.file_id,
                    "size": photo.file_size
                }
            return {"type": "webpage", "url": getattr(media_obj, "url", "")}
        elif media_type == "video":
            return {
                "type": "video",
                "url": file_id,
                "size": media_obj.file_size,
                "duration": media_obj.duration,
                "thumbnail_file_id": thumbnail_file_id
            }
        elif media_type == "animation":
            return {
                "type": "animation",
                "url": media_obj.file_id,
                "size": media_obj.file_size,
                "duration": media_obj.duration,
                "thumbnail_file_id": thumbnail_file_id
            }
        elif media_type == "messagemediatype":
            # Handle Telegram's internal media type
            actual_type = str(media_obj).rsplit('.', maxsplit=1)[-1].lower()
            logger.debug(f"Resolved MessageMediaType: {actual_type}")
            
            # Special handling for specific types
            if actual_type == "poll":
                return {"type": "poll", "url": None}
            elif actual_type == "web_page":
                return {"type": "webpage", "url": getattr(media_obj, "url", "")}
            
            return {"type": actual_type}
        elif media_type == "photo":
            return {
                "type": "photo",
                "url": file_id,
                "size": media_obj.file_size,
                "thumbnail_file_id": file_id
            }
        
        return {
            "type": media_type,
            "url": file_id,
            "size": getattr(media_obj, "file_size", None),
            "thumbnail_file_id": thumbnail_file_id
        }

    async def _parse_animation(self, animation_obj) -> dict:
        logger.debug(f"Parsing animation: {animation_obj}")
        if not animation_obj or not hasattr(animation_obj, 'thumbs'):
            logger.error("Invalid animation object")
            return {"type": "animation", "error": "invalid_object"}
        
        thumbnail_file_id = None
        if animation_obj.thumbs and len(animation_obj.thumbs) > 0:
            thumbnail_file_id = animation_obj.thumbs[0].file_id
        
        return {
            "type": "animation",
            "url": getattr(animation_obj, "file_id", ""),
            "size": animation_obj.file_size,
            "duration": animation_obj.duration,
            "thumbnail_file_id": thumbnail_file_id
        }

    def _generate_title(self, raw_text: str) -> str:
        if not raw_text:
            return ""
        # Get first non-empty line
        first_line = next((line.strip() for line in raw_text.split('\n') if line.strip()), "")
        # Trim and remove HTML tags
        clean_line = re.sub('<[^<]+?>', '', first_line)
        
        max_length = 65
        if len(clean_line) <= max_length:
            return clean_line.strip()
        
        # Cut to last space
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
            combined = {
                "id": post_id,
                "date": message.date.isoformat(),
                "title": self._generate_title(message.text or message.caption or ""),
                "raw": self._parse_raw_message(message),
                "html": "".join([m["html"] for m in parsed_messages if m["html"]]),
                "text": "".join([m["text"] for m in parsed_messages if m["text"]]),
                "views": max(m["views"] for m in parsed_messages),
                "media_group_id": message.media_group_id,
                "author": parsed_messages[0]["author"] if parsed_messages else ""
            }
            logger.debug(f"Combined result: {combined}")

            return combined
        except errors.RPCError as e:
            logger.error(f"Telegram API error: {e.__class__.__name__} - {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in get_post: {str(e)}")
            raise

    def _parse_raw_message(self, message: Message) -> dict:
        """Convert Pyrogram Message object to serializable dict"""
        return {
            "id": message.id,
            "date": message.date.isoformat(),
            "media_type": str(message.media).split('.')[-1].lower() if message.media else None,
            "text": message.text or message.caption,
            "author": self._get_author_info(message),
            "views": message.views,
            "reactions": self._parse_reactions(message),
            "media": self._parse_media_metadata(message),
            "sender_chat": self._parse_sender_chat(message),
            "edit_date": message.edit_date.isoformat() if message.edit_date else None,
            "forward_from": self._parse_forward_info(message),
            "media_group_id": message.media_group_id,
            "has_protected_content": message.has_protected_content
        }

    def _parse_sender_chat(self, message: Message) -> dict:
        if not message.sender_chat:
            return None
        return {
            "id": message.sender_chat.id,
            "title": getattr(message.sender_chat, "title", ""),
            "username": getattr(message.sender_chat, "username", ""),
            "photo": self._parse_chat_photo(message.sender_chat.photo)
        }

    def _parse_forward_info(self, message: Message) -> dict:
        if not message.forward_from_chat:
            return None
        return {
            "id": message.forward_from_chat.id,
            "title": getattr(message.forward_from_chat, "title", ""),
            "username": getattr(message.forward_from_chat, "username", "")
        }

    def _parse_chat_photo(self, photo) -> dict:
        if not photo:
            return None
        return {
            "small_file_id": photo.small_file_id,
            "big_file_id": photo.big_file_id
        }

    def _get_author_info(self, message: Message) -> dict:
        if message.sender_chat:
            return {
                "type": "channel",
                "title": getattr(message.sender_chat, "title", ""),
                "username": getattr(message.sender_chat, "username", "")
            }
        elif message.from_user:
            return {
                "type": "user",
                "first_name": getattr(message.from_user, "first_name", ""),
                "last_name": getattr(message.from_user, "last_name", ""),
                "username": getattr(message.from_user, "username", "")
            }
        return {"type": "unknown"}

    def _parse_reactions(self, message: Message) -> list:
        if not message.reactions:
            return []
        return [
            {
                "emoji": r.emoji,
                "count": r.count
            } for r in message.reactions.reactions
        ]

    def _parse_media_metadata(self, message: Message) -> dict:
        if not message.media:
            return {}
        return {
            "type": str(message.media).split('.')[-1].lower(),
            "file_id": getattr(message.media, "file_id", None),
            "file_size": getattr(message.media, "file_size", None),
            "duration": getattr(message.media, "duration", None),
            "photo": self._parse_photo_metadata(message.media) if hasattr(message.media, "photo") else None,
            "thumbs": self._parse_thumbs(message.media)
        }

    def _parse_photo_metadata(self, media_obj) -> dict:
        return {
            "width": media_obj.width,
            "height": media_obj.height,
            "file_size": media_obj.file_size,
            "date": media_obj.date.isoformat() if media_obj.date else None
        }

    def _parse_thumbs(self, media_obj) -> list:
        if not hasattr(media_obj, "thumbs") or not media_obj.thumbs:
            return []
        return [
            {
                "file_id": thumb.file_id,
                "width": thumb.width,
                "height": thumb.height,
                "file_size": thumb.file_size
            } for thumb in media_obj.thumbs
        ]

    async def download_media_file(self, file_id: str) -> str:
        """Download media file and return local path"""
        try:
            if not self.client.is_connected:
                await self.start()
            
            # Validate file_id format before downloading
            if len(file_id) < 20 or not re.match(r'^[a-zA-Z0-9_-]+$', file_id):
                logger.error(f"Invalid file_id format: {file_id}")
                raise ValueError("Invalid file identifier format")

            return await self.client.download_media(
                file_id,
                in_memory=False,
                block=True
            )
        except errors.RPCError as e:
            logger.error(f"Telegram media download error {file_id}: {type(e).__name__} - {str(e)}")
            raise
        except ValueError as e:
            logger.error(f"Invalid file_id {file_id}: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Media download failed {file_id}: {type(e).__name__} - {str(e)}")
            raise 

    async def get_channel_posts(self, channel: str, limit: int = 20) -> list:
        try:
            if not self.client.is_connected:
                await self.start()
            
            posts = []
            async for message in self.client.get_chat_history(
                chat_id=channel,
                limit=limit
            ):
                parsed = await self._parse_message(message)
                if parsed:
                    posts.append({
                        "id": message.id,
                        "date": message.date,
                        "title": self._generate_title(parsed.get("raw_text", "")),
                        "html": parsed["html"],
                        "media": parsed.get("media", [])
                    })
            return posts

        except Exception as e:
            logger.error(f"Channel posts error {channel}: {type(e).__name__} - {str(e)}")
            return [] 