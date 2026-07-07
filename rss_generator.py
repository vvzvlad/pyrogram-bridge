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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from feedgen.feed import FeedGenerator
from pyrogram import errors, Client
from pyrogram.types import Message
from post_parser import PostParser
from config import get_settings
from tg_throttle import tg_rpc_bounded
from sanitizer import sanitize_html

Config = get_settings()

logger = logging.getLogger(__name__)


@dataclass
class PreparedFeed:
    """Output of _prepare_feed_posts — everything both formatters need after
    fetch -> enrich -> render -> filter -> sanitize."""
    channel_username: str
    channel_title: str            # used by RSS metadata only
    posts: list[dict]             # rendered, filtered, sorted, SANITIZED


class ChannelNotFound(Exception):
    """Channel could not be resolved to a username; formatter -> create_error_feed."""
    def __init__(self, channel_identifier):
        # The prepared identifier (str or int), rendered into the error feed.
        self.channel_identifier = channel_identifier
        super().__init__(str(channel_identifier))

def _compute_time_based_group_ids(messages: list[Message], merge_seconds: int = 5) -> dict[int, str | int]:
    """Return {message.id: effective_media_group_id} WITHOUT mutating messages.

    Plain synchronous function (contains no await): runs inside the render thread
    via _render_pipeline. Must not touch asyncio.

    Contract: all messages belong to ONE chat (message.id is unique only per
    chat); callers must not mix chats in a single call.

    Pure re-implementation of the old _create_time_based_media_groups mutation,
    reproducing its result for every input the old code survived on PRODUCTION
    data (naive kurigram dates). Inputs only aware-date test mocks could produce
    (fully-None and aware+None mixes, where the old code clustered the None-date
    tail by insertion order incl. truthy-id adoption) are deliberately replaced
    (registry §3.11):
      - messages WITHOUT a date do not participate in time clustering and get
        NO mapping entry (their own media_group_id still applies downstream);
      - dated messages are ordered ascending by date via a timestamp key
        (naive-safe: no aware/naive mix once None-date are excluded);
      - a message joins the current cluster if the gap to the PREVIOUS message
        is <= merge_seconds; the gap is a NAIVE datetime subtraction
        (msg.date - prev.date).total_seconds(), exactly as the old code — NOT a
        timestamp diff (they diverge across a DST fold, and the old behavior is
        the contract);
      - the effective id of a cluster is the FIRST TRUTHY media_group_id in
        cluster order (old code used truthiness, not `is not None`, and
        overwrote members' own differing ids — kept);
      - a cluster of >= 2 members with no truthy id gets a synthetic
        f"time_{min(dates)}" id (exact old format);
      - singleton clusters and clusters with no effective id produce NO entries;
        every member of a cluster with an effective id gets one.
    """
    dated = sorted((m for m in messages if m.date is not None),
                   key=lambda m: m.date.timestamp())  # type: ignore
    group_ids: dict[int, str | int] = {}

    def _flush(cluster: list[Message], effective: str | int | None) -> None:
        if len(cluster) < 2:
            return
        if not effective:
            # All members are dated here (None-date excluded above), so min() is safe.
            effective = f"time_{min(m.date for m in cluster)}"  # type: ignore
        for m in cluster:
            group_ids[m.id] = effective

    cluster: list[Message] = []
    effective: str | int | None = None
    prev_date: Optional[datetime] = None

    for msg in dated:
        mgid = getattr(msg, "media_group_id", None)
        if not cluster:
            cluster = [msg]
            effective = mgid or None
            prev_date = msg.date
            continue
        time_diff = (msg.date - prev_date).total_seconds()  # type: ignore
        if time_diff <= merge_seconds:
            cluster.append(msg)
            # First truthy id in cluster order wins; keep it even if this member
            # carries a different truthy id (the old code overwrote it).
            if not effective and mgid:
                effective = mgid
            prev_date = msg.date
        else:
            _flush(cluster, effective)
            cluster = [msg]
            effective = mgid or None
            prev_date = msg.date

    _flush(cluster, effective)
    return group_ids

