#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation
# pylance: disable=reportMissingImports, reportMissingModuleSource
# mypy: disable-error-code="import-untyped"

import copy
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
from tg_throttle import tg_rpc_bounded
from file_io import upsert_media_file_ids_bulk_sync, DB_PATH
from bleach.css_sanitizer import CSSSanitizer
from bleach import clean as HTMLSanitizer

Config = get_settings()

logger = logging.getLogger(__name__)

def _create_time_based_media_groups(messages: list[Message], merge_seconds: int = 5) -> list[Message]:
    """
    Create media groups based on time difference between messages

    Plain synchronous function (contains no await): runs inside the render thread
    via _render_pipeline. Must not touch asyncio.
    """
    # Deep-copy the input list to avoid mutating cached Message objects (the cache may
    # reuse the same objects across calls with different merge_seconds values)
    messages = copy.deepcopy(messages)
    # Compute fallback once so all None-date messages get the same sort key (deterministic order)
    _sort_fallback = datetime.now(timezone.utc)
    messages_sorted = sorted(messages, key=lambda msg: msg.date or _sort_fallback) # type: ignore
    cluster: list[Message] = []
    last_msg_date: datetime = datetime.now(timezone.utc)
    current_media_group_id: Optional[str] = None

    for msg in messages_sorted:
        
        if not cluster:
            cluster.append(msg)
            # Use current time as fallback when date is None
            last_msg_date = msg.date or datetime.now(timezone.utc) # type: ignore
            current_media_group_id = getattr(msg, "media_group_id", None)
            continue
        
        # Use current time as fallback when date is None to avoid TypeError in subtraction
        msg_date = msg.date or datetime.now(timezone.utc)
        time_diff = (msg_date - last_msg_date).total_seconds()
        
        msg_media_group_id = getattr(msg, "media_group_id", None)
        
        if time_diff <= merge_seconds:
            if current_media_group_id:
                msg.media_group_id = current_media_group_id  # type: ignore
            elif msg_media_group_id:
                current_media_group_id = msg_media_group_id
                for m in cluster:
                    m.media_group_id = current_media_group_id  # type: ignore
            cluster.append(msg)
            # Use current time as fallback when date is None
            last_msg_date = msg.date or datetime.now(timezone.utc) # type: ignore
        else:
            if len(cluster) >= 2 and not current_media_group_id:
                dates = [m.date for m in cluster if m.date is not None]
                if dates:
                    min_date = min(dates)
                    new_group_id = f"time_{min_date}"
                    for m in cluster:
                        m.media_group_id = new_group_id  # type: ignore
            cluster = [msg]
            # Use current time as fallback when date is None
            last_msg_date = msg.date or datetime.now(timezone.utc) # type: ignore
            current_media_group_id = msg_media_group_id
    
    if len(cluster) >= 2 and not current_media_group_id:
        dates = [m.date for m in cluster if m.date is not None]
        if dates:
            min_date = min(dates)
            new_group_id = f"time_{min_date}"
            for m in cluster:
                m.media_group_id = new_group_id  # type: ignore

    return messages_sorted

def _create_messages_groups(messages: list[Message]) -> list[list[Message]]:
    """
    Process messages into formatted posts, handling media groups

    Plain synchronous function (contains no await): runs inside the render thread.
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

def _trim_messages_groups(messages_groups: list[list[Message]], limit: int):
    """
    Trim messages groups to limit

    Plain synchronous function (contains no await): runs inside the render thread.
    """
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


def _render_messages_groups(messages_groups: list[list[Message]],
                                    post_parser: PostParser,
                                    exclude_flags: str | None = None,
                                    exclude_text: str | None = None):
    """
    Render message groups into HTML format
    Plain synchronous function (contains no await): runs inside the render thread.
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
                # Feed path: raw_message not needed and sanitize deferred to the final
                # whole-feed pass, so each fragment is sanitized exactly once.
                message_data = post_parser.process_message(one_message, include_raw=False, sanitize=False)
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
                processed_messages = [post_parser.process_message(msg, include_raw=False, sanitize=False) for msg in group]

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
    
    # Sort by date; use 0.0 (epoch) as fallback for posts with None date to avoid TypeError
    rendered_posts.sort(key=lambda x: x['date'] if x['date'] is not None else 0.0, reverse=True)
    return rendered_posts


