#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation
# pylance: disable=reportMissingImports, reportMissingModuleSource
# mypy: disable-error-code="import-untyped"

import logging
import asyncio
import re
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from feedgen.feed import FeedGenerator
from pyrogram import errors, Client
from pyrogram.types import Message
from post_parser import PostParser
from config import get_settings
from bleach.css_sanitizer import CSSSanitizer
from bleach import clean as HTMLSanitizer

Config = get_settings()

logger = logging.getLogger(__name__)

async def _create_time_based_media_groups(messages: list[Message], merge_seconds: int = 5) -> list[Message]:
    """
    Create media groups based on time difference between messages
    """
    messages_sorted = sorted(messages, key=lambda msg: msg.date) # type: ignore
    cluster: list[Message] = []
    last_msg_date: datetime = datetime.now(timezone.utc)
    current_media_group_id: Optional[str] = None

    for msg in messages_sorted:
        
        if not cluster:
            cluster.append(msg)
            last_msg_date = msg.date # type: ignore
            current_media_group_id = getattr(msg, "media_group_id", None)
            continue
        
        time_diff = (msg.date - last_msg_date).total_seconds() # type: ignore
        
        msg_media_group_id = getattr(msg, "media_group_id", None)
        
        if time_diff <= merge_seconds:
            if current_media_group_id:
                msg.media_group_id = current_media_group_id  # type: ignore
            elif msg_media_group_id:
                current_media_group_id = msg_media_group_id
                for m in cluster:
                    m.media_group_id = current_media_group_id  # type: ignore
            cluster.append(msg)
            last_msg_date = msg.date # type: ignore
        else:
            if len(cluster) >= 2 and not current_media_group_id:
                dates = [m.date for m in cluster if m.date is not None]
                if dates:
                    min_date = min(dates)
                    new_group_id = f"time_{min_date}"
                    for m in cluster:
                        m.media_group_id = new_group_id  # type: ignore
            cluster = [msg]
            last_msg_date = msg.date # type: ignore
            current_media_group_id = msg_media_group_id
    
    if len(cluster) >= 2 and not current_media_group_id:
        dates = [m.date for m in cluster if m.date is not None]
        if dates:
            min_date = min(dates)
            new_group_id = f"time_{min_date}"
            for m in cluster:
                m.media_group_id = new_group_id  # type: ignore

    return messages_sorted

async def _create_messages_groups(messages: list[Message]) -> list[list[Message]]:
    """
    Process messages into formatted posts, handling media groups
    """
    processing_groups: list[list[Message]] = []
    media_groups: dict[str | int, list[Message]] = {}
    
    # First pass - collect messages and organize into processing groups
    for message in messages:
        try:
            # Skip service messages about pinned posts and new chat photos
            if message.service:
                if 'PINNED_MESSAGE'         in str(message.service): continue
                if 'NEW_CHAT_PHOTO'         in str(message.service): continue
                if 'NEW_CHAT_TITLE'         in str(message.service): continue
                if 'VIDEO_CHAT_STARTED'     in str(message.service): continue
                if 'VIDEO_CHAT_ENDED'       in str(message.service): continue
                if 'VIDEO_CHAT_SCHEDULED'   in str(message.service): continue
                if 'GROUP_CHAT_CREATED'     in str(message.service): continue
                if 'CHANNEL_CHAT_CREATED'   in str(message.service): continue
                if 'DELETE_CHAT_PHOTO'      in str(message.service): continue
                if 'NEW_CHAT_TITLE'         in str(message.service): continue

            if message.media_group_id:
                if message.media_group_id not in media_groups:
                    media_groups[message.media_group_id] = []
                media_groups[message.media_group_id].append(message)
            else:
                processing_groups.append([message]) # Single message becomes its own processing group
                
        except Exception as e:
            username = message.chat.username if message.chat else 'unknown_chat'
            logger.error(f"_create_messages_groups: channel {username}, message_id {message.id}, error {str(e)}")
            continue
    
    # Sort messages within media groups by message ID in descending order
    for media_group in media_groups.values():
        media_group.sort(key=lambda x: x.id, reverse=False)
        processing_groups.append(media_group)
    
    # Sort processing groups by date of first message in each group
    processing_groups.sort(key=lambda group: group[0].date if group[0].date else datetime.now(timezone.utc), reverse=True)
    
    return processing_groups

