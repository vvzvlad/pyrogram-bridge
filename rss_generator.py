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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from feedgen.feed import FeedGenerator
from pyrogram import errors, Client
from pyrogram.types import Message
from post_parser import PostParser, _wrap_post_html
from config import get_settings
from tg_throttle import tg_rpc_bounded
from sanitizer import sanitize_html

Config = get_settings()

logger = logging.getLogger(__name__)

# XML 1.0 forbids most C0 control chars (\x00-\x08, \x0B, \x0C, \x0E-\x1F) and lone
# surrogate code points (\uD800-\uDFFF, which in a Python str only ever appear
# unpaired — a real non-BMP char is a single code point > \uFFFF, never a surrogate).
# lxml (via feedgen) raises ValueError "All strings must be XML compatible" on ANY of
# these, even inside CDATA, which turns one bad post into an HTTP 500 on the whole feed.
# TAB (\x09), LF (\x0A) and CR (\x0D) ARE XML-compatible and are deliberately kept.
# \x09 (TAB), \x0A (LF), \x0D (CR) are the only C0 chars XML 1.0 allows — keep them.
# lxml also rejects the two BMP noncharacters U+FFFE/U+FFFF and lone surrogates.
# (re interprets \x.. / \u.... escapes inside the pattern even in a raw string.)
_XML_INCOMPATIBLE_RE = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]')


# --------------------------------------------------------------------------- #
# Canonical treatment of a post with no date (issue #59)
# --------------------------------------------------------------------------- #
# A dateless post (service/deleted message, or a degraded #60 placeholder built from a
# dateless message) used to be handled THREE inconsistent ways:
#   1. _create_messages_groups group sort  -> float('inf')  => sorts NEWEST, survives [:limit]
#   2. _render_messages_groups final sort   -> 0.0          => sorts to the TAIL of the feed
#   3. generate_channel_rss pubDate         -> datetime.now(utc)
# So the feed ORDER (place 2, tail) and the pubDate (place 3, "now") contradicted each
# other, and place 3 was NON-DETERMINISTIC: the post's pubDate changed on EVERY poll.
# Miniflux sorts by date, so such a post kept floating to the top of the reader; and
# because the feed ETag is a content signature over the serialized body incl. <pubDate>
# (issue #62), a floating pubDate meant the ETag NEVER stabilized for any feed containing
# a dateless post \u2014 defeating conditional GET / 304 for that feed.
#
# Canonical rule: a dateless post gets ONE DETERMINISTIC fallback timestamp \u2014 the Unix
# epoch (0.0, UTC) \u2014 used IDENTICALLY in every sort key AND in the RSS pubDate, never
# now(). Same input -> same pubDate across every serialization, so the ETag is stable
# (fixing #62's dateless-feed limitation). Epoch is the honest "unknown date = oldest"
# interpretation: the post sorts to the tail consistently in both places, and can be
# trimmed by [:limit] like any other oldest post instead of masquerading as the newest.
# We deliberately do NOT synthesize a date from message_id: that would manufacture
# arbitrary 1970-plus-id timestamps, and message_id already keys the (stable) guid so
# multiple dateless posts never collide \u2014 determinism, not per-post ordering, is what #59
# requires.
MISSING_DATE_TS: float = 0.0


def _sort_ts(date_ts: float | None) -> float:
    """Timestamp sort key with the canonical dateless fallback (issue #59).

    `date_ts` is an already-computed POSIX timestamp (post['date']) or None. None -> the
    deterministic epoch fallback so a dateless post sorts to the tail (never as newest,
    never with a per-poll-varying key).
    """
    return date_ts if date_ts is not None else MISSING_DATE_TS


def _msg_sort_ts(message: Message) -> float:
    """Timestamp sort key for a raw Message, applying the same dateless fallback (#59).

    Naive-safe: a dated kurigram Message yields its (naive-local) timestamp; a dateless
    one yields the epoch fallback instead of an aware now(), so no naive/aware mix and no
    +inf 'newest' hack.
    """
    return message.date.timestamp() if message.date else MISSING_DATE_TS