def _render_pipeline(messages: list[Message],
                     post_parser: PostParser,
                     limit: int,
                     exclude_flags: str | None,
                     exclude_text: str | None,
                     merge_seconds: int,
                     time_based_merge: bool):
    """
    Full synchronous feed render pipeline (grouping + trimming + rendering).

    Runs entirely in a worker thread via a single asyncio.to_thread call. It contains
    NO await, NO asyncio primitives, NO create_task/get_running_loop — all the CPU-heavy
    work (deepcopy, grouping, bleach-free rendering) happens here off the event loop.
    Media file-id records are accumulated on post_parser._pending_media_ids and flushed
    by the caller after this returns.
    """
    if time_based_merge:
        messages = _create_time_based_media_groups(messages, merge_seconds)
    message_groups = _create_messages_groups(messages)
    message_groups = _trim_messages_groups(message_groups, limit)
    return _render_messages_groups(message_groups, post_parser, exclude_flags, exclude_text)


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
            from tg_cache import cached_get_chat
            channel_info = await cached_get_chat(post_parser.client, channel)
            channel_title = channel_info.title or f"Telegram: {channel}"
            channel_username = channel_info.username or (str(channel_info.id) if channel_info.id and str(channel_info.id).startswith('-100') else None)
            if not channel_username:
                # Use prepared channel (which could be int) for error feed if username fails
                logger.warning(f"Could not get username for channel {channel}, using identifier for error feed.")
                return create_error_feed(str(channel), base_url) # Ensure channel is string for error feed
        except (errors.UsernameInvalid, errors.UsernameNotOccupied) as e:
            logger.warning(f"Channel not found error for {channel}: {str(e)}")
            return create_error_feed(channel, base_url)
        except errors.FloodWait:
            # Let FloodWait bubble up to api_server.py, which returns HTTP 429 with Retry-After header
            raise
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
        
        # Process messages into groups and render them.
        # The whole grouping/trimming/rendering pipeline is CPU-heavy (deepcopy +
        # per-message rendering) and contains no await, so run it in ONE worker
        # thread to keep the event loop responsive.
        processing_start_time = time.time()
        try:
            final_posts = await asyncio.to_thread(
                _render_pipeline, messages, post_parser, limit,
                exclude_flags, exclude_text, merge_seconds, Config['time_based_merge'],
            )
        finally:
            # Persist media file-ids collected during rendering with a single bulk
            # upsert — in a finally so a partial render still records what it collected
            # (the flush is best-effort and swallows its own errors, so it cannot mask a
            # render exception).
            await post_parser._flush_pending_media_ids()

        processing_elapsed = time.time() - processing_start_time
        logger.debug(f"rss_messages_processing_timing: channel {channel}, {len(final_posts)} posts processed in {processing_elapsed:.3f} seconds")
        
        # Generate feed entries
        feed_gen_start_time = time.time()
        
        # Log date range of posts being added to RSS
        if final_posts:
            dates = [datetime.fromtimestamp(post['date'], tz=timezone.utc) for post in final_posts if post.get('date')]
            if dates:
                oldest_date = min(dates)
                newest_date = max(dates)
                logger.info(f"rss_date_range: channel {channel}, oldest_post {oldest_date.isoformat()}, newest_post {newest_date.isoformat()}, total_posts {len(final_posts)}")
        
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
            
            if post['date'] is not None:
                pub_date = datetime.fromtimestamp(post['date'], tz=timezone.utc)
                logger.debug(f"rss_entry_date: channel {channel}, message_id {post['message_id']}, timestamp {post['date']}, pub_date {pub_date.isoformat()}")
                fe.pubDate(pub_date)
            else:
                # Date is None (e.g. service or deleted message) — fall back to current time
                pub_date = datetime.now(tz=timezone.utc)
                logger.warning(f"rss_entry_missing_date: channel {channel}, message_id {post['message_id']}, using current time as fallback")
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
    Enrich messages with reply-to messages.

    Instead of one API call per message, replies are batched: all message IDs
    that need enrichment are grouped by chat_id and fetched in a single
    client.get_messages() call per chat_id.
    """
    # Collect messages that need reply enrichment, grouped by chat_id
    chat_messages: dict[int, list[Message]] = {}
    for message in messages:
        if message.reply_to_message_id and message.chat:
            chat_id = message.chat.id
            if chat_id not in chat_messages:
                chat_messages[chat_id] = []
            chat_messages[chat_id].append(message)

    if not chat_messages:
        # No messages with replies — return unchanged
        return messages

    # Build a lookup: {(chat_id, message_id): full_message} using one batch call per chat
    reply_lookup: dict[tuple[int, int], Message] = {}
    for chat_id, chat_msgs in chat_messages.items():
        ids_to_fetch = [m.id for m in chat_msgs]
        try:
            # Throttle under the global RPC gate and bound the call via the shared
            # tg_rpc_bounded so a hung get_messages cannot pin the gate.
            async with tg_rpc_bounded(Config["tg_rpc_timeout"]):
                fetched = await client.get_messages(chat_id, ids_to_fetch)
            # get_messages may return a single Message or a list
            if not isinstance(fetched, list):
                fetched = [fetched]
            for fm in fetched:
                if fm and not getattr(fm, 'empty', False):
                    reply_lookup[(chat_id, fm.id)] = fm
        except Exception as e:
            logger.error(f"reply_enrichment_batch_error: chat_id {chat_id}, ids {ids_to_fetch}, error {str(e)}")

    # Apply reply_to_message from lookup
    for message in messages:
        if message.reply_to_message_id and message.chat:
            key = (message.chat.id, message.id)
            full_message = reply_lookup.get(key)
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
            from tg_cache import cached_get_chat
            channel_info = await cached_get_chat(post_parser.client, channel)
            channel_username = channel_info.username or (str(channel_info.id) if channel_info.id and str(channel_info.id).startswith('-100') else None)
            if not channel_username:
                logger.warning(f"Could not get username for channel {channel} in HTML generation, returning error feed structure (as string). NOTE: This should ideally return HTML error page.")
                # For HTML, returning an error feed string might not be ideal. Consider returning a dedicated HTML error page.
                return create_error_feed(str(channel), base_url) # Ensure channel is string for error feed
        except (errors.UsernameInvalid, errors.UsernameNotOccupied) as e:
            logger.warning(f"Channel not found error for {channel} in HTML generation: {str(e)}")
            # Consider returning a dedicated HTML error page.
            return create_error_feed(channel, base_url)
        except errors.FloodWait:
            # Let FloodWait bubble up to api_server.py, which returns HTTP 429 with Retry-After header
            raise
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

        # Process messages into groups and render them in ONE worker thread
        # (CPU-heavy, no await) to keep the event loop responsive.
        processing_start_time = time.time()
        try:
            final_posts = await asyncio.to_thread(
                _render_pipeline, messages, post_parser, limit,
                exclude_flags, exclude_text, merge_seconds, Config['time_based_merge'],
            )
        finally:
            # Persist media file-ids collected during rendering with a single bulk
            # upsert — in a finally so a partial render still records what it collected.
            await post_parser._flush_pending_media_ids()

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