async def _trim_messages_groups(messages_groups: list[list[Message]], limit: int):
    """
    Trim messages groups to limit
    """
    if messages_groups: # Remove the oldest group (the one with the lowest message id based on the first message's date)
        messages_groups.pop()
    
    if len(messages_groups) > limit: # Trim groups if they exceed the specified limit
        messages_groups = messages_groups[:limit]
    
    return messages_groups

def processed_message_to_tg_message(processed_message: dict) -> Message:
    """
    Convert processed message dictionary into a Message-like object
    containing only the attributes needed by generate_html_footer.
    """
    # Create a simple chat object
    chat_info = SimpleNamespace()
    channel_identifier = processed_message.get('channel')
    if isinstance(channel_identifier, str) and channel_identifier.startswith('-100'):
        setattr(chat_info, 'id', int(channel_identifier))
        setattr(chat_info, 'username', None)
    else:
        setattr(chat_info, 'id', None) # Or some placeholder if needed
        setattr(chat_info, 'username', channel_identifier)


    # Convert reactions dict to list of objects
    reactions_list = []
    if reactions_dict := processed_message.get('reactions'):
        for emoji, count in reactions_dict.items():
            # Assuming no custom/paid reactions in this simplified structure
            reactions_list.append(SimpleNamespace(emoji=emoji, count=count, is_paid=False, custom_emoji_id=None))
    
    # Recreate reactions structure expected by Pyrogram's reaction handling
    reactions_obj = SimpleNamespace(reactions=reactions_list) if reactions_list else None

    # Create the message-like object
    tg_message_mock = SimpleNamespace(
        id=processed_message.get('message_id'),
        date=datetime.fromtimestamp(processed_message['date'], tz=timezone.utc) if processed_message.get('date') else None,
        views=processed_message.get('views'),
        reactions=reactions_obj,
        chat=chat_info,
        # Add other attributes if generate_html_footer or its dependencies need them
        # For now, these seem sufficient based on the analysis of generate_html_footer
        # and _reactions_views_links.
        text=processed_message.get('text'), # Add text just in case
        caption=None, # Assume caption is merged into text by process_message
        forward_origin=None, # Not directly needed by footer generation logic itself
        reply_to_message=None, # Not directly needed by footer generation logic itself
        media=None, # Not needed by footer
        service=processed_message.get('service') # Potentially needed? Added just in case.
    )

    # Cast to Message type hint for static analysis, although it's a mock object
    return tg_message_mock # type: ignore


