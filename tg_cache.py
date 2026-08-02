#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation
# pylance: disable=reportMissingImports, reportMissingModuleSource


import os
import json
import uuid
import logging
import random
import asyncio
import time
from types import SimpleNamespace
from typing import Any, Optional, Union, List
from pyrogram import Client
from pyrogram.types import Message
from tg_throttle import tg_rpc_bounded
from telegram_client import safe_get_rich_message
import rich_tree
from config import get_settings
from message_snapshot import (
    SNAPSHOT_VERSION,
    snapshot_messages,
    restore_messages,
)
from channel_key import canonical_channel_key

logger = logging.getLogger(__name__)

Config = get_settings()

# Path to cache directory
CACHE_DIR = os.path.join('data', 'tgcache')

# TTL (hours) for the channel-info cache. Channel title/username/id change rarely,
# so a long TTL removes the rate-limited GetFullChannel from the feed-poll hot path.
try:
    CHAT_CACHE_TTL_HOURS = int(os.getenv("TG_CHAT_CACHE_TTL_HOURS", "12"))
    if CHAT_CACHE_TTL_HOURS < 1:
        CHAT_CACHE_TTL_HOURS = 12
except ValueError:
    CHAT_CACHE_TTL_HOURS = 12


def _safe_key(key: Union[str, int]) -> str:
    """Sanitize a channel id/username into a filesystem-safe basename component."""
    return str(key).replace('/', '_').replace('\\', '_')


def _cache_file_path(key: Union[str, int], suffix: str) -> str:
    """Return the cache file path <safe_key>.<suffix> (e.g. 'history.json' / 'chatinfo.json').

    The key is first canonicalized so that 'Durov', 'durov' and '@durov' collapse to a
    single cache file (Telegram usernames are case-insensitive).
    """
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
        logger.info(f"cache_dir_created: path {CACHE_DIR}")
    return os.path.join(CACHE_DIR, f"{_safe_key(canonical_channel_key(key))}.{suffix}")


# --------------------------------------------------------------------------- #
# Generic JSON entry store.
# --------------------------------------------------------------------------- #
def _store_entry(path: str, payload: dict) -> None:
    """Atomically write {version, timestamp, jitter, **payload} as JSON to ``path``.

    The document is written to a unique '<path>.tmp.<uuid4>' and os.replace()d into place
    so a concurrent reader never observes a half-written file. The unique per-writer tmp
    name means two concurrent writers to the same path do not clobber each other's temp
    file. This writer's own tmp file is always removed in the finally block.
    """
    tmp_path = f"{path}.tmp.{uuid.uuid4().hex}"
    entry = {
        'version': SNAPSHOT_VERSION,
        'timestamp': time.time(),
        # Per-write TTL jitter (§17): decided ONCE at write time, so repeated reads of the
        # same file are stable (the reader never calls random()).
        'jitter': random.uniform(0.8, 1.0),
    }
    entry.update(payload)
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(entry, f)
        os.replace(tmp_path, path)
    finally:
        # Remove our own leftover tmp file if os.replace didn't consume it (e.g. it raised
        # or json.dump failed). Never touches another writer's uniquely-named tmp file.
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _load_entry(path: str, max_age_hours: float) -> Optional[dict]:
    """Return the stored payload dict, or None on missing / version mismatch / expired / bad JSON.

    TTL uses the jitter written into the entry (no random() at read time) so repeated
    reads near the boundary give a stable result.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            entry = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.warning(f"cache_entry_read_error: path {path}, error {str(e)}")
        return None

    if not isinstance(entry, dict) or entry.get('version') != SNAPSHOT_VERSION:
        logger.info(f"cache_entry_version_mismatch: path {path}")
        return None

    timestamp = entry.get('timestamp')
    if not isinstance(timestamp, (int, float)):
        return None
    age = time.time() - timestamp
    adjusted_max_age = max_age_hours * 3600 * entry.get('jitter', 1.0)
    if age > adjusted_max_age:
        logger.info(f"cache_entry_expired: path {path}, age {age:.1f}s > adjusted max {adjusted_max_age:.1f}s")
        return None
    return entry


# --------------------------------------------------------------------------- #
# History cache.
# --------------------------------------------------------------------------- #
def _save_history_to_cache(channel_id: Union[str, int], messages: List[Message], limit: int) -> None:
    """Save message history (as JSON snapshots) to cache. Stores the fetch limit, not len()."""
    try:
        cache_file = _cache_file_path(channel_id, 'history.json')
        payload = {'limit': limit, 'messages': snapshot_messages(messages)}
        _store_entry(cache_file, payload)
        logger.info(f"history_cache_saved: channel {channel_id}, limit {limit}, messages {len(messages)}, file {cache_file}")
    except Exception as e:
        logger.error(f"history_cache_save_error: channel {channel_id}, limit {limit}, error {str(e)}")


def _get_history_from_cache(channel_id: Union[str, int], limit: int, max_age_hours: int = 8) -> Optional[List[Message]]:
    """
    Retrieve message history from cache if fresh and the cached fetch covers ``limit``.

    Messages are stored newest-first. A cached entry fetched with an equal-or-larger limit
    serves a smaller request by slicing (prefix). A smaller cached fetch is a miss UNLESS
    the channel is exhausted (fewer messages exist than were asked for), in which case the
    cache already holds the entire recent history and can serve any larger request.
    """
    try:
        cache_file = _cache_file_path(channel_id, 'history.json')
        payload = _load_entry(cache_file, max_age_hours)
        if payload is None:
            logger.info(f"history_cache_miss: channel {channel_id}, limit {limit}")
            return None

        cached_limit = payload.get('limit', 0)
        raw_messages = payload['messages']
        # Serve when fetched with an equal-or-larger limit, OR when the channel is exhausted
        # (fewer messages exist than asked -> cache holds entire recent history).
        if cached_limit < limit and len(raw_messages) >= cached_limit:
            logger.info(f"history_cache_limit_short: channel {channel_id}, cached limit {cached_limit}, requested {limit}")
            return None

        messages = restore_messages(raw_messages[:limit])
        logger.info(f"history_cache_hit: channel {channel_id}, served {limit} of cached {cached_limit}, messages {len(messages)}")
        return messages
    except Exception as e:
        logger.error(f"history_cache_read_error: channel {channel_id}, limit {limit}, error {str(e)}")
        return None


async def _reply_enrichment(client: Client, messages: list[Message]) -> list[Message]:
    """
    Enrich messages with reply-to messages.

    Runs at FETCH time (from cached_get_chat_history, once per history-cache-miss) so
    the resolved reply targets get snapshotted into the history cache — feed renders
    (RSS and HTML) then read the quote from cache instead of re-fetching per poll.

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


