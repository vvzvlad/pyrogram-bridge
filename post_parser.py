#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylance: disable=reportMissingImports, reportMissingModuleSource, reportGeneralTypeIssues
# type: ignore

import logging
import re
import os
import json
import html
import inspect
from datetime import datetime
from typing import Union, Dict, Any, List, Optional
from pyrogram.types import Message
from pyrogram.enums import MessageMediaType
from bleach.css_sanitizer import CSSSanitizer   
from bleach import clean as HTMLSanitizer
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

    @staticmethod
    def get_all_possible_flags() -> List[str]:
        """Dynamically extracts all possible flag names from the _extract_flags method."""
        try:
            source_code = inspect.getsource(PostParser._extract_flags)
            # Find all occurrences of flags.append("flag_name")
            flags = re.findall(r'flags\.append\("([^"]+)"\)', source_code)
            # Return unique flags
            return sorted(list(set(flags)))
        except Exception as e:
            logger.error(f"flag_extraction_error: Could not extract flags dynamically: {str(e)}")
            # Fallback to a manually defined list might be needed here in case of error,
            # but for now, we return an empty list.
            return []

    def channel_name_prepare(self, channel: str):
        if isinstance(channel, str) and channel.startswith('-100'): # Convert numeric channel ID to int
            channel_id = int(channel)
            return channel_id
        else:
            return channel

    async def get_post(self, channel: str, post_id: int, output_type: str = 'json', debug: bool = False) -> Union[str, Dict[Any, Any]]:
        print(f"Getting post {channel}, {post_id}")
        try:
            channel = self.channel_name_prepare(channel)
            message = await self.client.get_messages(channel, post_id)

            if Config["debug"]: print(message)

            if not message or getattr(message, 'empty', False):
                logger.error(f"post_not_found_or_empty: channel {channel}, post_id {post_id}")
                return None
            
            if output_type == 'html':
                return self._format_html(message, debug)
            elif output_type == 'json':
                return self.process_message(message)
            else:
                logger.error(f"Invalid output type: {output_type}")
                return None
            
        except Exception as e:
            logger.error(f"post_parsing_error: channel {channel}, post_id {post_id}, error {str(e)}")
            raise

    def _get_author_info(self, message: Message) -> str: #Tests: tests/postparser_author_info.py
        if message.sender_chat:
            title = getattr(message.sender_chat, 'title', None)
            username = getattr(message.sender_chat, 'username', None)
            title = title.strip() if title else ''
            username = username.strip() if username else ''
            if username:
                if title:
                    return f"{title} (@{username})"
                else:
                    return f"@{username}"
            return title
        elif message.from_user:
            first = getattr(message.from_user, 'first_name', None)
            last = getattr(message.from_user, 'last_name', None)
            username = getattr(message.from_user, 'username', None)
            first = first.strip() if first else ''
            last = last.strip() if last else ''
            username = username.strip() if username else ''
            name = ' '.join(filter(None, [first, last]))
            if not name:
                return "Unknown author"
            
            if username:
                return f"{name} (@{username})"
            else:
                return name
        return "Unknown author"

    def _truncate_title(self, first_line: str) -> str:
        """Truncate the title """
        # Step 1: Cut at the first period followed by a space, if present
        period_match = re.search(r'\.(?=\s)', first_line)
        if period_match: first_line = first_line[:period_match.start()].rstrip()

        # Step 2: Apply the old logic
        cut_at = 37
        max_extra_chars = 15
        limit_index = cut_at + max_extra_chars  # 52

        if len(first_line) > cut_at:
            cut_limit = min(len(first_line), limit_index)
            last_space_index = first_line.rfind(' ', 0, cut_limit)

            if last_space_index != -1 and last_space_index >= cut_at: cut_index = last_space_index
            else: cut_index = cut_limit

            title_segment = first_line[:cut_index]
            title_segment = re.sub(r'[\s.,;:]+$', '', title_segment).strip()

            if len(first_line[:cut_index]) < len(first_line): return f"{title_segment}..."
            else: return title_segment
        else: return first_line
        
    def _service_message_title(self, message: Message) -> str:
        if service := getattr(message, "service", None):
            if 'PINNED_MESSAGE'           in str(service): return "📌 Pinned message"
            elif 'NEW_CHAT_PHOTO'         in str(service): return "🖼 New chat photo"
            elif 'NEW_CHAT_TITLE'         in str(service): return "✏️ New chat title"
            elif 'VIDEO_CHAT_STARTED'     in str(service): return "▶️ Video chat started"
            elif 'VIDEO_CHAT_ENDED'       in str(service): return "⏹ Video chat ended"
            elif 'VIDEO_CHAT_SCHEDULED'   in str(service): return "⏰ Video chat scheduled"
            elif 'GROUP_CHAT_CREATED'     in str(service): return "✨ Group chat created"
            elif 'CHANNEL_CHAT_CREATED'   in str(service): return "✨ Chat created"
            elif 'DELETE_CHAT_PHOTO'      in str(service): return "🗑️ Chat photo deleted"

    def _media_message_title(self, message: Message) -> str:
        if message.media:
            if message.media == MessageMediaType.POLL:
                if hasattr(message, 'poll') and hasattr(message.poll, 'question'):
                    poll_question = message.poll.question.strip()
                    if poll_question:
                        return f"📊 Poll: {poll_question}"
                return "📊 Poll"
            if message.media == MessageMediaType.DOCUMENT:
                if hasattr(message.document, 'mime_type') and 'pdf' in message.document.mime_type.lower():
                    return "📄 PDF Document"
                return "📎 Document"
            if message.media == MessageMediaType.PHOTO:       return "📷 Photo"
            if message.media == MessageMediaType.VIDEO:       return "🎥 Video"
            if message.media == MessageMediaType.ANIMATION:   return "🎞 GIF"
            if message.media == MessageMediaType.AUDIO:       return "🎵 Audio"
            if message.media == MessageMediaType.VOICE:       return "🎤 Voice"
            if message.media == MessageMediaType.VIDEO_NOTE:  return "📱 Video circle"
            if message.media == MessageMediaType.STICKER:     return "🎯 Sticker"

        # Web pages (if no text or media title)
        if message.web_page:
            if message.web_page.title:
                return f"🔗 {message.web_page.title}"
            return "🔗 Web link"
        

    def _generate_base_title(self, message: Message) -> str:
        """Generates the base title without the FWD prefix."""
        # --- Text Processing --- (Phase 1: Process text if available)
        text = message.text or message.caption or ''
        text_stripped = text.strip()
        processed_title = None
        text_was_processed = False # Flag to indicate if text processing block was entered
        min_title_length = 10 # Minimum length for text to be preferred over media/webpage

        if text_stripped:
            text_was_processed = True
            text_has_urls = bool(re.search(r'https?://[^\\s<>\"\\\']+', text_stripped))

            clean_text = html.unescape(text) # Remove HTML entities 
            clean_text = re.sub(r'<[^>]+?>', '', clean_text) # Remove HTML tags
            clean_text = re.sub(r'https?://[^\s<>"\']+', '', clean_text) # Remove URLs
            clean_text = clean_text.strip() # Remove whitespaces

            if clean_text: # If text remains after cleaning
                # Process URL at the beginning
                if text_has_urls and "://" in clean_text:
                    clean_text = clean_text.split("://")[0].strip()

                # Process line breaks & punctuation
                first_line = clean_text.split('\n', 1)[0] # Get first line  
                first_line = re.sub(r'[.,;:]+$', '', first_line) # Remove trailing punctuation
                first_line = first_line.strip() # Remove whitespaces

                # Handle uppercase
                if first_line.isupper() and len(first_line) > 1:
                    first_line = first_line.lower().capitalize() # Downcase and capitalize first letter

                # --- Trim long strings ---
                processed_title = self._truncate_title(first_line) # Truncate title

        # --- Decision Logic --- (Phase 2: Decide whether to use processed text or fallback)

        # Condition to use processed_title: It exists AND (it's long enough OR there's no media)
        # Webpage presence doesn't prevent using short text if there's no media.
        use_text_title = processed_title and (len(processed_title) >= min_title_length or not message.media)
        if use_text_title: return processed_title

        # --- Fallback Processing --- (Phase 3: If text wasn't suitable or was discarded)

        # Handle specific cases for non-meaningful original text (if text block was entered but didn't yield a usable title)
        if text_was_processed and not use_text_title:
            if re.search(r'(?:youtube\.com|youtu\.be)', text_stripped.lower()): return "🎥 YouTube Link"
            # Check if original text was just a URL and there's a webpage title
            if message.web_page and message.web_page.title:
                url_match = re.match(r'^\s*(https?://[^\s<>"\']+)\s*$', text_stripped)
                if url_match: return f"🔗 {message.web_page.title}"
            if text_has_urls: # If original text had any URL (and wasn't YouTube/Webpage with title)
                return  "🔗 Web link"

    def _generate_title(self, message: Message) -> str: #Tests: tests/postparser_gen_title.py
        """Generate a title for a message, based on its content."""
        title = None
        title = self._service_message_title(message)
        if title is None: title = self._generate_base_title(message)
        if title is None: title = self._media_message_title(message)
        if title is None: title = "❓ Unknown Post"
        if message.forward_origin: title = f"FWD: {title}"
    
        return title

    def _format_forward_info(self, message: Message) -> Union[str, None]:
        if forward_origin := getattr(message, "forward_origin", None):
            if sender_chat := getattr(forward_origin, "chat", None):
                forward_title = getattr(sender_chat, "title", "Unknown channel")
                forward_username = getattr(sender_chat, "username", None)
                if forward_username:
                    forward_link = f'<a href="https://t.me/{forward_username}">{forward_title} (@{forward_username})</a>'
                    return f'<div class="message-forward">Forwarded from {forward_link}</div><br>'
                return f'<div class="message-forward">Forwarded from {forward_title}</div><br>'
            
        return None

    def _format_reply_info(self, message: Message) -> Union[str, None]:
        if getattr(message, "service", None) and 'PINNED_MESSAGE' in str(message.service) and (reply_to := getattr(message, "reply_to_message", None)):
            reply_text = reply_to.text or reply_to.caption or ''
            if len(reply_text) > 100:
                reply_text = reply_text[:100] + '...'
            return f'<div class="message-pinned">Pinned: {reply_text}</div><br>'
        
        if reply_to := getattr(message, "reply_to_message", None):
            reply_text = reply_to.text or reply_to.caption or ''
            if len(reply_text) > 100:
                reply_text = reply_text[:100] + '...'
            
            channel_username = getattr(reply_to.sender_chat, "username", None)
            if channel_username:
                reply_link = f'<a href="https://t.me/{channel_username}/{reply_to.id}">#{reply_to.id}</a>'
                return f'<div class="message-reply">Reply to {reply_link}: {reply_text}</div><br>'
            return f'<div class="message-reply">Reply to #{reply_to.id}: {reply_text}</div><br>'
        
        return None
    
    def _extract_reactions(self, message: Message) -> Union[str, None]:
        if reactions := getattr(message, 'reactions', None):
            return {r.emoji: r.count for r in reactions.reactions}
        return None

    def _extract_flags(self, message: Message) -> List[str]: #Tests: tests/postparser_extract_flags.py
        # Use raw text/caption for some checks before HTML processing
        message_text_str = str(message.text or message.caption or '')
        # Use HTML body for checks involving formatted text or links within HTML
        message_body_html = self._generate_html_body(message)
        flags = []

        # Add "fwd" flag for forwarded messages
        if message.forward_origin:
            flags.append("fwd")

        # Add flag "video" if the message media is VIDEO or ANIMATION and the body text is up to 200 characters.
        if (message.media in [MessageMediaType.VIDEO, MessageMediaType.ANIMATION, MessageMediaType.VIDEO_NOTE] and 
            len((message.text or message.caption or '').strip()) <= 200):
            flags.append("video")

        # Add flag "audio" if the message media is AUDIO
        if (message.media in [MessageMediaType.AUDIO, MessageMediaType.VOICE] and 
            len((message.text or message.caption or '').strip()) <= 200):
            flags.append("audio")
        
        # Add flag for posts without images
        if not message.media or message.media == MessageMediaType.POLL:
            flags.append("no_image")

        # Add flag for sticker messages
        if message.media == MessageMediaType.STICKER:
            flags.append("sticker")

        # Add flag for poll messages
        if message.media == MessageMediaType.POLL:
            flags.append("poll")

        # Check if the message text contains variations of the word "стрим", "вебинар" 
        # or "онлайн-лекция" in a case-insensitive manner.
        if re.search(r'(?i)\b(стрим\w*|livestream|онлайн-лекци[яю]|вебинар\w*)\b', message_text_str):
            flags.append("stream")
        
        # Check if the message text contains the word "донат" in a case-insensitive manner.
        if re.search(r'(?i)\bдонат\w*\b', message_text_str):
            flags.append("donat")

        # Check for pay.cloudtips.ru links and add donat flag
        if re.search(r'(?i)pay\.cloudtips\.ru', message_text_str):
            flags.append("donat")

        # Check for t.me/boost links and add donat flag
        if re.search(r'https?://(?:www\.)?t\.me/boost/', message_text_str):
            flags.append("donat")

        # Check if the post's reactions contain more clown emojis (🤡) or poo emojis (💩).
        if getattr(message, "reactions", None):
            for reaction in message.reactions.reactions:
                if reaction.emoji == "🤡" and reaction.count >= 30:
                    flags.append("clownpoo")
                    break
                if reaction.emoji == "💩" and reaction.count >= 30:
                    flags.append("clownpoo")
                    break

        # Check if the message text contains "#реклама", "Партнерский пост", "по промокоду", "скидка на курс", "регистрируйтесь тут" in a case-insensitive manner.
        if re.search(r'(?i)(#реклама|#промо|О\s+рекламодателе|партнерский\s+пост|по\s+промокоду|erid|скидка\s+на\s+курс|регистрируйтесь\s+тут)', message_text_str):
            flags.append("advert")

        # Check for paywall-related words and tags
        if re.search(r'(?i)(Дзен\.Премиум|Sponsr|Бусти|Boosty)', message_text_str):
            flags.append("paywall")

        # --- Link Flags ---
        # Check if the message consists ONLY of a link (text/caption) or only has a webpage preview
        is_only_link = False
        if message.web_page and not message_text_str.strip():
            # Case 1: Only a web_page preview, no text/caption
            is_only_link = True
        elif message_text_str.strip():
            # Case 2: Text/caption exists, check if it's just a single non-t.me URL
            # Regex matches string starting and ending with a non-t.me URL
            url_pattern = r'^https?://(?!(?:www\.)?t\.me)[^\s<>"\']+$'
            if re.fullmatch(url_pattern, message_text_str.strip()):
                is_only_link = True
        
        if is_only_link:
            flags.append("only_link")

        # Check if the message contains any http/https links (excluding t.me) 
        # Only add 'link' if 'only_link' isn't already set
        if not is_only_link:
            # Search within the generated HTML body to catch links in hrefs as well
            if (re.search(r'https?://(?!(?:www\.)?t\.me)[^\s<>"\']+', message_body_html) or 
                re.search(r'href=[\"\']https?://(?!(?:www\.)?t\.me)[^\"\']+[\"\']', message_body_html)):
                flags.append("link")
        # --- End Link Flags ---

        # Check if the message contains channel mentions in the format @name
        if re.search(r'@[a-zA-Z][a-zA-Z0-9_]{3,}', message_body_html):
            flags.append("mention")

        try:
            # Find links with a '+' after t.me/ indicating a hidden channel link.
            hidden_links = re.findall(r'https?://(?:www\.)?t\.me/\+([A-Za-z0-9]+)', message_body_html)
            # Find links without a '+' after t.me/ indicating an open (foreign) channel link.
            open_links = re.findall(r'https?://(?:www\.)?t\.me/(?!\+)([A-Za-z0-9_]+)', message_body_html)
            
            # Find links with pattern t.me/boost/channel_name
            boost_links = re.findall(r'https?://(?:www\.)?t\.me/boost/([A-Za-z0-9_]+)', message_body_html)
            
            if hidden_links:
                flags.append("hid_channel")

            current_channel = self.get_channel_username(message)
            
            # Check regular open links
            for open_link in open_links:
                if current_channel is None or (open_link.lower() != current_channel.lower() and open_link.lower() != 'boost'):
                    flags.append("foreign_channel")
                    break
                    
            # Check boost links separately - only consider as foreign if the boosted channel is not current channel
            if "foreign_channel" not in flags:
                for boost_channel in boost_links:
                    if current_channel is None or boost_channel.lower() != current_channel.lower():
                        flags.append("foreign_channel")
                        break
        except Exception as e:
            logger.error(f"tme_link_extraction_error: message_id {message.id}, error {str(e)}")

        return flags

    def _format_html(self, message: Message, debug: bool = False) -> str:
        html_content = []
        data = self.process_message(message)['html']

        if debug:
            html_content.append(f'<div class="title">Title: {data["title"]}</div>')
        html_content.append(f'<div class="message-media">{data["media"]}</div>')
        html_content.append(f'<div class="message-body">{data["body"]}</div>')
        html_content.append(f'<div class="message-footer">{data["footer"]}</div>')
        
        # Add raw JSON debug output if debug is enabled
        if debug:
            html_content.append(f'<pre class="debug-json" style="background: #f5f5f5; padding: 10px; margin-top: 20px; overflow-x: auto; font-size: 10px; white-space: pre-wrap;">{str(message)}</pre>')
        html_content = '\n'.join(html_content)
        return html_content

    def _format_flags(self, flags_list: list) -> str:
        if not Config['show_post_flags']:
            return ''
        
        if flags_list:
            flags_html = ['<div class="message-flags">']
            for flag in flags_list:
                flags_html.append(f'🏷 {flag}')
            flags_html.append('</div>')
            return ' '.join(flags_html)
        return ''

    def process_message(self, message: Message) -> Dict[Any, Any]:
        result = {
            'channel': self.get_channel_username(message),
            'message_id': message.id,
            'date': datetime.timestamp(message.date) if message.date else None,
            'date_formatted': message.date.strftime('%Y-%m-%d %H:%M:%S') if message.date else None,
            'text': message.text or message.caption or '',
            'html': {
                'title': self._generate_title(message),
                'body': self._generate_html_body(message),
                'media': self._generate_html_media(message),
                'footer': self.generate_html_footer(message)
            },
            'flags': self._extract_flags(message),
            'author': self._get_author_info(message),
            'views': message.views,
            'reactions': self._extract_reactions(message),
            'media_group_id': message.media_group_id,
            'service': getattr(message, "service", None)
        }
        
        # Add webpage data if available
        if webpage := getattr(message, "web_page", None):
            result['webpage'] = {
                'type': getattr(webpage, 'type', None),
                'url': getattr(webpage, 'url', None),
                'display_url': getattr(webpage, 'display_url', None),
                'site_name': getattr(webpage, 'site_name', None),
                'title': getattr(webpage, 'title', None),
                'description': getattr(webpage, 'description', None),
                'has_large_media': getattr(webpage, 'has_large_media', False),
                'is_telegram_link': getattr(webpage, 'type', '') == 'telegram_message'
            }

        return result
    

    def _sanitize_html(self, html_raw: str) -> str:
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
                html_raw,
                tags=allowed_tags,
                attributes=allowed_attributes,
                protocols=['http', 'https', 'tg'],
                css_sanitizer=css_sanitizer,
                strip=True,
            )
            return sanitized_html
        except Exception as e:
            logger.error(f"html_sanitization_error: {str(e)}")
            return html_raw

    def _generate_html_body(self, message: Message) -> str:
        content_body = []

        test_fwd = self._format_forward_info(message)
        test_reply = self._format_reply_info(message)
        logger.error(f"Forward info: {test_fwd}, Reply info: {test_reply}")

        if reply_html := self._format_reply_info(message): content_body.append(reply_html)

        if message.text: text = message.text.html
        elif message.caption: text = message.caption.html
        else: text = ''

        text = text.replace('\n', '<br>') # Replace newlines with <br>
        text_html = self._add_hyperlinks_to_raw_urls(text)

        poll_html = ''
        if poll := getattr(message, "poll", None):
            poll_html = self._format_poll(poll)

        if text_html or poll_html: 
            content_body.append(f'<div class="message-text">')
            if message.forward_origin: content_body.append(self._format_forward_info(message)) # Forward info
            content_body.append(f'{text_html}')
            if poll_html: content_body.append(poll_html) # Poll
            if message.forward_origin: content_body.append(f"<br>---- Forward post end ----") # Forward info end
            content_body.append(f'</div><br>')

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
                
                # Check if document is a PDF file
                if message.media == MessageMediaType.DOCUMENT and hasattr(message.document, 'mime_type') and message.document.mime_type == 'application/pdf':
                    if channel_username.startswith('-100'): tg_link = f"https://t.me/c/{channel_username[4:]}/{message.id}"
                    else:  tg_link = f"https://t.me/{channel_username}/{message.id}"
                    content_media.append(f'<div class="document-pdf" style="padding: 10px;"><a href="{tg_link}" target="_blank">[PDF-файл]</a></div>')
                elif message.media in [MessageMediaType.PHOTO, MessageMediaType.DOCUMENT]:
                    content_media.append(f'<img src="{url}" style="max-width:100%; width:auto; height:auto; max-height:400px; object-fit:contain;">')
                elif message.media == MessageMediaType.VIDEO:
                    content_media.append(f'<video controls src="{url}" style="max-width:100%; width:auto; height:auto; max-height:400px;"></video>')
                elif message.media == MessageMediaType.ANIMATION:
                    content_media.append(f'<video controls src="{url}" style="max-width:100%; width:auto; height:auto; max-height:400px;"></video>')
                elif message.media == MessageMediaType.VIDEO_NOTE:
                    content_media.append(f'<video controls src="{url}" style="max-width:100%; width:auto; height:auto; max-height:400px;"></video>')
                elif message.media == MessageMediaType.AUDIO:
                    mime_type = getattr(message.audio, 'mime_type', 'audio/mpeg')
                    content_media.append(f'<audio controls style="width:100%; max-width:400px;"><source src="{url}" type="{mime_type}"></audio>')
                    content_media.append('<br>')
                elif message.media == MessageMediaType.VOICE:
                    mime_type = getattr(message.voice, 'mime_type', 'audio/ogg')
                    content_media.append(f'<audio controls style="width:100%; max-width:400px;"><source src="{url}" type="{mime_type}"></audio>')
                    content_media.append('<br>')
                elif message.media == MessageMediaType.STICKER:
                    emoji = getattr(message.sticker, 'emoji', '')
                    if getattr(message.sticker, 'is_video', False):
                        content_media.append(f'<video controls autoplay loop muted src="{url}" style="max-width:100%; width:auto; height:auto; max-height:200px; object-fit:contain;"></video>')
                    else:
                        content_media.append(f'<img src="{url}" alt="Sticker {emoji}" style="max-width:100%; width:auto; height:auto; max-height:200px; object-fit:contain;">')
                content_media.append('</div>')
        
        if webpage := getattr(message, "web_page", None): # Web page preview
            if webpage_html := self._format_webpage(webpage, message):
                if len((message.text or message.caption or '').strip()) <= 10:
                    content_media.append(webpage_html)

        html_media = '\n'.join(content_media)
        html_media = self._sanitize_html(html_media)
        return html_media


    def _format_webpage(self, webpage, message) -> Union[str, None]:
        base_url = Config['pyrogram_bridge_url']
        try:
            # Check if this is a Telegram message link
            is_telegram_message = getattr(webpage, "type", "") == "telegram_message"
            
            if is_telegram_message:
                # Telegram message preview with distinctive styling
                html_parts = ['<div class="webpage-preview telegram-preview" style="border-left: 3px solid #0088cc; padding-left: 10px; margin: 10px 0; background-color: #f5fafd;">']
                html_parts.append(f'<div class="webpage-site" style="color:#0088cc; font-size:0.9em;">📱 Telegram</div>')
            else:
                # Regular webpage preview
                html_parts = ['<div class="webpage-preview" style="border-left: 3px solid #ccc; padding-left: 10px; margin: 10px 0;">']
                # Add site name if available
                if site_name := getattr(webpage, "site_name", None):
                    html_parts.append(f'<div class="webpage-site" style="color:#666; font-size:0.9em;">{site_name}</div>')
            
            # Add title with link if available
            if title := getattr(webpage, "title", None):
                url = getattr(webpage, "url", "#")
                html_parts.append(f'<div class="webpage-title" style="font-weight:bold; margin:5px 0;"><a href="{url}" target="_blank">{title}</a></div>')
            
            # Add description if available
            if description := getattr(webpage, "description", None):
                # Process the description to handle line breaks and possibly HTML
                processed_description = description.replace('\n', '<br>')
                processed_description = self._add_hyperlinks_to_raw_urls(processed_description)
                html_parts.append(f'<div class="webpage-description" style="margin:5px 0;">{processed_description}</div>')
            
            # Display URL for non-telegram links or when display_url is available
            if not is_telegram_message:
                display_url = getattr(webpage, "display_url", None)
                url = getattr(webpage, "url", None)
                if display_url:
                    html_parts.append(f'<div class="webpage-url" style="color:#666; font-size:0.9em; margin-bottom:5px;">{display_url}</div>')
                elif url:
                    html_parts.append(f'<div class="webpage-url" style="color:#666; font-size:0.9em; margin-bottom:5px;">{url}</div>')
            
            # Add photo if available
            if photo := getattr(webpage, "photo", None):
                if file_unique_id := getattr(photo, "file_unique_id", None):
                    channel_username = self.get_channel_username(message)
                    file = f"{channel_username}/{message.id}/{file_unique_id}"
                    digest = generate_media_digest(file)
                    url = f"{base_url}/media/{file}/{digest}"
                    html_parts.append(f'<div class="webpage-photo" style="margin-top:10px;"><a href="{webpage.url}" target="_blank">'
                                        f'<img src="{url}" style="max-width:100%; width:auto; height:auto; max-height:200px; object-fit:contain;"></a></div>')
            
            html_parts.append('</div>')
            return '\n'.join(html_parts)
        except Exception as e:
            logger.error(f"webpage_parsing_error: url {getattr(webpage, 'url', 'unknown')}, error {str(e)}")
            return None


    def generate_html_footer(self, message: Message, flags_list: Optional[List[str]] = None) -> str:
        content_footer = []
        content_footer.append('<br>')
        if reactions_views_html := self._reactions_views_links(message):  # Add reactions, views, date and links
            content_footer.append(reactions_views_html)
        
        # Use provided flags_list if available, otherwise extract from message
        current_flags = flags_list if flags_list is not None else self._extract_flags(message)
        
        if current_flags:
            flags_html = self._format_flags(current_flags)
            content_footer.append('<br>' + flags_html)
            
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


    def _reactions_views_links(self, message: Message) -> Union[str, None]:
        try:
            parts = []
            
            # First line: reactions + views + date
            first_line_parts = []
            
            # Add reactions
            reactions_html = ""
            if reactions := getattr(message, "reactions", None):
                for reaction in reactions.reactions:
                    if getattr(reaction, "is_paid", False): emoji = "⭐" 
                    elif hasattr(reaction, "emoji") and reaction.emoji: emoji = reaction.emoji 
                    elif hasattr(reaction, "custom_emoji_id"): emoji = "❓" # Then check custom emoji
                    else: emoji = "❓" # Default for unknown cases
                    reactions_html += f'<span class="reaction">{emoji} {reaction.count}&nbsp;&nbsp;</span>'
                reactions_html = reactions_html.rstrip()
                first_line_parts.append(reactions_html)

            # Add views
            if views := getattr(message, "views", None):
                first_line_parts.append(f'<span class="views">{views} 👁</span>')

            # Add date
            if message.date:
                formatted_date = message.date.strftime("%d/%m/%y, %H:%M:%S")
                first_line_parts.append(formatted_date)

            if first_line_parts:
                parts.append('&nbsp;&nbsp;|&nbsp;&nbsp;'.join(first_line_parts))

            # Second line: links
            channel_identifier = self.get_channel_username(message)
            if channel_identifier:
                links = []
                base_url = Config['pyrogram_bridge_url']
                
                if channel_identifier.startswith('-100'):
                    channel_id = channel_identifier[4:]
                    links.append(f'<a href="tg://resolve?domain=c/{channel_id}&post={message.id}">Open in Telegram</a>')
                    links.append(f'<a href="https://t.me/c/{channel_id}/{message.id}">Open in Web</a>')
                else:
                    links.append(f'<a href="tg://resolve?domain={channel_identifier}&post={message.id}">Open in Telegram</a>')
                    links.append(f'<a href="https://t.me/{channel_identifier}/{message.id}">Open in Web</a>')
                if Config['show_bridge_link']:
                    token = Config['token']
                    links.append(f'<a href="{base_url}/html/{channel_identifier}/{message.id}?token={token}&debug=true">Open in Bridge</a>')
                
                if links:
                    parts.append('&nbsp;|&nbsp;'.join(links))

            result_html = '<br>'.join(parts) if parts else None
            return self._sanitize_html(result_html) if result_html else None
            
        except Exception as e:
            # Log the type of the object causing the error
            obj_type = type(message).__name__
            # Try to get an identifier safely
            log_id = None
            try:
                if isinstance(message, dict):
                    log_id = message.get('message_id', 'unknown_dict_id')
                else:
                    log_id = getattr(message, 'id', 'unknown_object_id')
            except Exception:
                log_id = "id_retrieval_failed"

            logger.error(f"reactions_views_parsing_error: Type '{obj_type}', MsgID '{log_id}', Error: {str(e)}", exc_info=True)
            logger.error(f"Problematic object data: {str(message)}")
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
                if message.video and message.video.file_size > 100 * 1024 * 1024: return
                
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
