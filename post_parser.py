import logging
import copy
import re
import os
import json

from datetime import datetime
from typing import Union, Dict, Any, List
from pyrogram.types import Message
from bleach.css_sanitizer import CSSSanitizer   
from bleach import clean as HTMLSanitizer
from pyrogram.enums import MessageMediaType
from config import get_settings
from url_signer import generate_media_digest

Config = get_settings()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)


#tests
#http://127.0.0.1:8000/post/html/DragorWW_space/114 — video
#http://127.0.0.1:8000/post/html/DragorWW_space/20 - many photos
#http://127.0.0.1:8000/post/html/DragorWW_space/58 - photos+video
#http://127.0.0.1:8000/post/html/DragorWW_space/44 - poll
#http://127.0.0.1:8000/post/html/DragorWW_space/46 - photo
#http://127.0.0.1:8000/post/html/DragorWW_space/49, http://127.0.0.1:8000/post/html/DragorWW_space/63  — webpage
#http://127.0.0.1:8000/post/html/deckru/826 - animation
#http://127.0.0.1:8000/post/html/DragorWW_space/61 — links
#http://127.0.0.1:8000/post/html/theyforcedme/3577 - video note
#http://127.0.0.1:8000/post/html/theyforcedme/3572 - audio
#http://127.0.0.1:8000/post/html/theyforcedme/3558 - audio-note
#http://127.0.0.1:8000/html/vvzvlad_lytdybr/426 - sticker
#http://127.0.0.1:8000/html/wrkshprn/634
# http://127.0.0.1:8000/html/ni404head/1278
# http://127.0.0.1:8000/html/fieryfiles/4840 — links without <a>
#http://127.0.0.1:8000/html/ru2ch_ban/26586 - large video
#http://127.0.0.1:8000/html/smallpharm/4828 - forwarded from channel
#http://127.0.0.1:8000/html/vvzvlad_lytdybr/659 - forwarded from user
#http://127.0.0.1:8000/html/ufjqk/1070 - reply to
#http://127.0.0.1:8000/html/tetstststststststffd/4 - forwarded from channel without name
#http://127.0.0.1:8000/html/tetstststststststffd/14 - forwarded from hidden user
#https://t.me/smallpharm/4802 many pics and text
# https://t.me/webstrangler/3987 many pics without text
# https://t.me/teslacoilpro/7117
# https://t.me/red_spades/1222 many media + text

#https://t.me/ni404head/1283 file
#http://127.0.0.1:8000/rss/-1002069358234 channel with numeric ID