# Total wall-clock budget (seconds) for part-message enrichment PER feed render (#83 / #86).
# The RPC gate is global and serialised, so unbounded per-render enrichment could starve every
# other feed poll; and the whole history snapshot must be written before the reader's ~20s HTTP
# timeout. 15s leaves ample margin. When the budget is spent the remaining part posts are still
# snapshotted with their partial tree + part plaque (fixed on the next re-fetch after TTL).
RICH_ENRICH_TIME_BUDGET = 15


def _needs_rich_enrichment(message: Message) -> bool:
    """True for a partial (part=True) rich message — INCLUDING a #84 parse_failed sentinel.

    A part=True post whose parse crashed is ALSO re-fetched: the fuller GetRichMessage vectors
    may parse cleanly. A part=False (complete) rich post and a non-rich post are skipped.
    """
    rm = getattr(message, 'rich_message', None)
    if rm is None:
        return False
    return bool(getattr(rm, 'part', False))


async def enrich_rich_parts(client: Client, messages: List[Message]) -> List[Message]:
    """Re-fetch the FULL content of part=True rich posts and splice it in before snapshot.

    Modelled on :func:`_reply_enrichment`: runs at FETCH time (cached_get_chat_history live
    path / PostParser.get_post) so the enriched tree lands in the history snapshot and every
    feed render reads the full rich content from cache instead of re-fetching per poll.

    Each partial post is re-fetched via ``safe_get_rich_message`` UNDER the global RPC gate
    (``tg_rpc_bounded``, like _reply_enrichment). On success ``message.rich_message`` is
    replaced with the full object and the memoised rich tree is INVALIDATED so the snapshot
    and render observe the enriched tree (else the stale partial tree persists).

    Breaker + budget — both STOP enrichment for the REST of this render but never abort it
    (the snapshot is ALWAYS written; un-enriched posts keep their partial tree + part plaque
    until the history TTL, then the next re-fetch fixes them):
      * the FIRST FloodWait (global throttle) OR the FIRST timeout (slow DC) trips the breaker;
      * exceeding ``RICH_ENRICH_TIME_BUDGET`` seconds stops enrichment.
    A one-off generic RPC error on a single post only skips THAT post and keeps going.
    """
    targets = [m for m in messages if _needs_rich_enrichment(m)]
    if not targets:
        return messages

    start = time.monotonic()
    enriched_count = 0
    for message in targets:
        if time.monotonic() - start > RICH_ENRICH_TIME_BUDGET:
            logger.warning(
                f"rich_enrich_budget_exhausted: stopped after {enriched_count} enriched "
                f"({RICH_ENRICH_TIME_BUDGET}s budget); remaining part posts keep the plaque"
            )
            break
        chat = getattr(message, 'chat', None)
        chat_id = getattr(chat, 'id', None)
        if chat_id is None:
            continue
        try:
            # Gate outside, timeout inside — the shared tg_rpc_bounded. safe_get_rich_message
            # bounds the RPC body itself (RICH_ENRICH_RPC_TIMEOUT) and never raises; a gate
            # timeout (if tg_rpc_timeout is shorter) surfaces here and is treated as a breaker.
            async with tg_rpc_bounded(Config["tg_rpc_timeout"]):
                result = await safe_get_rich_message(client, chat_id, message.id)
        except Exception as e:
            logger.warning(f"rich_enrich_gate_error: {type(e).__name__}: {e} — stopping enrichment")
            break

        if result.outcome == "ok" and result.rich_message is not None:
            message.rich_message = result.rich_message
            rich_tree.invalidate_tree_memo(message)
            enriched_count += 1
        elif result.outcome in ("floodwait", "timeout"):
            logger.warning(
                f"rich_enrich_breaker: {result.outcome} on {chat_id}/{message.id} — stopping "
                f"enrichment for this render (enriched {enriched_count} before the breaker)"
            )
            break
        # "error": skip this one post, keep enriching the rest.

    logger.debug(f"rich_enrich_done: enriched {enriched_count}/{len(targets)} part posts this render")
    return messages