async def _render_messages_groups(messages_groups: list[list[Message]], 
                                    post_parser: PostParser, 
                                    exclude_flags: str | None = None, 
                                    exclude_text: str | None = None):
    """
    Render message groups into HTML format
    Args:
        messages_groups: List of message groups (each group is a list of messages)
        post_parser: PostParser instance
        exclude_flags: Comma-separated list of flags to exclude
        exclude_text: Text to exclude from posts (comma-separated phrases)
    Returns:
        List of rendered posts
    """
    rendered_posts = []
    
    for group in messages_groups:
        try:
            if len(group) == 1: # Single message - simple case
                one_message = group[0]
                message_data = post_parser.process_message(one_message)
                html_parts = [
                    f'<div class="message-body">{message_data["html"]["body"]}</div>',
                    f'<div class="message-footer">{message_data["html"]["footer"]}</div>'
                ]
                rendered_posts.append({
                    'html': '\n'.join(html_parts),
                    'date': message_data['date'],
                    'message_id': message_data['message_id'],
                    'title': message_data['html']['title'],
                    'text': message_data['text'],
                    'author': message_data['author'],
                    'flags': message_data['flags']
                })
            else: # Multiple messages in group - merge text and html body
                processed_messages = [post_parser.process_message(msg) for msg in group]

                # Determine main message for header/footer/title
                main_message = next(  
                    (msg for msg in processed_messages if msg['text']),
                    processed_messages[0]  # fallback if no message contains text
                )
            

                # Merge text fields from all messages
                all_texts = [msg['text'] for msg in processed_messages if msg['text']]
                combined_text = '\n'.join(all_texts)

                # Merge html body sections from all messages
                all_html_bodies = [msg['html']['body'] for msg in processed_messages if msg['html']['body']]
                combined_html_body = '\n<br><br>\n'.join(all_html_bodies)

                # Collect all unique flags from all messages in the group
                all_flags = set()
                for msg in processed_messages:
                    if msg.get('flags'): # Check if flags exist and are not empty
                        all_flags.update(msg['flags'])
                all_flags.add("merged")
                merged_flags = list(all_flags) # Convert back to list if needed, or keep as set

                # generate tg-message from processed message
                tg_message = processed_message_to_tg_message(main_message)


                footer_html = post_parser.generate_html_footer(tg_message, flags_list=merged_flags)

                html_parts = [
                    f'<div class="message-body">{combined_html_body}</div>',
                    f'<div class="message-footer">{footer_html}</div>'
                ]

                rendered_posts.append({
                    'html': '\n'.join(html_parts),
                    'date': main_message['date'],
                    'message_id': main_message['message_id'],
                    'title': main_message['html']['title'],
                    'text': combined_text,
                    'author': main_message['author'],
                    'flags': merged_flags
                })
                
        except Exception as e:
            logger.error(f"message_group_rendering_error: error {str(e)}")
            continue

    # Filter posts by exclude_flags
    if exclude_flags:
        exclude_flag_list = [flag.strip() for flag in exclude_flags.split(',')] # Split comma-separated flags into list
        filtered_posts = []
        for post in rendered_posts:
            # If "all" is specified and the post has any flags, exclude the post.
            if "all" in exclude_flag_list and post['flags']:
                continue
            # Exclude post if any flag in the exclude list is present in the post's flags.
            if any(flag in post['flags'] for flag in exclude_flag_list):
                continue
            filtered_posts.append(post)
        rendered_posts = filtered_posts
    
    # Filter posts by exclude_text
    if exclude_text:
        # Compile single regex pattern with UNICODE flag for proper handling of non-ASCII characters
        exclude_pattern = re.compile(exclude_text.strip(), re.IGNORECASE | re.UNICODE)
        filtered_posts = []
        for post in rendered_posts:
            # Check if pattern matches the post text
            if not exclude_pattern.search(post['text']):
                filtered_posts.append(post)
            else:
                logger.debug(f"excluded_post: message_id {post['message_id']}, pattern {exclude_pattern.pattern}")
        rendered_posts = filtered_posts
    
    # Sort by date
    rendered_posts.sort(key=lambda x: x['date'], reverse=True)
    return rendered_posts