def _create_messages_groups(messages: list[Message], group_ids: dict[int, str | int] | None = None) -> list[list[Message]]:
    """
    Process messages into formatted posts, handling media groups

    Plain synchronous function (contains no await): runs inside the render thread.
    """
    processing_groups: list[list[Message]] = []
    media_groups: dict[str | int, list[Message]] = {}
    # Time-clustering supplies effective ids as a PURE mapping (no message mutation,
    # no deepcopy). Absent an entry, the message's own media_group_id applies
    # (registry §3.11 / spec Этап 4).
    group_ids = group_ids or {}

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

            effective_group_id = group_ids.get(message.id, message.media_group_id)
            if effective_group_id:
                if effective_group_id not in media_groups:
                    media_groups[effective_group_id] = []
                media_groups[effective_group_id].append(message)
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
    
    # Sort processing groups by date of first message in each group. Timestamp-based key
    # is naive-safe: kurigram dates are naive-local, and a None-date group used to fall
    # back to an AWARE datetime.now(timezone.utc) here, raising TypeError on the first
    # naive-vs-aware comparison — a 500 on ANY feed carrying a None-date post, in the
    # DEFAULT path (registry §3.12). None-date groups now sort as newest (float('inf'))
    # and deterministically survive the later [:limit] slice; the final post sort in
    # _render_messages_groups (0.0 fallback) still places them at the tail of the feed.
    processing_groups.sort(key=lambda group: group[0].date.timestamp() if group[0].date else float('inf'), reverse=True)

    return processing_groups

def _trim_messages_groups(messages_groups: list[list[Message]], limit: int):
    """
    Trim messages groups to limit

    Plain synchronous function (contains no await): runs inside the render thread.
    """
    if len(messages_groups) > limit: # Trim groups if they exceed the specified limit
        messages_groups = messages_groups[:limit]
    
    return messages_groups

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
                # Feed path: raw_message not needed and sanitize deferred to the per-post
                # pass in _render_pipeline (both RSS and HTML), so each fragment is
                # sanitized exactly once.
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

                # Determine the main message with the SAME criterion the processed dicts
                # used: first message that has text or caption, else the first of the
                # group. processed_messages[i] corresponds to group[i], so main_message
                # (dict, for title/date/author) and main_raw (the real Message, for the
                # footer) point at the same index.
                main_idx = next((i for i, m in enumerate(group) if (m.text or m.caption)), 0)
                main_raw = group[main_idx]
                main_message = processed_messages[main_idx]

                # Merge text fields from all messages
                all_texts = [msg['text'] for msg in processed_messages if msg['text']]
                combined_text = '\n'.join(all_texts)

                # Merge html body sections from all messages
                all_html_bodies = [msg['html']['body'] for msg in processed_messages if msg['html']['body']]
                combined_html_body = '\n<br><br>\n'.join(all_html_bodies)

                # Deterministic merged flags: first-seen order across the group, then
                # 'merged' (registry §3.8 — replaces the hash-ordered list(set(...))).
                merged_flags = list(dict.fromkeys(f for msg in processed_messages for f in msg['flags']))
                merged_flags.append("merged")

                # Render the merged footer DIRECTLY from the real main Message — no
                # dict->mock round-trip. The raw Message carries its real reactions
                # (custom emojis get their own span, registry §3.6) and its naive-local
                # date (registry §3.7), matching a single post of the same message.
                footer_html = post_parser.generate_html_footer(main_raw, flags_list=merged_flags)

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
                     time_based_merge: bool,
                     channel: str | int):
    """
    Full synchronous feed render pipeline (grouping + trimming + rendering + sanitize).

    Runs entirely in a worker thread via a single asyncio.to_thread call. It contains
    NO await, NO asyncio primitives, NO create_task/get_running_loop — all the CPU-heavy
    work (grouping, rendering, bleach) happens here off the event loop.
    Media file-id records are accumulated on post_parser._pending_media_ids and flushed
    by the caller after this returns.

    `channel` is used only for the sanitize log_context (grep-ability).
    """
    if time_based_merge:
        # Pure mapping msg.id -> effective_group_id (no message mutation, no deepcopy),
        # plus a date-ASC pre-sort with the same naive-safe timestamp key (None-date last
        # via +inf). A stable sorted() on the fetch-order input reproduces the old
        # `messages_sorted` order — including ties — and does NOT mutate the input, so the
        # cache-protecting deepcopy is gone (spec Этап 4).
        group_ids = _compute_time_based_group_ids(messages, merge_seconds)
        messages = sorted(messages, key=lambda m: m.date.timestamp() if m.date else float('inf'))
        message_groups = _create_messages_groups(messages, group_ids)
    else:
        message_groups = _create_messages_groups(messages)
    message_groups = _trim_messages_groups(message_groups, limit)
    posts = _render_messages_groups(message_groups, post_parser, exclude_flags, exclude_text)
    # Sanitize each surviving (post-filter) post exactly once, here in the worker
    # thread — no per-post thread hop / per-post CSSSanitizer anymore. Per-post
    # granularity means a failed post is html.escape()d in isolation instead of the
    # whole feed (registry §3.5), and an unbalanced fragment is normalized within its
    # OWN post rather than swallowing the following ones (registry §3.4). sanitize_html
    # is fail-closed (registry §3.2). For the HTML path the <hr> divider is joined in
    # the formatter AFTER this, so it survives sanitize (registry §3.3).
    for post in posts:
        post['html'] = sanitize_html(
            post['html'],
            log_context=f"channel {channel}, message_id {post['message_id']}",
        )
    return posts