async def cached_get_chat_history(client: Client, channel_id: Union[str, int], limit: int = 20) -> List[Message]:
    """
    Gets chat message history with caching.

    Args:
        client: Pyrogram client
        channel_id: Channel ID or username
        limit: Maximum number of messages to retrieve

    Returns:
        List of messages, same as original client.get_chat_history(). On a cache miss the
        live pyrogram Messages are returned; on a hit, restored CachedMessage objects.
    """
    cached_messages = await asyncio.to_thread(_get_history_from_cache, channel_id, limit)

    if cached_messages is not None:
        return cached_messages

    try:
        logger.info(f"history_cache_request: fetching fresh history for channel {channel_id}, limit {limit}")
        # Hold the global RPC gate for the live fetch and bound the RPC body with the
        # timeout — gate outside, timeout inside — via the shared tg_rpc_bounded (so the
        # tricky nesting is not re-derived here). The timeout covers the whole paginated
        # fetch; see the note in tg_rpc_bounded.
        async with tg_rpc_bounded(Config["tg_rpc_timeout"]):
            messages = [m async for m in client.get_chat_history(channel_id, limit=limit)]
        # Resolve reply targets ONCE at fetch time so they are persisted in the
        # history snapshot below — feed renders (RSS and HTML) then read the quote
        # from cache instead of re-fetching it on every poll.
        messages = await _reply_enrichment(client, messages)
        # Re-fetch the full content of any part=True rich posts before the snapshot, so the
        # enriched rich tree (not the partial one) is what gets cached (#86).
        messages = await enrich_rich_parts(client, messages)
        await asyncio.to_thread(_save_history_to_cache, channel_id, messages, limit)

        return messages
    except Exception as e:
        logger.error(f"history_cache_request_error: channel {channel_id}, limit {limit}, error {str(e)}")
        raise


# --------------------------------------------------------------------------- #
# Channel-info cache.
# --------------------------------------------------------------------------- #
def _save_chat_to_cache(channel_id: Union[str, int], data: dict) -> None:
    """Saves channel-info (id/title/username) to cache."""
    try:
        cache_file = _cache_file_path(channel_id, 'chatinfo.json')
        _store_entry(cache_file, {'data': data})
        logger.info(f"chatinfo_cache_saved: channel {channel_id}, file {cache_file}")
    except Exception as e:
        logger.error(f"chatinfo_cache_save_error: channel {channel_id}, error {str(e)}")