# Process-wide count of posts whose render raised and were emitted as a degraded
# placeholder instead of being silently dropped (issue #60). Rendering runs inside a
# worker thread (asyncio.to_thread) and feeds can render concurrently, so guard the
# counter with a lock. Surfaced read-only via get_render_failed_count() (exposed on
# /health) so a silent render regression becomes observable.
_render_failed_lock = threading.Lock()
_render_failed_count = 0


def _record_render_failure():
    global _render_failed_count
    with _render_failed_lock:
        _render_failed_count += 1


def get_render_failed_count() -> int:
    """Total posts that fell back to a degraded placeholder since process start (#60)."""
    with _render_failed_lock:
        return _render_failed_count


# User-visible marker text for a post whose render raised. Kept as plain text (no markup
# beyond the wrapping <p>) so it survives both sanitize_html and _strip_xml_incompatible
# unchanged, and reads clearly in any RSS reader. English, matching create_error_feed.
_RENDER_FAILED_TITLE = "⚠ Post failed to render"
_RENDER_FAILED_NOTE = "This post could not be rendered. See the original on Telegram."


def _degraded_post(group: list[Message], error: Exception) -> dict:
    """Build a visible placeholder entry for a post group whose render raised (#60).

    A render exception must NOT make the post vanish silently from the feed: instead we
    emit a degraded entry carrying the SAME guid_basis the successful render would have
    used (window-independent album/min-id basis, #58), so the feed guid stays stable —
    once the render is fixed the reader updates that entry in place, not resurfaces it.

    All attribute access here is defensive: the group already failed to render, so we
    must not raise a second time while describing the failure.
    """
    # Pick the representative message with the SAME criterion the render path uses:
    # first message carrying text/caption, else the first of the group.
    try:
        main_idx = next((i for i, m in enumerate(group) if (m.text or m.caption)), 0)
    except Exception:
        main_idx = 0
    try:
        main = group[main_idx]
    except Exception:
        main = group[0]

    message_id = getattr(main, "id", None)
    msg_date = getattr(main, "date", None)
    try:
        date_ts = msg_date.timestamp() if msg_date else None
    except Exception:
        date_ts = None

    # Keep the SAME window-independent guid basis a successful render would use (#58),
    # so a degraded album entry does not diverge from its rendered self and get shown
    # twice. Defensive: the group already failed once, must not raise a second time.
    try:
        guid_basis = _stable_guid_basis(group)
    except Exception:
        guid_basis = ("msg", message_id)

    return {
        # sanitize_html (in _render_pipeline) keeps this plain <p> text as-is.
        'html': f"<p>{_RENDER_FAILED_NOTE}</p>",
        'date': date_ts,
        'message_id': message_id,
        'guid_basis': guid_basis,
        'title': _RENDER_FAILED_TITLE,
        'text': _RENDER_FAILED_NOTE,
        'author': None,
        'flags': ['render_failed'],
    }


def _strip_xml_incompatible(s):
    """Remove XML-incompatible control chars and lone surrogates from a string.

    Applied ONLY to the copy of post/channel text handed to the feedgen serializer,
    immediately before serialization — the stored/returned post text is untouched.
    Non-str input (e.g. an optional field that is None) is returned unchanged.
    """
    if not isinstance(s, str):
        return s
    return _XML_INCOMPATIBLE_RE.sub('', s)