async def _prepare_feed_posts(channel: str | int,
                              client: Client,
                              *,
                              limit: int,
                              exclude_flags: str | None,
                              exclude_text: str | None,
                              merge_seconds: int,
                              history_limit: int,
                              enrich_replies: bool,
                              log_prefix: str) -> PreparedFeed:
    """Single code path feeding both feeds: validate -> resolve chat -> fetch history ->
    (optionally) enrich replies -> render/filter/sort/sanitize in one worker thread. The
    RSS/HTML formatters used to duplicate this ~80% verbatim; path differences are now
    EXPLICIT parameters (history_limit: RSS over-fetches limit*2; enrich_replies: HTML-only;
    log_prefix 'rss'|'html' keeps today's paired log-line names).

    Raises ChannelNotFound (formatter -> create_error_feed), re-raises errors.FloodWait
    for both the get_chat and the get_history path (§3.9; api_server -> HTTP 429), and
    wraps any other resolution/history error in ValueError with unified text (§3.10).
    """
    # 1) Validate limit.
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit}")
    if limit > 200:
        raise ValueError(f"limit cannot exceed 200, got {limit}")

    post_parser = PostParser(client=client)

    # 2) Resolve the chat. NOTE: keep `from tg_cache import ...` INSIDE this function —
    # feed tests monkeypatch tg_cache and rely on late name resolution.
    channel_info_start_time = time.time()
    try:
        channel = post_parser.channel_name_prepare(channel)
        from tg_cache import cached_get_chat
        channel_info = await cached_get_chat(post_parser.client, channel)
        channel_title = channel_info.title or f"Telegram: {channel}"
        channel_username = channel_info.username or (str(channel_info.id) if channel_info.id and str(channel_info.id).startswith('-100') else None)
        if not channel_username:
            # Prepared channel (which could be int) is carried into the error feed.
            logger.warning(f"Could not get username for channel {channel}, using identifier for error feed.")
            raise ChannelNotFound(channel)
    except (errors.UsernameInvalid, errors.UsernameNotOccupied) as e:
        logger.warning(f"Channel not found error for {channel}: {str(e)}")
        raise ChannelNotFound(channel) from e
    except errors.FloodWait:
        # Let FloodWait bubble up to api_server.py, which returns HTTP 429 with Retry-After.
        raise
    except ChannelNotFound:
        # The no-username branch above — not a resolution failure to wrap in ValueError.
        raise
    except Exception as e:
        logger.error(f"Error during get_chat for channel '{channel}' (type: {type(channel)}): {str(e)}", exc_info=True)
        raise ValueError(f"Failed to get chat info for {channel}: {str(e)}") from e

    channel_info_elapsed = time.time() - channel_info_start_time
    logger.debug(f"{log_prefix}_channel_info_timing: channel {channel}, retrieved in {channel_info_elapsed:.3f} seconds")

    # 3) Fetch history.
    messages_start_time = time.time()
    try:
        from tg_cache import cached_get_chat_history
        messages = await cached_get_chat_history(post_parser.client, channel, limit=history_limit)
    except errors.FloodWait:
        # §3.9: FloodWait from history propagates -> HTTP 429 (previously wrapped -> 400).
        raise
    except Exception as e:
        logger.error(f"Error during get_chat_history for channel '{channel}' (type: {type(channel)}): {str(e)}", exc_info=True)
        raise ValueError(f"Failed to get chat history for {channel}: {str(e)}") from e

    messages_elapsed = time.time() - messages_start_time
    logger.debug(f"{log_prefix}_messages_retrieval_timing: channel {channel}, {len(messages)} messages retrieved in {messages_elapsed:.3f} seconds")

    # 4) Optional reply enrichment (HTML-only today — deliberate, keeps RSS polling cheap).
    if enrich_replies:
        enrichment_start_time = time.time()
        messages = await _reply_enrichment(client, messages)
        enrichment_elapsed = time.time() - enrichment_start_time
        logger.debug(f"{log_prefix}_reply_enrichment_timing: channel {channel}, replies enriched in {enrichment_elapsed:.3f} seconds")

    # 5) Process messages into groups and render them. The whole grouping/trimming/
    # rendering pipeline is CPU-heavy (per-message rendering) and contains no
    # await, so run it in ONE worker thread to keep the event loop responsive.
    processing_start_time = time.time()
    try:
        posts = await asyncio.to_thread(
            _render_pipeline, messages, post_parser, limit,
            exclude_flags, exclude_text, merge_seconds, Config['time_based_merge'],
            channel,
        )
    finally:
        # Persist media file-ids collected during rendering with a single bulk upsert —
        # in a finally so a partial render still records what it collected (the flush is
        # best-effort and swallows its own errors, so it cannot mask a render exception).
        await post_parser._flush_pending_media_ids()

    processing_elapsed = time.time() - processing_start_time
    logger.debug(f"{log_prefix}_messages_processing_timing: channel {channel}, {len(posts)} posts processed in {processing_elapsed:.3f} seconds")

    return PreparedFeed(channel_username=channel_username, channel_title=channel_title, posts=posts)