async def generate_channel_rss(channel: str | int, 
                                client: Client, 
                                limit: int = 20, 
                                exclude_flags: str | None = None,
                                exclude_text: str | None = None,
                                merge_seconds: int = 5
                                ) -> str:
    """
    Generate RSS feed for channel using actual messages
    Args:
        channel: Telegram channel name
        post_parser: Optional PostParser instance. If not provided, will create new one
        client: Telegram client instance
        limit: Maximum number of posts to include in the RSS feed
        exclude_flags: Flags to exclude from the RSS feed
        exclude_text: Text to exclude from posts
    Returns:
        RSS feed as string in XML format
    """
    total_start_time = time.time()
    
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit}")
    if limit > 200:
        raise ValueError(f"limit cannot exceed 200, got {limit}")

    try:
        post_parser = PostParser(client=client)
            
        fg = FeedGenerator()
        fg.load_extension('dc')
        base_url = Config['pyrogram_bridge_url']
        
        channel_info_start_time = time.time()
        try:
            channel  = post_parser.channel_name_prepare(channel)
            channel_info = await post_parser.client.get_chat(channel)
            channel_title = channel_info.title or f"Telegram: {channel}"
            channel_username = channel_info.username or (str(channel_info.id) if channel_info.id and str(channel_info.id).startswith('-100') else None)
            if not channel_username:
                # Use prepared channel (which could be int) for error feed if username fails
                logger.warning(f"Could not get username for channel {channel}, using identifier for error feed.")
                return create_error_feed(str(channel), base_url) # Ensure channel is string for error feed
        except (errors.UsernameInvalid, errors.UsernameNotOccupied) as e:
            logger.warning(f"Channel not found error for {channel}: {str(e)}")
            return create_error_feed(channel, base_url)
        except Exception as e:
            logger.error(f"Error during get_chat for channel '{channel}' (type: {type(channel)}): {str(e)}", exc_info=True) # Log error specifically for get_chat
            # Re-raise the original exception to be caught by the outer handler if needed,
            # but add specific logging here.
            raise ValueError(f"Failed to get chat info for {channel}: {str(e)}") from e # Raise a more specific error perhaps
        
        channel_info_elapsed = time.time() - channel_info_start_time
        logger.debug(f"rss_channel_info_timing: channel {channel}, retrieved in {channel_info_elapsed:.3f} seconds")

        # Set feed metadata
        main_name = f"{channel_title} (@{channel_username})"
        fg.title(main_name)
        fg.link(href=f"https://t.me/{channel_username}", rel='alternate')
        fg.description(f'Telegram channel {channel_username} RSS feed')
        fg.language('ru')
        fg.id(f"{base_url}/rss/{channel_username}") # Use username for feed ID consistency
        
        # Collect messages
        messages_start_time = time.time()
        try:
            from tg_cache import cached_get_chat_history
            
            messages = await cached_get_chat_history(post_parser.client, channel, limit=limit*2)
        except Exception as e:
            logger.error(f"Error during get_chat_history for channel '{channel}' (type: {type(channel)}): {str(e)}", exc_info=True) # Log error specifically for get_chat_history
            raise ValueError(f"Failed to get chat history for {channel}: {str(e)}") from e # Raise a more specific error
            
        messages_elapsed = time.time() - messages_start_time
        logger.debug(f"rss_messages_retrieval_timing: channel {channel}, {len(messages)} messages retrieved in {messages_elapsed:.3f} seconds")
        
        # Process messages into groups and render them
        processing_start_time = time.time()
        if Config['time_based_merge']:
            messages = await _create_time_based_media_groups(messages, merge_seconds)
        message_groups = await _create_messages_groups(messages)
        message_groups = await _trim_messages_groups(message_groups, limit)
        final_posts = await _render_messages_groups(message_groups, post_parser, exclude_flags, exclude_text)
        
        processing_elapsed = time.time() - processing_start_time
        logger.debug(f"rss_messages_processing_timing: channel {channel}, {len(final_posts)} posts processed in {processing_elapsed:.3f} seconds")
        
        # Generate feed entries
        feed_gen_start_time = time.time()
        for post in final_posts:
            fe = fg.add_entry()
            fe.title(post['title'])
            
            post_link = f"https://t.me/{channel_username}/{post['message_id']}"
            fe.link(href=post_link)
            
            fe.description(post['text'].replace('\n', ' '))
            # Sanitize heavy HTML in thread to avoid blocking the loop
            try:
                def _sanitize_sync(html_raw: str) -> str:
                    css_sanitizer = CSSSanitizer(
                        allowed_css_properties=["max-width", "max-height", "object-fit", "width", "height"]
                    )
                    return HTMLSanitizer(
                        html_raw,
                        tags=['p', 'a', 'b', 'i', 'strong', 'em', 'ul', 'ol', 'li', 'br', 'div', 'span', 'img', 'video', 'audio', 'source'],
                        attributes={
                            'a': ['href', 'title', 'target'],
                            'img': ['src', 'alt', 'style'],
                            'video': ['controls', 'src', 'style'],
                            'audio': ['controls', 'style'],
                            'source': ['src', 'type'],
                            'div': ['class', 'style'],
                            'span': ['class']
                        },
                        protocols=['http', 'https', 'tg'],
                        css_sanitizer=css_sanitizer,
                        strip=True,
                    )
                sanitized_html = await asyncio.to_thread(_sanitize_sync, post['html'])
            except Exception as e:
                logger.error(f"rss_html_sanitization_error: channel {channel}, message_id {post['message_id']}, error {str(e)}")
                sanitized_html = post['html']
            fe.content(content=sanitized_html, type='CDATA')
            
            pub_date = datetime.fromtimestamp(post['date'], tz=timezone.utc)
            fe.pubDate(pub_date)
            fe.guid(post_link, permalink=True)
            
            if post['author'] and post['author'] != main_name:
                fe.author(name="", email=post['author'])
        
        feed_gen_elapsed = time.time() - feed_gen_start_time
        logger.debug(f"rss_feed_generation_timing: channel {channel}, feed generated in {feed_gen_elapsed:.3f} seconds")
        
        # Serialize RSS in thread (feedgen may be CPU-heavy)
        rss_feed = await asyncio.to_thread(fg.rss_str, pretty=True)
        if isinstance(rss_feed, bytes):
            return rss_feed.decode('utf-8')
        
        total_elapsed = time.time() - total_start_time
        logger.debug(f"rss_total_generation_timing: channel {channel}, total time {total_elapsed:.3f} seconds")
        return rss_feed
        
    except Exception as e:
        logger.error(f"generate_channel_rss: channel {channel}, error {str(e)}")
        raise