def _stable_guid_basis(group: list[Message]) -> tuple[str, object]:
    """Return a window-independent identity for a rendered group's feed guid (issue #58).

    A media group's guid must NOT depend on which subset of its messages happens to fall
    inside the current fetch window: the reader dedups on the guid, so a guid that shifts
    when the window boundary moves re-surfaces the same album as a brand-new (duplicate)
    unread entry on every poll while the album is entering or leaving the window.

    The old basis — the message_id of the "first message carrying text/caption" — moves
    both when that message leaves the window AND, as an album enters the window newest
    member first, while the group composition itself grows ({102} -> {101,102} ->
    {100,101,102}). A minimum-message_id basis has the same growth sensitivity. Only the
    Telegram-assigned media_group_id is identical for every album member and independent
    of how many are currently in the window.

    Returns a (kind, key) pair, consumed by the RSS formatter which owns channel_username:
    - ('album', media_group_id): the group's messages all share one truthy, immutable
      Telegram media_group_id (a native album — including the transient case where only
      part of it is in the window). Fully window-independent.
    - ('msg', min_message_id): a single non-album message, or a TIME_BASED synthetic
      cluster of unrelated posts (mixed/absent media_group_id). message ids are immutable
      and min() is independent of text-presence/index/order; a synthetic cluster has no
      window-independent album id, so this is best-effort.
    """
    mgids = {getattr(m, "media_group_id", None) for m in group}
    if len(mgids) == 1:
        only = next(iter(mgids))
        if only:
            return ("album", only)
    return ("msg", min(m.id for m in group))


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
    # DEFAULT path (registry §3.12). None-date groups now use the CANONICAL dateless
    # fallback (epoch, MISSING_DATE_TS) here AND in the final _render_messages_groups sort
    # AND in the RSS pubDate — one deterministic treatment across all three (issue #59):
    # they sort to the tail and are trimmed like any other oldest post.
    processing_groups.sort(key=lambda group: _msg_sort_ts(group[0]), reverse=True)

    return processing_groups

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
                rendered_posts.append({
                    'html': _wrap_post_html(message_data["html"]["body"], message_data["html"]["footer"]),
                    'date': message_data['date'],
                    'message_id': message_data['message_id'],
                    'guid_basis': _stable_guid_basis(group),
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

                rendered_posts.append({
                    'html': _wrap_post_html(combined_html_body, footer_html),
                    'date': main_message['date'],
                    'message_id': main_message['message_id'],
                    'guid_basis': _stable_guid_basis(group),
                    'title': main_message['html']['title'],
                    'text': combined_text,
                    'author': main_message['author'],
                    'flags': merged_flags
                })
                
        except Exception as e:
            # Issue #60: a post whose render raises must NOT disappear silently from the
            # feed (indistinguishable from "nothing was posted"). Emit a degraded
            # placeholder entry (stable guid) instead of dropping it, and count the
            # failure so the regression is observable on /health. sanitize/serialize of
            # this trivial placeholder happens on the same paths as any other post.
            degraded = _degraded_post(group, e)
            _record_render_failure()
            logger.error(f"message_group_rendering_error: message_id {degraded['message_id']}, error {str(e)}, emitting degraded placeholder")
            rendered_posts.append(degraded)
            continue

    # Filter posts by exclude_flags
    if exclude_flags:
        exclude_flag_list = [flag.strip() for flag in exclude_flags.split(',')] # Split comma-separated flags into list
        rendered_posts = [
            post for post in rendered_posts
            # Keep a post unless "all" is requested and it carries any flag, or any of
            # its flags appears in the exclude list.
            if not ("all" in exclude_flag_list and post['flags'])
            and not any(flag in post['flags'] for flag in exclude_flag_list)
        ]
    
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
    
    # Sort by date; a None-date post uses the canonical epoch fallback (MISSING_DATE_TS),
    # identical to the group sort and the RSS pubDate, so feed order and pubDate agree and
    # a dateless post no longer floats (issue #59).
    rendered_posts.sort(key=lambda x: _sort_ts(x['date']), reverse=True)
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
        messages = sorted(messages, key=_msg_sort_ts)
        message_groups = _create_messages_groups(messages, group_ids)
    else:
        message_groups = _create_messages_groups(messages)
    # Trim groups if they exceed the requested limit (slice is a no-op when shorter).
    message_groups = message_groups[:limit]
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

        # Set feed metadata. channel_title comes from Telegram (channel_info.title), so
        # sanitize the strings that carry it before they reach feedgen/lxml.
        main_name = f"{channel_title} (@{channel_username})"
        fg.title(_strip_xml_incompatible(main_name))
        fg.link(href=f"https://t.me/{channel_username}", rel='alternate')
        fg.description(_strip_xml_incompatible(f'Telegram channel {channel_username} RSS feed'))
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
            # Every post-derived string is stripped of XML-incompatible control chars /
            # lone surrogates here, at the feedgen boundary, so a single bad byte in any
            # field cannot 500 the whole feed (issue #54). CDATA does NOT protect against
            # this — lxml raises in the CDATA branch too.
            fe.title(_strip_xml_incompatible(post['title']))

            # The human-facing <link> points at the group's representative message (may
            # shift harmlessly as an album enters/leaves the window — cosmetic only).
            post_link = f"https://t.me/{channel_username}/{post['message_id']}"
            fe.link(href=post_link)

            fe.description(_strip_xml_incompatible(post['text'].replace('\n', ' ')))
            # post['html'] is already sanitized per-post inside _render_pipeline
            # (single project-wide bleach config, fail-closed). No per-post thread
            # hop / CSSSanitizer here anymore.
            fe.content(content=_strip_xml_incompatible(post['html']), type='CDATA')

            if post['date'] is not None:
                pub_date = datetime.fromtimestamp(post['date'], tz=timezone.utc)
                logger.debug(f"rss_entry_date: channel {channel}, message_id {post['message_id']}, timestamp {post['date']}, pub_date {pub_date.isoformat()}")
                fe.pubDate(pub_date)
            else:
                # Date is None (e.g. service or deleted message). Use the CANONICAL
                # deterministic fallback (epoch, MISSING_DATE_TS) — the SAME value the sort
                # keys use — NOT datetime.now(). now() made this post's pubDate change on
                # every poll: it floated to the top in Miniflux and kept busting the feed
                # ETag (issue #62). A fixed epoch pubDate is stable across serializations,
                # matches the tail position the sort gives it, and keeps the ETag steady
                # for dateless feeds (issue #59).
                pub_date = datetime.fromtimestamp(MISSING_DATE_TS, tz=timezone.utc)
                logger.warning(f"rss_entry_missing_date: channel {channel}, message_id {post['message_id']}, using deterministic epoch fallback {pub_date.isoformat()}")
                fe.pubDate(pub_date)
            # Derive the guid from the group's window-independent identity (issue #58) so a
            # media group is not re-shown as a duplicate when the fetch window boundary
            # shifts. A native album keys on its immutable Telegram media_group_id (not a
            # real page, so isPermaLink="false"); everything else keys on the immutable
            # message id, keeping the t.me permalink form. Falls back to message_id for any
            # post dict lacking a basis (defensive).
            guid_kind, guid_key = post.get('guid_basis', ('msg', post['message_id']))
            if guid_kind == 'album':
                fe.guid(f"https://t.me/{channel_username}?mediagroup={guid_key}", permalink=False)
            else:
                fe.guid(f"https://t.me/{channel_username}/{guid_key}", permalink=True)
            
            if post['author'] and post['author'] != main_name:
                fe.author(name="", email=_strip_xml_incompatible(post['author']))
        
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

    # `channel` is a caller/URL-derived identifier that flows into text AND into URL
    # attributes (link/id/guid), and lxml rejects XML-incompatible chars in attributes
    # too — so sanitize it ONCE here and use the clean value everywhere below (issue #54).
    channel = _strip_xml_incompatible(channel)

    fg.title(_strip_xml_incompatible(f"Error: Channel @{channel} not found"))
    fg.link(href=f"https://t.me/{channel}", rel='alternate')
    fg.description(_strip_xml_incompatible(f'Error: Telegram channel @{channel} does not exist'))
    fg.language('en')
    fg.id(f"{base_url}/rss/{channel}")

    fe = fg.add_entry()
    fe.title("Channel not found")
    # Use the original channel string identifier passed to the function for links/text
    fe.link(href=f"https://t.me/{channel}")
    error_html = f"<p>The Telegram channel @{channel} does not exist or is not accessible.</p>"
    fe.description(_strip_xml_incompatible(f"{error_html}"))
    fe.content(content=_strip_xml_incompatible(error_html), type='CDATA')
    fe.pubDate(datetime.now(tz=timezone.utc))
    fe.guid(f"https://t.me/{channel}", permalink=True)

    rss_feed = fg.rss_str(pretty=True)
    if isinstance(rss_feed, bytes):
        return rss_feed.decode('utf-8')
    return rss_feed