async def generate_channel_rss(channel: str | int,
                                client: Client,
                                limit: int = 20,
                                exclude_flags: str | None = None,
                                exclude_text: str | None = None,
                                merge_seconds: int = 5
                                ) -> str:
    """
    Generate RSS feed for channel using actual messages.

    Thin formatter over the shared _prepare_feed_posts: it only turns the prepared,
    sanitized posts into a feedgen RSS document. RSS over-fetches (history_limit=limit*2)
    and skips reply enrichment (enrich_replies=False).
    Args:
        channel: Telegram channel name
        client: Telegram client instance
        limit: Maximum number of posts to include in the RSS feed
        exclude_flags: Flags to exclude from the RSS feed
        exclude_text: Text to exclude from posts
    Returns:
        RSS feed as string in XML format
    """
    total_start_time = time.time()
    base_url = Config['pyrogram_bridge_url']

    try:
        prepared = await _prepare_feed_posts(
            channel, client,
            limit=limit, exclude_flags=exclude_flags, exclude_text=exclude_text,
            merge_seconds=merge_seconds, history_limit=limit * 2,
            enrich_replies=False, log_prefix="rss",
        )

        channel_username = prepared.channel_username
        channel_title = prepared.channel_title
        final_posts = prepared.posts

        fg = FeedGenerator()
        fg.load_extension('dc')

        # Set feed metadata
        main_name = f"{channel_title} (@{channel_username})"
        fg.title(main_name)
        fg.link(href=f"https://t.me/{channel_username}", rel='alternate')
        fg.description(f'Telegram channel {channel_username} RSS feed')
        fg.language('ru')
        fg.id(f"{base_url}/rss/{channel_username}") # Use username for feed ID consistency

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
            # post['html'] is already sanitized per-post inside _render_pipeline
            # (single project-wide bleach config, fail-closed). No per-post thread
            # hop / CSSSanitizer here anymore.
            fe.content(content=post['html'], type='CDATA')
            
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

    except ChannelNotFound as e:
        return create_error_feed(str(e.channel_identifier), base_url)
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
    Generate HTML feed for channel using actual messages.

    Thin formatter over the shared _prepare_feed_posts: it only joins the prepared,
    sanitized posts with the <hr> divider. HTML fetches exactly `limit` messages and
    enables reply enrichment (enrich_replies=True).
    Args:
        channel: Telegram channel name
        client: Telegram client instance
        limit: Maximum number of posts to include in the HTML feed
        exclude_flags: Flags to exclude from the HTML feed
        exclude_text: Text to exclude from posts
    Returns:
        HTML feed as string
    """
    total_start_time = time.time()
    base_url = Config['pyrogram_bridge_url']

    try:
        prepared = await _prepare_feed_posts(
            channel, client,
            limit=limit, exclude_flags=exclude_flags, exclude_text=exclude_text,
            merge_seconds=merge_seconds, history_limit=limit,
            enrich_replies=True, log_prefix="html",
        )

        final_posts = prepared.posts

        # Generate HTML content.
        html_gen_start_time = time.time()
        # Each post is already sanitized per-post inside _render_pipeline. Join with the
        # <hr> divider AFTER sanitize so the divider survives (registry §3.3), and each
        # post's DOM was normalized within its own fragment (registry §3.4). The join is
        # a trivial string op — no worker thread needed.
        html = '\n<hr class="post-divider">\n'.join(post['html'] for post in final_posts)

        html_gen_elapsed = time.time() - html_gen_start_time
        logger.debug(f"html_generation_timing: channel {channel}, HTML generated in {html_gen_elapsed:.3f} seconds")
        
        total_elapsed = time.time() - total_start_time
        logger.debug(f"html_total_generation_timing: channel {channel}, total time {total_elapsed:.3f} seconds")
        
        return html

    except ChannelNotFound as e:
        return create_error_feed(str(e.channel_identifier), base_url)
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