async def _reply_enrichment(client: Client, messages: list[Message]) -> list[Message]:
    """
    Enrich messages with reply to messages
    """
    for message in messages:
        if message.reply_to_message_id and message.chat:
            full_message = await client.get_messages(message.chat.id, message.id)
            if isinstance(full_message, list):
                if full_message and full_message[0].reply_to_message:
                    message.reply_to_message = full_message[0].reply_to_message
            else:
                if full_message and full_message.reply_to_message:
                    message.reply_to_message = full_message.reply_to_message

    return messages

async def generate_channel_html(channel: str | int, 
                                client: Client,
                                limit: int = 20, 
                                exclude_flags: str | None = None,
                                exclude_text: str | None = None,
                                merge_seconds: int = 5
                                ) -> str:
    """
    Generate HTML feed for channel using actual messages
    Args:
        channel: Telegram channel name
        post_parser: Optional PostParser instance. If not provided, will create new one
        client: Telegram client instance
        limit: Maximum number of posts to include in the RSS feed
        exclude_flags: Flags to exclude from the RSS feed
        exclude_text: Text to exclude from posts
    Returns:
        HTML feed as string
    """
    total_start_time = time.time()
    
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit}")
    if limit > 200:
        raise ValueError(f"limit cannot exceed 200, got {limit}")

    try:
        post_parser = PostParser(client=client)
            
        base_url = Config['pyrogram_bridge_url']
        
        channel_info_start_time = time.time()
        try:
            channel = post_parser.channel_name_prepare(channel)
            logger.debug(f"Prepared channel identifier for HTML: {channel} (type: {type(channel)})") # Log prepared channel
            channel_info = await post_parser.client.get_chat(channel)
            channel_username = channel_info.username or (str(channel_info.id) if channel_info.id and str(channel_info.id).startswith('-100') else None)
            if not channel_username:
                logger.warning(f"Could not get username for channel {channel} in HTML generation, returning error feed structure (as string). NOTE: This should ideally return HTML error page.")
                # For HTML, returning an error feed string might not be ideal. Consider returning a dedicated HTML error page.
                return create_error_feed(str(channel), base_url) # Ensure channel is string for error feed
        except (errors.UsernameInvalid, errors.UsernameNotOccupied) as e:
            logger.warning(f"Channel not found error for {channel} in HTML generation: {str(e)}")
            # Consider returning a dedicated HTML error page.
            return create_error_feed(channel, base_url)
        except Exception as e:
            logger.error(f"Error during get_chat for channel '{channel}' (type: {type(channel)}) in HTML generation: {str(e)}", exc_info=True)
            raise ValueError(f"Failed to get chat info for {channel} in HTML generation: {str(e)}") from e

        channel_info_elapsed = time.time() - channel_info_start_time
        logger.debug(f"html_channel_info_timing: channel {channel}, retrieved in {channel_info_elapsed:.3f} seconds")

        # Collect messages
        messages_start_time = time.time()
        try:
            from tg_cache import cached_get_chat_history
            
            messages = await cached_get_chat_history(post_parser.client, channel, limit=limit)
        except Exception as e:
            logger.error(f"Error during get_chat_history for channel '{channel}' (type: {type(channel)}) in HTML generation: {str(e)}", exc_info=True)
            raise ValueError(f"Failed to get chat history for {channel} in HTML generation: {str(e)}") from e

        messages_elapsed = time.time() - messages_start_time
        logger.debug(f"html_messages_retrieval_timing: channel {channel}, {len(messages)} messages retrieved in {messages_elapsed:.3f} seconds")

        # Enrich messages with reply to messages
        enrichment_start_time = time.time()
        messages = await _reply_enrichment(client, messages)
        enrichment_elapsed = time.time() - enrichment_start_time
        logger.debug(f"html_reply_enrichment_timing: channel {channel}, replies enriched in {enrichment_elapsed:.3f} seconds")

        # Process messages into groups and render them
        processing_start_time = time.time()
        if Config['time_based_merge']:
            messages = await _create_time_based_media_groups(messages, merge_seconds)

        # Process messages into groups and render them
        message_groups = await _create_messages_groups(messages)
        message_groups = await _trim_messages_groups(message_groups, limit)
        final_posts = await _render_messages_groups(message_groups, post_parser, exclude_flags, exclude_text)
        
        processing_elapsed = time.time() - processing_start_time
        logger.debug(f"html_messages_processing_timing: channel {channel}, {len(final_posts)} posts processed in {processing_elapsed:.3f} seconds")

        # Generate HTML content
        html_gen_start_time = time.time()
        # Concatenate HTML in thread to avoid blocking the loop on large payloads
        html_posts = [post['html'] for post in final_posts]
        def _concat_html(parts: list[str]) -> str:
            return '\n<hr class="post-divider">\n'.join(parts)
        html = await asyncio.to_thread(_concat_html, html_posts)
        
        # Optionally re-sanitize the final big HTML to ensure safety without blocking the loop
        try:
            def _sanitize_sync(html_raw: str) -> str:
                css_sanitizer = CSSSanitizer(
                    allowed_css_properties=["max-width", "max-height", "object-fit", "width", "height"]
                )
                return HTMLSanitizer(
                    html_raw,
                    tags=['p', 'a', 'b', 'i', 'strong', 'em', 'ul', 'ol', 'li', 'br', 'div', 'span', 'img', 'video', 'audio', 'source'],
                    attributes={
                        'a': ['href', 'title', 'target'],
                        'img': ['src', 'alt', 'style'],
                        'video': ['controls', 'src', 'style'],
                        'audio': ['controls', 'style'],
                        'source': ['src', 'type'],
                        'div': ['class', 'style'],
                        'span': ['class']
                    },
                    protocols=['http', 'https', 'tg'],
                    css_sanitizer=css_sanitizer,
                    strip=True,
                )
            html = await asyncio.to_thread(_sanitize_sync, html)
        except Exception as e:
            logger.error(f"html_final_sanitization_error: channel {channel}, error {str(e)}")
        
        html_gen_elapsed = time.time() - html_gen_start_time
        logger.debug(f"html_generation_timing: channel {channel}, HTML generated in {html_gen_elapsed:.3f} seconds")
        
        total_elapsed = time.time() - total_start_time
        logger.debug(f"html_total_generation_timing: channel {channel}, total time {total_elapsed:.3f} seconds")
        
        return html
        
    except Exception as e:
        logger.error(f"html_generation_error: channel {channel}, error {str(e)}")
        raise

def create_error_feed(channel: str | int, base_url: str) -> str:
    """
    Create an empty RSS feed with metadata indicating an error when the channel is not found.
    Args:
        channel: Telegram channel name
        base_url: Base URL for RSS feed
    Returns:
        Empty RSS feed as string in XML format.
    """
    fg = FeedGenerator()
    
    fg.title(f"Error: Channel @{channel} not found")
    fg.link(href=f"https://t.me/{channel}", rel='alternate')
    fg.description(f'Error: Telegram channel @{channel} does not exist')
    fg.language('en')
    fg.id(f"{base_url}/rss/{channel}")
    
    fe = fg.add_entry()
    fe.title("Channel not found")
    # Use the original channel string identifier passed to the function for links/text
    fe.link(href=f"https://t.me/{channel}")
    error_html = f"<p>The Telegram channel @{channel} does not exist or is not accessible.</p>"
    fe.description(f"{error_html}")
    fe.content(content=error_html, type='CDATA')
    fe.pubDate(datetime.now(tz=timezone.utc))
    fe.guid(f"https://t.me/{channel}", permalink=True)

    rss_feed = fg.rss_str(pretty=True)
    if isinstance(rss_feed, bytes):
        return rss_feed.decode('utf-8')
    return rss_feed