class PostParser:
    def __init__(self, client):
        self.client = client

    def _debug_message(self, message: Message) -> Message:
        if Config["debug"]:   
            debug_message = copy.deepcopy(message)
            debug_message.sender_chat = None
            debug_message.caption_entities = None
            debug_message.reactions = None
            debug_message.entities = None
            print(debug_message)
        return
    
    def channel_name_prepare(self, channel: str):
        if isinstance(channel, str) and channel.startswith('-100'): # Convert numeric channel ID to int
            channel_id = int(channel)
            return channel_id
        else:
            return channel

    async def get_post(self, channel: str, post_id: int, output_type: str = 'json') -> Union[str, Dict[Any, Any]]:
        print(f"Getting post {channel}, {post_id}")
        try:
            channel = self.channel_name_prepare(channel)
            message = await self.client.get_messages(channel, post_id)

            self._debug_message(message)

            if not message:
                logger.error(f"post_not_found: channel {channel}, post_id {post_id}")
                return None
            
            if output_type == 'html':
                return self._format_html(message)
            elif output_type == 'json':
                return self.process_message(message)
            else:
                logger.error(f"Invalid output type: {output_type}")
                return None
            
        except Exception as e:
            logger.error(f"post_parsing_error: channel {channel}, post_id {post_id}, error {str(e)}")
            raise

    def _get_author_info(self, message: Message) -> str:
        if message.sender_chat:
            title = getattr(message.sender_chat, 'title', None)
            username = getattr(message.sender_chat, 'username', None)
            title = title.strip() if title else ''
            username = username.strip() if username else ''
            return f"{title} (@{username})" if username else title
        elif message.from_user:
            first = getattr(message.from_user, 'first_name', None)
            last = getattr(message.from_user, 'last_name', None)
            username = getattr(message.from_user, 'username', None)
            first = first.strip() if first else ''
            last = last.strip() if last else ''
            username = username.strip() if username else ''
            name = ' '.join(filter(None, [first, last]))
            return f"{name} (@{username})" if username else name
        return "Unknown author"

    def _generate_title(self, message: Message) -> str:
        # Check for "channel created" service message
        if getattr(message, "channel_chat_created", False):
            return "✨ Channel created"
        text = message.text or message.caption or ''
        if not text:
            if message.media == MessageMediaType.PHOTO: return "📷 Photo"
            elif message.media == MessageMediaType.VIDEO: return "🎥 Video"
            elif message.media == MessageMediaType.ANIMATION: return "🎞 GIF"
            elif message.media == MessageMediaType.AUDIO: return "🎵 Audio"
            elif message.media == MessageMediaType.VOICE: return "🎤 Voice message"
            elif message.media == MessageMediaType.VIDEO_NOTE: return "🎥 Video message"
            elif message.media == MessageMediaType.STICKER: return "🎯 Sticker"
            elif message.media == MessageMediaType.POLL: return "📊 Poll"
            return "📷 Media post"

        # Remove URLs
        text = re.sub(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', '', text)
        # Remove HTML tags
        text = re.sub('<[^<]+?>', '', text)
        # Remove multiple spaces and empty lines
        text = '\n'.join(line.strip() for line in text.split('\n') if line.strip())

        # Get first non-empty line
        first_line = text.split('\n', maxsplit=1)[0] if text else ""
        
        max_length = 100
        if len(first_line) <= max_length:
            return first_line.strip()
        
        # Cut to last space
        trimmed = first_line[:max_length]
        last_space = trimmed.rfind(' ')
        if last_space > 0:
            trimmed = trimmed[:last_space]
        
        return f"{trimmed.strip()}..." if trimmed else ""

    def _format_forward_info(self, message: Message) -> Union[str, None]:
        if forward_from_chat := getattr(message, "forward_from_chat", None):
            forward_title = getattr(forward_from_chat, "title", "Unknown channel")
            forward_username = getattr(forward_from_chat, "username", None)
            if forward_username:
                forward_link = f'<a href="https://t.me/{forward_username}">{forward_title} (@{forward_username})</a>'
                return f'<div class="message-forward">Forwarded from {forward_link}</div><br>'
            return f'<div class="message-forward">Forwarded from {forward_title}</div><br>'
        
        elif forward_from := getattr(message, "forward_from", None):
            forward_name = f"{getattr(forward_from, 'first_name', '')} {getattr(forward_from, 'last_name', '')}".strip()
            forward_username = getattr(forward_from, "username", None)
            if forward_username:
                forward_link = f'<a href="https://t.me/{forward_username}">{forward_name} (@{forward_username})</a>'
                return f'<div class="message-forward">Forwarded from {forward_link}</div><br>'
            return f'<div class="message-forward">Forwarded from {forward_name}</div><br>'
        
        elif forward_sender_name := getattr(message, "forward_sender_name", None):
            return f'<div class="message-forward">Forwarded from {forward_sender_name}</div><br>'
        
        return None

    def _format_reply_info(self, message: Message) -> Union[str, None]:
        if reply_to := getattr(message, "reply_to_message", None):
            reply_text = reply_to.text or reply_to.caption or ''
            if len(reply_text) > 100:
                reply_text = reply_text[:100] + '...'
            
            channel_username = getattr(reply_to.chat, "username", None)
            if channel_username:
                reply_link = f'<a href="https://t.me/{channel_username}/{reply_to.id}">#{reply_to.id}</a>'
                return f'<div class="message-reply">Reply to {reply_link}: {reply_text}</div><br>'
            return f'<div class="message-reply">Reply to #{reply_to.id}: {reply_text}</div><br>'
        
        return None
    
    def _extract_reactions(self, message: Message) -> Union[str, None]:
        if reactions := getattr(message, 'reactions', None):
            return {r.emoji: r.count for r in reactions.reactions}
        return None


    def _format_html(self, message: Message) -> str:
        html_content = []
        data = self.process_message(message)['html']

        html_content.append(f'<div class="message-header">{data["header"]}</div>')
        html_content.append(f'<div class="message-media">{data["media"]}</div>')
        html_content.append(f'<div class="message-text">{data["body"]}</div>')
        html_content.append(f'<div class="message-footer">{data["footer"]}</div>')
        html_content = '\n'.join(html_content)
        return html_content


    def process_message(self, message: Message) -> Dict[Any, Any]:
        result = {
            'channel': self.get_channel_username(message),
            'message_id': message.id,
            'date': datetime.timestamp(message.date),
            'date_formatted': message.date.strftime('%Y-%m-%d %H:%M:%S'),
            'text': message.text or message.caption or '',
            'html': {
                'title': self._generate_title(message),
                'header': self._generate_html_header(message),
                'body': self._generate_html_body(message),
                'media': self._generate_html_media(message),
                'footer': self._generate_html_footer(message)
            },
            'author': self._get_author_info(message),
            'views': message.views,
            'reactions': self._extract_reactions(message),
            'media_group_id': message.media_group_id
        }

        return result
    

    def _sanitize_html(self, html: str) -> str:
        allowed_tags = ['p', 'a', 'b', 'i', 'strong', 'em', 'ul', 'ol', 'li', 'br', 'div', 'span', 'img', 'video', 'audio', 'source']
        allowed_attributes = {
            'a': ['href', 'title', 'target'],
            'img': {'src': True, 'alt': True, 'style': True},
            'video': {'controls': True, 'src': True, 'style': True},
            'audio': {'controls': True, 'style': True},
            'source': ['src', 'type'],
            'div': {'class': True, 'style': True},
            'span': ['class']
        }

        try:
            css_sanitizer = CSSSanitizer(
                allowed_css_properties=["max-width", "max-height", "object-fit", "width", "height"]
            )
            sanitized_html = HTMLSanitizer(
                html,
                tags=allowed_tags,
                attributes=allowed_attributes,
                css_sanitizer=css_sanitizer,
                strip=True,
            )
            return sanitized_html
        except Exception as e:
            logger.error(f"html_sanitization_error: {str(e)}")
            return html

    def _generate_html_header(self, message: Message) -> str:
        content_header = []
        # Add forwarded from or reply info if present
        if forward_html := self._format_forward_info(message):
            content_header.append(forward_html)
        elif reply_html := self._format_reply_info(message):
            content_header.append(reply_html)
        html_header = '\n'.join(content_header)
        html_header = self._sanitize_html(html_header)
        return html_header

    def _generate_html_body(self, message: Message) -> str:
        content_body = []
        if message.text: text = message.text.html
        elif message.caption: text = message.caption.html
        else: text = ''

        text = text.replace('\n', '<br>') # Replace newlines with <br>
        text = self._add_hyperlinks_to_raw_urls(text) # Add hyperlinks to raw URLs
        
        if text: # Message text
            content_body.append(f'<div class="message-text">{text}</div>')

        if poll := getattr(message, "poll", None): # Poll message
            if poll_html := self._format_poll(poll):
                content_body.append(poll_html)

        if getattr(message, "channel_chat_created", False): # Create service message for "channel created"
            content_body.append('<div class="message-service">Channel created</div>')

        html_body = '\n'.join(content_body)
        html_body = self._sanitize_html(html_body)
        return html_body

    def _generate_html_media(self, message: Message) -> str:
        self._save_media_file_ids(message) # Save media file_ids for caching

        content_media = []
        base_url = Config['pyrogram_bridge_url']
        if message.media and message.media != "MessageMediaType.POLL":
            file_unique_id = self._get_file_unique_id(message)
            if file_unique_id is None:
                logger.debug(f"File unique id not found for message {message.id}")
            elif file_unique_id:
                channel_username = self.get_channel_username(message)
                file = f"{channel_username}/{message.id}/{file_unique_id}"
                digest = generate_media_digest(file)
                url = f"{base_url}/media/{file}/{digest}"

                logger.debug(f"Collected media file: {channel_username}/{message.id}/{file_unique_id}")
                content_media.append(f'<div class="message-media">')
                if message.media in [MessageMediaType.PHOTO, MessageMediaType.DOCUMENT]:
                    content_media.append(f'<img src="{url}" style="max-width:400px; max-height:400px; object-fit:contain;">')
                elif message.media == MessageMediaType.VIDEO:
                    content_media.append(f'<video controls src="{url}" style="max-width:400px; max-height:400px;"></video>')
                elif message.media == MessageMediaType.ANIMATION:
                    content_media.append(f'<video controls src="{url}" style="max-width:400px; max-height:400px;"></video>')
                elif message.media == MessageMediaType.VIDEO_NOTE:
                    content_media.append(f'<video controls src="{url}" style="max-width:400px; max-height:400px;"></video>')
                elif message.media == MessageMediaType.AUDIO:
                    mime_type = getattr(message.audio, 'mime_type', 'audio/mpeg')
                    content_media.append(f'<audio controls style="width:100%; max-width:400px;"><source src="{url}" type="{mime_type}"></audio>')
                elif message.media == MessageMediaType.VOICE:
                    mime_type = getattr(message.voice, 'mime_type', 'audio/ogg')
                    content_media.append(f'<audio controls style="width:100%; max-width:400px;"><source src="{url}" type="{mime_type}"></audio>')
                elif message.media == MessageMediaType.STICKER:
                    emoji = getattr(message.sticker, 'emoji', '')
                    content_media.append(f'<img src="{url}" alt="Sticker {emoji}" style="max-width:200px; max-height:200px; object-fit:contain;">')
                content_media.append('</div>')
        
        if webpage := getattr(message, "web_page", None): # Web page preview
            if webpage_html := self._format_webpage(webpage, message):
                content_media.append(webpage_html)

        html_media = '\n'.join(content_media)
        html_media = self._sanitize_html(html_media)
        return html_media

    def _generate_html_footer(self, message: Message) -> str:
        content_footer = []
        if reactions_views_html := self._reactions_views_links(message):  # Add reactions, views and links
            content_footer.append(reactions_views_html)
        html_footer = '\n'.join(content_footer)
        html_footer = self._sanitize_html(html_footer)
        return html_footer


    def _add_hyperlinks_to_raw_urls(self, text: str) -> str:
        try:
            a_tags = re.finditer(r'<a[^>]*>.*?</a>', text) # Find all existing <a> tags
            excluded_ranges = [(m.start(), m.end()) for m in a_tags] # Store their positions
            result = text
            offset = 0
            
            for match in re.finditer(r'https?://[^\s<>"\']+', text): # Find all URLs that are not already in HTML tags
                start, end = match.span()
                
                # Check if URL is inside an <a> tag
                is_in_tag = any(tag_start <= start and end <= tag_end for tag_start, tag_end in excluded_ranges)
                if not is_in_tag:
                    url = match.group()
                    replacement = f'<a href="{url}" target="_blank">{url}</a>'
                    
                    # Apply replacement considering offset
                    result = result[:start + offset] + replacement + result[end + offset:]
                    offset += len(replacement) - (end - start)
            
            return result
            
        except Exception as e:
            logger.error(f"url_processing_error: error {str(e)}")
            return text

    def _format_webpage(self, webpage, message) -> Union[str, None]:
        base_url = Config['pyrogram_bridge_url']
        try:
            if photo := getattr(webpage, "photo", None):
                logger.debug(f"Processing webpage with photo: message_id={message.id}, photo={photo}")
                if file_unique_id := getattr(photo, "file_unique_id", None):
                    channel_username = self.get_channel_username(message)
                    url = f"{base_url}/media/{channel_username}/{message.id}/{file_unique_id}"
                    logger.debug(f"Generated media URL: {url}")
                    return (
                        f'<div style="margin:5px;">'
                        f'<a href="{webpage.url}" target="_blank">'
                        f'<img src="{url}" style="max-width:600px; max-height:600px; object-fit:contain;"></a>'
                        f'</div>'
                    )
                else:
                    logger.error(f"webpage_photo_error: no file_unique_id found for photo in message {message.id}")
            return None
        except Exception as e:
            logger.error(f"webpage_parsing_error: url {getattr(webpage, 'url', 'unknown')}, error {str(e)}")
            return None

    def _reactions_views_links(self, message: Message) -> Union[str, None]:
        try:
            parts = []
            
            if reactions := getattr(message, "reactions"):
                reactions_html = ''
                for reaction in reactions.reactions:
                    reactions_html += f'<span class="reaction">{reaction.emoji} {reaction.count}&nbsp;&nbsp;</span>'
                parts.append(reactions_html.rstrip())

            if views := getattr(message, "views", None):
                views_html = f'<span class="views">{views} views</span>'
                parts.append(views_html)

            channel_identifier = self.get_channel_username(message)
            if channel_identifier:
                links = []
                if channel_identifier.startswith('-100'):  # For channels with only ID
                    channel_id = channel_identifier[4:]  # Remove '-100' prefix for web links
                    links.append(f'<a href="tg://resolve?domain=c/{channel_id}&post={message.id}">Open in Telegram</a>')
                    links.append(f'<a href="https://t.me/c/{channel_id}/{message.id}">Open in Web</a>')
                else: # For channels with username
                    links.append(f'<a href="tg://resolve?domain={channel_identifier}&post={message.id}">Open in Telegram</a>')
                    links.append(f'<a href="https://t.me/{channel_identifier}/{message.id}">Open in Web</a>')
                parts.append('&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;'.join(links))

            html = '&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;'.join(parts) if parts else None
            return f"<br>{html}" if html else None
            
        except Exception as e:
            logger.error(f"reactions_views_parsing_error: {str(e)}")
            return None

    def _format_poll(self, poll) -> str:
        try:
            poll_text = f"📊 Poll: {poll.question}\n"
            if hasattr(poll, "options") and poll.options:
                for i, option in enumerate(poll.options, 1):
                    poll_text += f"{i}. {getattr(option, 'text', '')}\n"
            poll_text += "\n→ Vote in Telegram 🔗\n"
            return f'<div class="message-poll">{poll_text.replace(chr(10), "<br>")}</div>'
        except Exception as e:
            logger.error(f"poll_parsing_error: {str(e)}")
            return '<div class="message-poll">[Error displaying poll]</div>'

    def _get_file_unique_id(self, message: Message) -> Union[str, None]:
        try:
            media_mapping = {
                MessageMediaType.PHOTO: lambda m: m.photo.file_unique_id,
                MessageMediaType.VIDEO: lambda m: m.video.file_unique_id,
                MessageMediaType.DOCUMENT: lambda m: m.document.file_unique_id,
                MessageMediaType.AUDIO: lambda m: m.audio.file_unique_id,
                MessageMediaType.VOICE: lambda m: m.voice.file_unique_id,
                MessageMediaType.VIDEO_NOTE: lambda m: m.video_note.file_unique_id,
                MessageMediaType.ANIMATION: lambda m: m.animation.file_unique_id,
                MessageMediaType.STICKER: lambda m: m.sticker.file_unique_id,
                MessageMediaType.WEB_PAGE: lambda m: m.web_page.photo.file_unique_id if m.web_page and m.web_page.photo else None
            }
            
            if message.media in media_mapping:
                return media_mapping[message.media](message)
            
            return None
            
        except Exception as e:
            logger.error(f"file_id_extraction_error: media_type {message.media}, error {str(e)}")
            return None

    async def get_recent_posts(self, channel: str, limit: int = 20) -> List[Dict[Any, Any]]:
        try:
            messages = []
            async for message in self.client.get_chat_history(channel, limit=limit):
                try:
                    post = await self.get_post(channel, message.id, output_type='json')
                    if post:
                        messages.append(post)
                except Exception as e:
                    logger.error(f"message_processing_error: channel {channel}, message_id {message.id}, error {str(e)}")
                    continue
                    
            return messages
            
        except Exception as e:
            logger.error(f"recent_posts_error: channel {channel}, error {str(e)}")
            raise

    def _save_media_file_ids(self, message: Message) -> None:
        try:
            file_data = {
                'channel': None,
                'post_id': None,
                'file_unique_id': None,
                'added': None
            }

            channel_username = self.get_channel_username(message)
            if not channel_username:
                logger.error(f"channel_username_error: no username found for chat in message {message.id}")
                return

            if message.media:
                # Skip large videos - they shouldn't be cached permanently
                if message.video and message.video.file_size > 100 * 1024 * 1024:
                    return
                
                if message.photo: file_data['file_unique_id'] = message.photo.file_unique_id
                elif message.video: file_data['file_unique_id'] = message.video.file_unique_id
                elif message.document: file_data['file_unique_id'] = message.document.file_unique_id
                elif message.audio: file_data['file_unique_id'] = message.audio.file_unique_id
                elif message.voice: file_data['file_unique_id'] = message.voice.file_unique_id
                elif message.video_note: file_data['file_unique_id'] = message.video_note.file_unique_id
                elif message.animation: file_data['file_unique_id'] = message.animation.file_unique_id
                elif message.sticker: file_data['file_unique_id'] = message.sticker.file_unique_id
                elif message.web_page and message.web_page.photo: file_data['file_unique_id'] = message.web_page.photo.file_unique_id

                if file_data['file_unique_id']:
                    file_data['channel'] = channel_username
                    file_data['post_id'] = message.id
                    file_data['added'] = datetime.now().timestamp()

                    file_path = os.path.join(os.path.abspath("./data"), 'media_file_ids.json')
                    try:
                        existing_data = []
                        if os.path.exists(file_path):
                            with open(file_path, 'r', encoding='utf-8') as f:
                                existing_data = json.load(f)
                        
                        # Check if file already exists by all three fields
                        found = False
                        for item in existing_data:
                            if (item.get('channel') == file_data['channel'] and 
                                item.get('post_id') == file_data['post_id'] and 
                                item.get('file_unique_id') == file_data['file_unique_id']):
                                item['added'] = datetime.now().timestamp()
                                found = True
                                break
                        
                        # Add new entry if not found
                        if not found:
                            existing_data.append(file_data)
                        
                        with open(file_path, 'w', encoding='utf-8') as f:
                            json.dump(existing_data, f, ensure_ascii=False, indent=2)

                    except Exception as e:
                        logger.error(f"file_id_save_error: error writing to {file_path}: {str(e)}")

        except Exception as e:
            logger.error(f"file_id_collection_error: message_id {message.id}, error {str(e)}")

    def get_channel_username(self, message):
        """Extract channel username or ID from message"""
        chat = message.chat if hasattr(message, 'chat') else message
        if not chat: return None
        
        # First try to get username
        if hasattr(chat, 'usernames') and chat.usernames: # Check many usernames
            active_usernames = [u.username for u in chat.usernames if u.active]
            if active_usernames:
                return active_usernames[0]
        if hasattr(chat, 'username') and chat.username: return chat.username # Check single username
        
        # Return numeric ID only if no username found
        if isinstance(chat.id, int) and str(chat.id).startswith('-100'): return str(chat.id)
        
        logger.error(f"channel_username_error: no username or valid ID found for chat")
        return None 