def _get_chat_from_cache(channel_id: Union[str, int], max_age_hours: int = CHAT_CACHE_TTL_HOURS) -> Optional[dict]:
    """Retrieves channel-info from cache if fresh (jittered TTL)."""
    try:
        cache_file = _cache_file_path(channel_id, 'chatinfo.json')
        payload = _load_entry(cache_file, max_age_hours)
        if payload is None:
            logger.info(f"chatinfo_cache_miss: channel {channel_id}")
            return None
        data = payload.get('data')
        if not isinstance(data, dict):
            logger.warning(f"chatinfo_cache_invalid: channel {channel_id}, unexpected payload type {type(data).__name__}")
            return None
        logger.info(f"chatinfo_cache_hit: channel {channel_id}")
        return data
    except Exception as e:
        logger.error(f"chatinfo_cache_read_error: channel {channel_id}, error {str(e)}")
        return None


async def cached_get_chat(client: Client, channel_id: Union[str, int]) -> SimpleNamespace:
    """Gets channel info (id/title/username) with disk TTL caching.

    Avoids the rate-limited channels.GetFullChannel on every feed poll. Mirrors
    cached_get_chat_history. On a cache miss the live get_chat is throttled and its
    exceptions (FloodWait, UsernameInvalid, ...) propagate to the caller unchanged;
    only successful lookups are cached. The returned object exposes .id/.title/.username.
    """
    cached = await asyncio.to_thread(_get_chat_from_cache, channel_id)
    if cached is not None:
        return SimpleNamespace(**cached)

    logger.info(f"chatinfo_cache_request: fetching fresh chat info for channel {channel_id}")
    async with tg_rpc_bounded(Config["tg_rpc_timeout"]):
        chat = await client.get_chat(channel_id)

    data = {
        'id': getattr(chat, 'id', None),
        'title': getattr(chat, 'title', None),
        'username': getattr(chat, 'username', None),
    }
    await asyncio.to_thread(_save_chat_to_cache, channel_id, data)
    return SimpleNamespace(**data)


# --------------------------------------------------------------------------- #
# Maintenance: legacy cleanup + age sweep.
# --------------------------------------------------------------------------- #
def cleanup_legacy_cache_files() -> int:
    """Delete legacy binary cache files (*.cache incl. *_history.cache, and *.chatinfo).

    The new store uses *.history.json / *.chatinfo.json, so these old-format files are
    dead weight and would otherwise never be reclaimed. Returns the number removed.
    """
    removed = 0
    if not os.path.isdir(CACHE_DIR):
        return 0
    try:
        names = os.listdir(CACHE_DIR)
    except OSError as e:
        logger.warning(f"cleanup_legacy_list_error: dir {CACHE_DIR}, error {str(e)}")
        return 0
    for name in names:
        if name.endswith('.cache') or name.endswith('.chatinfo'):
            try:
                os.remove(os.path.join(CACHE_DIR, name))
                removed += 1
            except OSError as e:
                logger.warning(f"cleanup_legacy_remove_error: file {name}, error {str(e)}")
    if removed:
        logger.info(f"cleanup_legacy_cache_files: removed {removed} legacy files from {CACHE_DIR}")
    return removed


def sweep_tgcache(max_age_days: int = 7) -> int:
    """Delete files in CACHE_DIR whose mtime is older than ``max_age_days``.

    Reclaims cache for dead channels and orphaned uuid tmp files. A race with an in-flight
    writer is POSSIBLE (stat -> unlink is not atomic against os.replace) but harmless: the
    worst outcome is one extra cache miss. This is NOT an atomicity guarantee. Returns the
    number of files removed.
    """
    removed = 0
    if not os.path.isdir(CACHE_DIR):
        return 0
    cutoff = time.time() - max_age_days * 86400
    try:
        names = os.listdir(CACHE_DIR)
    except OSError as e:
        logger.warning(f"sweep_tgcache_list_error: dir {CACHE_DIR}, error {str(e)}")
        return 0
    for name in names:
        path = os.path.join(CACHE_DIR, name)
        try:
            if not os.path.isfile(path):
                continue
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError as e:
            logger.warning(f"sweep_tgcache_remove_error: file {name}, error {str(e)}")
    if removed:
        logger.info(f"sweep_tgcache: removed {removed} stale files (> {max_age_days}d) from {CACHE_DIR}")
    return removed
