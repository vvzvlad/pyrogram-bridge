#!/usr/bin/env python3
# -*- coding: utf-8 -*-


# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation
# pylance: disable=reportMissingImports, reportMissingModuleSource

import logging
import os
import re
import uuid
import mimetypes
import hashlib
import hmac
from typing import List, Union, Any

import json
from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, format_datetime
import time
from contextlib import asynccontextmanager
import random
import asyncio
from concurrent.futures import ThreadPoolExecutor
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
from starlette.background import BackgroundTask
import sys

import magic
from pyrogram import errors
from pyrogram.types import Message
from pyrogram.enums import MessageMediaType
from fastapi import FastAPI, HTTPException, Response, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from telegram_client import TelegramClient
from config import get_settings, setup_logging
from rss_generator import generate_channel_rss, generate_channel_html, get_render_failed_count
from post_parser import PostParser
from url_signer import verify_media_digest
from file_io import (DB_PATH, init_db_sync, get_all_media_file_ids_sync,
                     update_media_file_access_sync, update_media_file_access_bulk_sync,
                     remove_media_file_ids_sync,
                     get_mime_type_sync, set_mime_type_sync)
from tg_cache import cleanup_legacy_cache_files, sweep_tgcache
from channel_key import canonical_channel_key
from migrate_channel_keys import migrate_channel_keys_sync

# Global python-magic instance for MIME type detection
magic_mime = magic.Magic(mime=True)

# Media cache on-disk layout. Resolved once at import so every producer/consumer of the
# cache agrees on the exact same absolute path format instead of re-deriving it inline.
MEDIA_CACHE_DIR = os.path.abspath(os.path.join("data", "cache"))  # resolved once at import


def media_cache_path(channel: str, post_id: int, file_unique_id: str | None = None) -> str:
    """Single source of truth for the on-disk layout: <root>/<channel>/<post_id>[/<fid>].

    Callers that need only the per-post directory omit ``file_unique_id``; passing it yields
    the full per-file path. ``channel`` is stringified by the caller where it may be an int.
    """
    path = os.path.join(MEDIA_CACHE_DIR, str(channel), str(post_id))
    if file_unique_id is not None:
        path = os.path.join(path, file_unique_id)
    return path


# MIME types are immutable per file_unique_id, so a process-lifetime dict in front of
# SQLite removes a to_thread + connect from every cache-hit response.
_mime_types: dict[tuple[str, int, str], str] = {}
_MIME_CACHE_MAX = 50_000  # crude bound; clear-all on overflow is fine at this size

# Content types safe to serve INLINE from our own origin. The media URL carries the feed's
# TOKEN, so serving active content (HTML/SVG/etc.) inline here would be stored XSS with
# access to that capability URL. We therefore NEVER echo a sniffed content type into the
# response: an allowlisted type is served inline; anything else is served as a neutralized
# attachment (see prepare_file_response). This set is intentionally tight — only the
# passive image/video/audio types this bridge actually produces from Telegram media
# (photos -> jpeg/png, stickers -> webp, video stickers/animations/video -> webm/mp4,
# voice/audio -> ogg/mpeg), plus the common container siblings a browser renders passively.
#
# DELIBERATELY EXCLUDED — do NOT add these to the inline set:
#   - image/svg+xml : an SVG is an image but executes embedded <script>/onload -> XSS.
#   - text/html, application/xhtml+xml, application/xml : active/script-capable documents.
#   - application/* : never safe to render inline from this origin.
#
# LIBMAGIC NAMING: the response type is chosen by matching the string that
# python-magic/libmagic ACTUALLY returns for each media file — which for several audio
# formats is an `x-`/vendor name, NOT the canonical IANA one. Verified empirically with
# file-5.46 on this box (`magic.Magic(mime=True).from_file(...)`):
#   M4A (ftyp brand "M4A ") -> audio/x-m4a      (NOT audio/mp4)
#   WAV (RIFF/WAVE)         -> audio/x-wav       (NOT audio/wav)
#   AAC (ADTS stream)       -> audio/x-hx-aac-adts (NOT audio/aac)
# We list BOTH the real libmagic outputs and the canonical siblings: different libmagic
# builds may emit either, so keeping both is harmless belt-and-suspenders. Without the
# x- names, legit voice/audio silently flips to attachment and won't play inline.
_INLINE_SAFE_CONTENT_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "video/mp4", "video/webm", "video/quicktime", "video/mpeg",
    "audio/mpeg", "audio/ogg", "audio/mp4", "audio/wav", "audio/aac", "audio/flac",
    # Actual libmagic outputs (see LIBMAGIC NAMING above):
    "audio/x-wav", "audio/x-m4a", "audio/x-hx-aac-adts", "audio/x-aac",
})

# Define custom exception for zero-size files
class ZeroSizeFileError(Exception):
    """Custom exception for zero-size files found or downloaded."""

# Raised when a live download can't get an HTTP_DOWNLOAD_SEMAPHORE permit within the
# admission window. It signals a saturated server (map to 503 + Retry-After), NOT a
# property of the file — so it must NOT arm the download backoff / negative cache.
class DownloadAdmissionTimeout(Exception):
    """Raised when acquiring a live-download permit times out (server saturated)."""

class RequestLoggingMiddleware:
    """Pure-ASGI request logger (no BaseHTTPMiddleware).

    BaseHTTPMiddleware runs the downstream app in a separate anyio task and pumps the
    response through an in-memory stream, which adds per-request overhead and interacts
    badly with streaming bodies, background tasks and client cancellation. This plain
    ASGI middleware only wraps `send` to observe the response status line, so it never
    buffers the body — the FileResponse stream flows straight through untouched.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Log only method and path (with query, matching the old request.url logging) at
        # debug level to avoid flooding logs on active RSS polling.
        _qs = scope.get("query_string") or b""
        _path = scope["path"] + (f"?{_qs.decode('latin-1')}" if _qs else "")
        logger.debug(f"Request: {scope['method']} {_path}")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                logger.debug(f"Response status: {message['status']}")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as e:
            logger.error(f"Request processing error: {str(e)}")
            raise

logger = logging.getLogger(__name__)
if not logger.handlers: pass  

client = TelegramClient()
Config = get_settings()
HTTP_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)  # semaphore for live HTTP media requests
BACKGROUND_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)  # semaphore for background cache worker
download_queue = asyncio.Queue(maxsize=100)
# Keys currently enqueued for (or being processed by) the background download worker.
# download_new_files re-scans the whole media table every sweep and, under a FloodWait,
# the worker drains slowly — without this guard the SAME not-yet-downloaded file gets
# re-enqueued on every pass, flooding the queue with duplicates. The event loop is
# single-threaded and every check/mutation below runs with no await in between, so a plain
# set is race-free: download_new_files adds the key before put_nowait; the worker discards
# it in its finally alongside task_done().
_queued_media: set[tuple[str, int, str]] = set()
def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """Parse an int env var for a module-level tunable; fall back to default on absence/garbage."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning(f"{name} must be an integer, got {raw!r}; using default {default}")
        return default

# --- Failing-download backoff (negative cache) --------------------------------
# A media file whose download keeps failing (hang/timeout/not-found) must not be
# retried on every cache sweep (cache_sweep_interval, default 900s), nor keep
# occupying a scarce Pyrogram transmission slot. We remember recent failures per
# (channel, post_id, file_unique_id) and skip / fast-reject re-attempts until an
# exponentially growing backoff elapses. Cleared on the first successful download.
# All access is from the single event-loop thread, so a plain dict needs no lock.
_DOWNLOAD_BACKOFF_BASE = 60.0     # seconds; backoff after the first failure
# Cap on the backoff (seconds), env MEDIA_BACKOFF_MAX_S, default 6h. It MUST be >= the
# cache sweep interval: with a shorter cap the backoff always expires before the next
# sweep, so download_new_files re-queues the dead file every pass (the retry-storm this
# fixes — a dead file was retried every ~15 min for up to 20 days). A long cap is safe
# because a VERIFIED self-heal restart clears the whole negative cache outright (see
# _clear_all_download_failures), so a healed system never holds a stale, long backoff
# that keeps fast-503'ing a now-downloadable file.
# Clamp STRICTLY above the sweep interval (+1), so a capped dead file's backoff still
# has time remaining at the next sweep structurally — not by timing luck — even if
# MEDIA_BACKOFF_MAX_S is mis-set at/below the sweep interval.
_DOWNLOAD_BACKOFF_MAX = float(max(
    _env_int("MEDIA_BACKOFF_MAX_S", 21600, minimum=1),
    int(Config["cache_sweep_interval"]) + 1,
))
# After this many CONSECUTIVE failed downloads, drop the media row from SQLite so a
# genuinely-dead file (deleted post, gone media) stops being re-queued forever. env
# MEDIA_FAILURES_DROP_ROW, default 15. Best-effort / fire-and-forget: if the post is
# still in a feed the renderer recreates the row, but the in-memory failure counter
# survives until success/restart, so a recreated-then-refailing row is dropped again
# quickly. See _drop_media_row.
_DOWNLOAD_FAILURES_DROP_ROW = _env_int("MEDIA_FAILURES_DROP_ROW", 15, minimum=1)
# LRU cap on the negative cache. Permanently-404 / deleted files would otherwise leave
# eternal entries and slowly leak memory on a long-uptime process. We keep at most this
# many most-recently-failed keys and evict the oldest. Eviction is harmless: a dropped
# key is simply treated as "never failed" again — at worst one extra retry attempt, which
# re-arms its backoff on failure. 10k keys is a tiny footprint yet far above any realistic
# concurrent-failure working set.
_DOWNLOAD_FAILURES_MAX = 10000
# key -> (consecutive_failures, retry_not_before_monotonic, kind). kind is 'permanent'
# (deleted post / genuinely-gone media -> get_media serves a clean 404 so readers stop
# retrying) or 'transient' (throttle/timeout/DC issue -> 503 + Retry-After). OrderedDict so
# we can evict in least-recently-updated order once the LRU cap is exceeded.
_download_failures: "OrderedDict[tuple[str, int, str], tuple[int, float, str]]" = OrderedDict()

def _download_backoff_remaining(key: tuple[str, int, str]) -> float:
    """Seconds until `key` may be retried; 0.0 if allowed now (or never failed)."""
    entry = _download_failures.get(key)
    if entry is None:
        return 0.0
    return max(0.0, entry[1] - time.monotonic())

def _download_failure_kind(key: tuple[str, int, str]) -> Union[str, None]:
    """Return the recorded failure kind ('permanent'|'transient') for `key`, or None if it
    has no recorded failure. get_media uses this on a backoff hit to serve a clean 404 for a
    permanent failure (stop retrying) vs a 503 + Retry-After for a transient one."""
    entry = _download_failures.get(key)
    return entry[2] if entry is not None else None

def _record_download_failure(key: tuple[str, int, str], kind: str = "transient") -> None:
    """Register a failed download and (re)arm an exponential backoff for `key`.

    `kind` classifies the failure: 'permanent' for a genuinely-gone file (a 404 from
    download_media_file), 'transient' for retryable faults (timeouts, RPC 5xx, zero-size).
    A later attempt that fails differently overwrites the kind, so the freshest failure wins.
    """
    fails = _download_failures.get(key, (0, 0.0, kind))[0] + 1
    backoff = min(_DOWNLOAD_BACKOFF_MAX, _DOWNLOAD_BACKOFF_BASE * (2 ** (fails - 1)))
    _download_failures[key] = (fails, time.monotonic() + backoff, kind)
    _download_failures.move_to_end(key)  # mark as most-recently-updated for LRU eviction
    # Bound memory (see _DOWNLOAD_FAILURES_MAX): evict the oldest entries beyond the cap.
    while len(_download_failures) > _DOWNLOAD_FAILURES_MAX:
        _download_failures.popitem(last=False)
    logger.warning(f"download_backoff_armed: {key[0]}/{key[1]}/{key[2]} failed {fails}x, next retry in {backoff:.0f}s")
    # Drop a persistently-dead row from SQLite so the sweeper stops re-queueing it forever.
    if fails >= _DOWNLOAD_FAILURES_DROP_ROW:
        _drop_media_row(key)

# Strong refs to fire-and-forget row-drop tasks so they are not GC'd mid-flight.
_drop_row_tasks: set[asyncio.Task] = set()

def _drop_media_row(key: tuple[str, int, str]) -> None:
    """Fire-and-forget removal of a persistently-failing media row from SQLite.

    _record_download_failure runs synchronously on the event loop; remove_media_file_ids_sync
    is a blocking sqlite call, so we schedule it via to_thread and do NOT await it (strict
    durability is unnecessary — the counter in _download_failures survives, so a re-added row
    that keeps failing is dropped again). Skipped when no loop is running (a sync unit test),
    where there is nothing to schedule onto."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    channel, post_id, file_unique_id = key

    async def _do():
        try:
            await asyncio.to_thread(
                remove_media_file_ids_sync, DB_PATH, [(channel, post_id, file_unique_id)]
            )
            logger.warning(
                f"download_row_dropped: {channel}/{post_id}/{file_unique_id} removed from SQLite "
                f"after >= {_DOWNLOAD_FAILURES_DROP_ROW} consecutive download failures"
            )
        except Exception as e:  # noqa: BLE001 — best-effort cleanup, never crash the caller
            logger.error(f"download_row_drop_failed: {channel}/{post_id}/{file_unique_id}: {e}")

    task = loop.create_task(_do())
    _drop_row_tasks.add(task)
    task.add_done_callback(_drop_row_tasks.discard)

def _clear_download_failure(key: tuple[str, int, str]) -> None:
    """Forget any recorded failure for `key` after a successful download."""
    if _download_failures.pop(key, None) is not None:
        logger.info(f"download_backoff_cleared: {key[0]}/{key[1]}/{key[2]} recovered")

def _clear_all_download_failures() -> None:
    """Drop the entire download negative cache. Registered with telegram_client and invoked
    ONLY after a VERIFIED self-heal restart (verify_get_me OK). _restart_client() rebuilds
    the WHOLE client — all DCs, including the media DC — so every per-file backoff is stale
    and a previously-failing file may now download. Without this a file that reached the
    backoff cap would keep fast-503'ing for up to _DOWNLOAD_BACKOFF_MAX after recovery, and
    nothing would retry it (the sweeper skips backed-off keys, get_media fast-503s them), so
    it could not self-heal until the backoff expired — defeating the self-heal's purpose.
    The retry-storm risk if the restart did not actually help is bounded: each re-download is
    timeout-bounded and simply re-arms its backoff."""
    n = len(_download_failures)
    _download_failures.clear()
    if n:
        logger.warning(f"download_backoff_cleared_all: dropped {n} entries after verified self-heal restart")
# How stale a temp_* file's mtime must be before a serve refreshes it (keeps the 1h
# sweeper from deleting an actively-viewed video). Well below 1h so the file stays alive,
# but large enough that the mtime — and thus FileResponse's ETag — is stable within any
# such window; a view running longer than one interval costs at most one safe 200
# If-Range restart per interval (a full re-fetch, never corruption).
TEMP_MTIME_REFRESH_INTERVAL = 300  # seconds

# In-flight download dedup registry: maps (channel, post_id, file_unique_id) to the
# shared Future of an ongoing download. The FIRST request for a key runs the download in
# a DETACHED task and shares its Future; concurrent requests await that Future instead of
# starting their own download. Detaching the download means a client disconnecting (which
# cancels its request coroutine) can never cancel the download nor leave the Future
# forever-pending — so waiters can't hang. See _download_deduped.
_inflight: dict[tuple[str, int, str], asyncio.Future] = {}
# Strong refs to detached download tasks so they are not garbage-collected mid-flight.
_inflight_tasks: set[asyncio.Task] = set()

async def _supervised(factory, name: str, min_restart_interval: float = 60.0):
    """Run factory() forever, restarting it if it dies with a non-cancellation error.

    A background loop that returns or raises (anything except CancelledError) is logged
    at CRITICAL and restarted, but successive (re)starts are spaced at least
    min_restart_interval seconds apart so a hard-failing task can't spin the event loop.
    CancelledError (shutdown) is propagated to the child and stops supervision.
    """
    while True:
        start = time.monotonic()
        task = asyncio.create_task(factory(), name=name)
        try:
            await task
            # A supervised background loop is not expected to return on its own.
            logger.critical(f"supervised_task_exited: {name} returned unexpectedly; restarting")
        except asyncio.CancelledError:
            # Shutdown or external cancel: propagate to the child, then stop supervising.
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise
        except Exception as e:
            logger.critical(f"supervised_task_crashed: {name} died with {e!r}; restarting", exc_info=True)
        # Rate-limit restarts: keep successive starts at least min_restart_interval apart.
        elapsed = time.monotonic() - start
        if elapsed < min_restart_interval:
            await asyncio.sleep(min_restart_interval - elapsed)


# Access-time write accumulator. A /media cache hit used to touch SQLite on the hot path
# (a threadpool hop + connect + UPDATE per request), which starves the threadpool under
# active RSS polling. Instead a cache hit just records the timestamp here — a dict write
# on the single-threaded event loop is cheap and atomic — and a periodic background task
# flushes the whole batch to SQLite in one executemany. Keys use str(channel) to stay
# consistent with the string form written at insert time and to not lean on SQLite's
# implicit column-affinity coercion (the channel column is TEXT, so a bound int would be
# coerced and still match — but we key the accumulator by the same type we store, rather
# than depend on that).
ACCESS_FLUSH_INTERVAL = 60  # seconds between access-time flushes
_access_updates: dict[tuple[str, int, str], float] = {}


async def _flush_access_updates() -> None:
    """Flush the accumulated access timestamps to SQLite in one bulk UPDATE.

    Snapshot-then-clear atomically on the loop: capture the current dict reference and
    replace the module global with a fresh empty dict in ONE synchronous step (before any
    await), so cache-hit writes arriving DURING the flush land in the new dict and are not
    lost. The bulk UPDATE runs off-loop via asyncio.to_thread. An empty batch is a no-op.
    """
    global _access_updates
    if not _access_updates:
        return
    pending = _access_updates
    _access_updates = {}
    entries = [(channel, post_id, file_unique_id, added)
               for (channel, post_id, file_unique_id), added in pending.items()]
    try:
        await asyncio.to_thread(update_media_file_access_bulk_sync, DB_PATH, entries)
    except Exception:
        # Bulk write failed: re-queue this batch so the access-times are not lost (a lost
        # timestamp would eventually evict a still-used file from the 20-day cache). Use
        # setdefault so any FRESHER write accumulated during the flush is never overwritten
        # by our stale snapshot. Runs on the loop with no await before the mutation, so this
        # is race-free. Re-raise so the flush loop logs it.
        for key, added in pending.items():
            _access_updates.setdefault(key, added)
        raise


async def _access_flush_loop() -> None:
    """Periodically flush the access-time accumulator (runs under _supervised)."""
    while True:
        await asyncio.sleep(ACCESS_FLUSH_INTERVAL)
        try:
            await _flush_access_updates()
        except Exception as e:
            # Log and keep looping: a transient SQLite error must not drop the batch's
            # successors. (_supervised still restarts us if this ever raises out.)
            logger.error(f"access_flush_error: {e}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging(Config["log_level"])

    # Enlarge the default threadpool: SQLite/python-magic/pickle/os.walk all run via
    # asyncio.to_thread, and the interpreter default (min(32, cpu+4) = 5-6 on a 1-2 CPU
    # container) is too small under load. Configurable via IO_THREAD_POOL_SIZE.
    loop = asyncio.get_running_loop()
    io_executor = ThreadPoolExecutor(max_workers=Config["io_thread_pool_size"], thread_name_prefix="io")
    loop.set_default_executor(io_executor)

    os.makedirs(MEDIA_CACHE_DIR, exist_ok=True) # Create cache directory

    # Initialize SQLite database (creates table if not present)
    await asyncio.to_thread(init_db_sync, DB_PATH)

    # One-shot migration of existing cache dirs + DB rows to the canonical channel key
    # (case-insensitive username collapse). Runs AFTER init_db_sync and BEFORE client.start()
    # and the background tasks. Wrapped in to_thread because it is a blocking FS-rename +
    # SQLite routine that would otherwise stall the event loop. Idempotent: a re-run is a no-op.
    await asyncio.to_thread(migrate_channel_keys_sync, DB_PATH, MEDIA_CACHE_DIR)

    # One-shot startup maintenance of the history/chatinfo cache: drop legacy pickle files
    # (which are now always a miss), age-sweep stale tgcache entries, and remove the
    # pre-SQLite data/media_file_ids.json legacy dump (no longer read by any code).
    await asyncio.to_thread(cleanup_legacy_cache_files)
    await asyncio.to_thread(sweep_tgcache)
    legacy_media_ids = os.path.join("data", "media_file_ids.json")
    if os.path.exists(legacy_media_ids):
        try:
            await asyncio.to_thread(os.remove, legacy_media_ids)
            logger.info(f"legacy_media_file_ids_removed: {legacy_media_ids}")
        except OSError as e:
            logger.warning(f"legacy_media_file_ids_remove_error: {e}")

    # Wire the self-heal restart -> negative-cache clear hook. telegram_client owns the
    # restart; the negative cache lives here. Registering a plain callback (rather than
    # importing api_server from telegram_client) keeps the dependency one-way and avoids a
    # circular import. The hook fires ONLY on a verified restart — see _restart_client.
    client.set_restart_callback(_clear_all_download_failures)

    await client.start()
    # Supervise the background tasks: if either dies (not via cancellation) it is logged
    # CRITICAL and restarted, so a crash can no longer silently stop cache sweeping or downloads.
    background_task = asyncio.create_task(_supervised(cache_media_files, "cache_media_files"))
    worker_task = asyncio.create_task(_supervised(background_download_worker, "background_download_worker"))
    access_flush_task = asyncio.create_task(_supervised(_access_flush_loop, "access_flush_loop"))
    yield
    background_task.cancel() # Cleanup
    worker_task.cancel()
    access_flush_task.cancel()
    try:
        await background_task
    except asyncio.CancelledError:
        pass
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    try:
        await access_flush_task
    except asyncio.CancelledError:
        pass
    # Final flush AFTER the loop task is cancelled (no race with a loop-driven flush) and
    # BEFORE the threadpool is shut down (to_thread still has its executor), so the last
    # <=ACCESS_FLUSH_INTERVAL seconds of access-times are persisted on shutdown.
    try:
        await _flush_access_updates()
    except Exception as e:
        logger.error(f"access_flush_shutdown_error: {e}")
    await client.stop()
    # Shut the io threadpool down so its threads don't linger past a reload/restart.
    io_executor.shutdown(wait=False)

app = FastAPI(title="Pyrogram Bridge", lifespan=lifespan)
app.add_middleware(RequestLoggingMiddleware)

def mask_sensitive_value(input_str: str) -> str:
    """Mask middle part of sensitive value, showing only first and last 4 characters"""
    if not input_str or len(input_str) < 8:
        return '***'
    visible_chars = 4
    return f"{input_str[:visible_chars]}{'*' * (len(input_str) - visible_chars * 2)}{input_str[-visible_chars:]}"


if __name__ == "__main__":
    import uvicorn
    
    setup_logging(Config["log_level"])
    
    logger.info("Starting server with configuration:")
    for key, value in Config.items():
        if any(sensitive in key.lower() for sensitive in ['token', 'tg_api_id', 'tg_api_hash']):
            logger.info(f"    {key}: {mask_sensitive_value(str(value))}")
        else:
            logger.info(f"    {key}: {value}")
    
    # Log uvloop status
    logger.info("    uvloop: enabled (asyncio speedup active)")
    
    try:
        uvicorn.run(
            "api_server:app",
            host=Config["api_host"],
            port=Config["api_port"],
            loop="uvloop"
        )
    except OSError as e:
        if "[Errno 98] Address already in use" in str(e):
            logger.critical(f"Port {Config['api_port']} is already in use. Exiting with code 1 to trigger Docker restart.")
            sys.exit(1)
        else:
            logger.critical(f"Failed to start server: {str(e)}")
            sys.exit(1)

async def find_file_id_in_message(message: Message, file_unique_id: str) -> Union[str, None]:
    """Find file_id by checking all possible media types in message"""
    if message.media == MessageMediaType.POLL:
        # Kurigram 2.2.23: polls may carry media in description_media and
        # explanation_media (MessageContent objects). This branch is now LIVE:
        # download_media_file no longer short-circuits polls, so a signed /media URL for a
        # poll's description_media resolves its fid here and downloads normally. The bridge
        # renders only description_media, but explanation_media is searched too: if a signed
        # URL for it ever exists, the download must still work. getattr-only access —
        # older Poll objects/mocks do not define these fields.
        poll = getattr(message, 'poll', None)
        for container_name in ('description_media', 'explanation_media'):
            content = getattr(poll, container_name, None) if poll else None
            if content is None:
                continue
            for media_attr in ('photo', 'video', 'animation', 'sticker',
                               'document', 'audio', 'voice', 'video_note'):
                media_obj = getattr(content, media_attr, None)
                if media_obj is None:
                    continue
                if getattr(media_obj, 'file_unique_id', None) == file_unique_id:
                    return getattr(media_obj, 'file_id', None)
        logger.debug(f"Message {message.id} is a poll, media '{file_unique_id}' not found in poll content")
        return None

    media_found = []
    if message.photo:
        media_found.append(f"photo ({message.photo.file_unique_id})")
        if message.photo.file_unique_id == file_unique_id:
            return message.photo.file_id
    if message.video:
        media_found.append(f"video ({message.video.file_unique_id})")
        if message.video.file_unique_id == file_unique_id:
            return message.video.file_id
    if message.animation:
        media_found.append(f"animation ({message.animation.file_unique_id})")
        if message.animation.file_unique_id == file_unique_id:
            return message.animation.file_id
    if message.video_note:
        media_found.append(f"video_note ({message.video_note.file_unique_id})")
        if message.video_note.file_unique_id == file_unique_id:
            return message.video_note.file_id
    if message.audio:
        media_found.append(f"audio ({message.audio.file_unique_id})")
        if message.audio.file_unique_id == file_unique_id:
            return message.audio.file_id
    if message.voice:
        media_found.append(f"voice ({message.voice.file_unique_id})")
        if message.voice.file_unique_id == file_unique_id:
            return message.voice.file_id
    if message.sticker:
        media_found.append(f"sticker ({message.sticker.file_unique_id})")
        if message.sticker.file_unique_id == file_unique_id:
            return message.sticker.file_id
    if message.web_page and message.web_page.photo:
        media_found.append(f"web_page.photo ({message.web_page.photo.file_unique_id})")
        if message.web_page.photo.file_unique_id == file_unique_id:
            return message.web_page.photo.file_id
    if message.document:
        media_found.append(f"document ({message.document.file_unique_id})")
        if message.document.file_unique_id == file_unique_id:
            return message.document.file_id
    # New media types (Kurigram 2.2.23): getattr-only access, the attributes do not
    # exist on older Message objects/mocks.
    if live_photo := getattr(message, 'live_photo', None):
        media_found.append(f"live_photo ({getattr(live_photo, 'file_unique_id', None)})")
        if getattr(live_photo, 'file_unique_id', None) == file_unique_id:
            return getattr(live_photo, 'file_id', None)
    if story := getattr(message, 'story', None):
        for story_attr in ('photo', 'video'):
            story_media = getattr(story, story_attr, None)
            if story_media is None:
                continue
            media_found.append(f"story.{story_attr} ({getattr(story_media, 'file_unique_id', None)})")
            if getattr(story_media, 'file_unique_id', None) == file_unique_id:
                return getattr(story_media, 'file_id', None)

    # If we reached here, the file_unique_id was not found
    channel_id_log = message.chat.id if message.chat else 'unknown_chat'
    logger.warning(f"Could not find media with file_unique_id '{file_unique_id}' in message {message.id} (channel: {channel_id_log}). Found media: {', '.join(media_found) or 'None'}")
    return None


async def delayed_delete_file(file_path: str, delay: int = 300) -> None:
    """Delete a temporary file after a delay. Runs as an async background task."""
    await asyncio.sleep(delay)
    try:
        os.remove(file_path)
        logger.info(f"Deleted temporary file {file_path} after delay of {delay} seconds")
    except Exception as e:
        logger.error(f"Failed to delete temporary file {file_path}: {str(e)}")


async def prepare_file_response(file_path: str, request: Request, delete_after: bool = False,
                                media_key: tuple[str, int, str] | None = None) -> Response:
    """Serve a cached media file via Starlette's FileResponse.

    FileResponse handles Range/If-Range/206/416/multipart and sets
    Accept-Ranges/ETag/Last-Modified itself, and reads the file efficiently (no per-64KB
    to_thread hop that starved the threadpool). We keep: the early 404 pre-check, the MIME
    logic (python-magic + SQLite type cache), the stage-2 temp_* mtime touch, and the
    delete_after BackgroundTask.
    """
    # `request` is unused now that FileResponse parses the Range header itself, but the
    # signature is kept for call-site compatibility (and future needs).

    # Keep an actively-viewed large-video temp file alive: refresh its mtime so the 1h
    # sweeper (which deletes temp_* by mtime) won't remove it out from under a viewer.
    # DEBOUNCED: FileResponse derives ETag/Last-Modified from mtime, so touching on EVERY
    # serve would change the validators per request. We only refresh when the mtime is
    # already older than TEMP_MTIME_REFRESH_INTERVAL — far below the 1h sweeper window, so
    # the file stays alive, and the ETag is stable within any such window (a view running
    # longer than one interval costs at most one safe 200 If-Range restart per interval).
    if os.path.basename(file_path).startswith("temp_"):
        try:
            age = time.time() - await asyncio.to_thread(os.path.getmtime, file_path)
            if age > TEMP_MTIME_REFRESH_INTERVAL:
                await asyncio.to_thread(os.utime, file_path, None)
        except OSError as e:
            logger.debug(f"Failed to refresh mtime for {file_path}: {e}")

    # Take ONE authoritative stat and hand it to FileResponse as stat_result. This both
    # (a) preserves the 404 semantics: FileResponse with stat_result=None re-stats at
    # send-time and raises a RuntimeError (-> 500, escaping this handler's try/except) if
    # the file was swept between here and the send; and (b) makes the ETag/Last-Modified
    # reflect exactly the mtime observed after the optional touch above. The remaining
    # narrow window (deleted between this stat and FileResponse's own open) truncates the
    # body — that pre-existed the FileResponse migration and is not handled here.
    try:
        stat_result = await asyncio.to_thread(os.stat, file_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")

    media_type: str | None = None

    if media_key is not None:
        # Fast path: the in-memory MIME cache. MIME is immutable per file_unique_id, so a
        # hit here serves the response with no to_thread hop and no SQLite connection at all.
        channel_key, post_id_key, file_unique_id_key = media_key
        media_type = _mime_types.get(media_key)
        if not media_type:
            # Dict miss — consult the SQLite type cache (still avoids python-magic I/O).
            media_type = await asyncio.to_thread(get_mime_type_sync, DB_PATH, channel_key, post_id_key, file_unique_id_key)
            if media_type:
                # Populate the dict so the next request skips the to_thread + connect.
                if len(_mime_types) >= _MIME_CACHE_MAX:
                    _mime_types.clear()
                _mime_types[media_key] = media_type

    if not media_type:
        # Cache miss or no media_key — detect with python-magic in a thread to avoid blocking the event loop
        try:
            media_type = await asyncio.to_thread(magic_mime.from_file, file_path)
        except Exception as e:
            logger.warning(f"Failed to determine MIME type using python-magic: {str(e)}")
            media_type = None

        # Persist the detected MIME type so the next request can skip python-magic — into
        # BOTH the SQLite type cache and the in-memory dict.
        if media_type and media_key is not None:
            await asyncio.to_thread(set_mime_type_sync, DB_PATH, channel_key, post_id_key, file_unique_id_key, media_type)
            if len(_mime_types) >= _MIME_CACHE_MAX:
                _mime_types.clear()
            _mime_types[media_key] = media_type

    if not media_type: media_type, _ = mimetypes.guess_type(file_path)  # Fallback to mimetypes if python-magic failed
    if not media_type: media_type = "application/octet-stream"  # Final fallback to octet-stream

    logger.debug(f"Determined media type for {os.path.basename(file_path)}: {media_type}")

    # Delete the temporary file once the response has been fully sent (stage-2 delete_after).
    # FileResponse runs this BackgroundTask after streaming the body.
    background = BackgroundTask(delayed_delete_file, file_path) if delete_after else None

    # SECURITY: decide the RESPONSE content type from an allowlist, NOT from the sniffed
    # `media_type` above (which stays as the value persisted to the MIME cache). If magic
    # sniffed attacker-influenced bytes as text/html or image/svg+xml, echoing that type
    # inline from our own origin — the origin whose URL carries the feed TOKEN — is stored
    # XSS. So: an allowlisted passive type is served inline with that exact type; anything
    # else (text/html, image/svg+xml, application/*, anything script-capable) is served as
    # a neutralized download instead of being refused, since media must stay retrievable.
    if media_type in _INLINE_SAFE_CONTENT_TYPES:
        response_media_type = media_type
        disposition = "inline"
    else:
        response_media_type = "application/octet-stream"
        disposition = "attachment"

    # FileResponse handles Range/If-Range/206/416/multipart and sets
    # Accept-Ranges/ETag/Last-Modified itself (from the stat_result we pass). Do NOT
    # hand-build Content-Disposition: FileResponse forms it from filename= —
    # `<disposition>; filename="x"` for an ASCII name, adding `filename*=UTF-8''x` only for
    # a non-ASCII name. It uses setdefault, so a manual header would OVERRIDE it, not double
    # it; letting FileResponse own it keeps the RFC 5987 encoding correct.
    #
    # The extra headers below apply to the 200 AND the 206 (single- and multi-range) paths:
    # Starlette 0.45.3 FileResponse emits `self.raw_headers` (which includes everything from
    # this headers= dict) on both the 200 start and every 206 start, only overriding
    # content-range/content-length. (The 400 malformed-range and 416 unsatisfiable paths
    # build a fresh PlainTextResponse WITHOUT these headers, but neither serves a body, so
    # there is no sniffable content to protect there.)
    #   - X-Content-Type-Options: nosniff makes our declared content type authoritative so
    #     the browser won't re-sniff an octet-stream/image body as HTML.
    #   - Content-Security-Policy sandboxes the response as defense-in-depth.
    #
    # Files are addressed by file_unique_id which is immutable in Telegram, so it is safe
    # to cache them aggressively on the client side.
    return FileResponse(
        file_path,
        media_type=response_media_type,
        filename=os.path.basename(file_path),
        content_disposition_type=disposition,
        stat_result=stat_result,
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
        background=background,
    )

def _media_download_timeout(file_size: int) -> float:
    """Scale the download timeout with file size.

    Effective floor of `media_download_min_speed` bytes/s (timeout ≈ size / min_speed),
    clamped to [media_download_timeout_min, media_download_timeout_max] seconds.
    A non-positive/unknown size falls back to the minimum timeout.
    """
    min_t = Config["media_download_timeout_min"]
    max_t = Config["media_download_timeout_max"]
    min_speed = Config["media_download_min_speed"]
    if file_size <= 0 or min_speed <= 0:
        return float(min_t)
    return float(min(max_t, max(min_t, file_size // min_speed)))


async def _download_atomic(file_id: str, final_path: str, timeout: float) -> str:
    """Download a media file through a unique partial path and atomically publish it.

    Invariant enforced here: a file that exists at a FINAL name (`{file_unique_id}` or
    `temp_{file_unique_id}`) is ALWAYS complete. The downloader only ever writes to a
    unique `{final_path}.part.{hex}` path; the file appears at its final name solely via
    os.rename (atomic on POSIX). The finally block ALWAYS removes our partial (on timeout,
    cancel, zero-size, or losing a rename race), so no stub is ever served or left behind.

    Raises ZeroSizeFileError if the download produced a missing/zero-size file.
    """
    part_path = f"{final_path}.part.{uuid.uuid4().hex}"
    # Create the cache dir here, immediately before writing the partial — NOT up-front in
    # download_media_file / download_new_files. That way an item that never actually
    # downloads (permanently-dead file) never leaves an empty dir behind for the sweeper's
    # os.walk to trip over, and this self-heals a race with the sweeper's empty-dir rmdir
    # (which could run between an earlier makedirs and this open).
    os.makedirs(os.path.dirname(part_path), exist_ok=True)
    try:
        await client.safe_download_media(file_id, part_path, timeout=timeout)
        if not os.path.exists(part_path) or os.path.getsize(part_path) == 0:
            raise ZeroSizeFileError(
                f"Downloaded file for {final_path} is zero size or missing after download attempt."
            )
        # Publish atomically, but only if nobody else already produced the final file
        # (a concurrent request that won the race). rename is atomic on POSIX.
        if not os.path.exists(final_path):
            os.rename(part_path, final_path)
        return final_path
    finally:
        # Always clean up our partial: timeout, cancellation, zero-size, or race loser
        # (rename skipped because final_path already existed).
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError as e:
                logger.warning(f"cleanup_error: Failed to remove partial file {part_path}: {e}")


async def _download_deduped(channel: Union[str, int], post_id: int, file_unique_id: str,
                            semaphore: asyncio.Semaphore) -> tuple[Union[str, None], bool]:
    """Deduplicate concurrent downloads of the same media by (channel, post_id, fid).

    The first request for a key runs download_media_file in a DETACHED task and shares its
    Future; concurrent requests await the same Future (bounded by wait_for). The detached
    task sets the Future's result/exception (on success/failure) and its finally ALWAYS
    pops the key — so both happen before the task ends: a completed Future never leaves the
    key stuck, and a client disconnect (cancelling only the awaiting request coroutine) can
    neither cancel the download nor hang other waiters.

    The live-download permit (`semaphore`) is acquired INSIDE the detached runner, so ONE
    live download holds exactly ONE permit regardless of how many requests wait on the
    shared Future — N concurrent requests for the same file no longer burn N permits. The
    acquire is bounded (30s): on saturation the runner resolves the Future with a
    DownloadAdmissionTimeout (server-busy, mapped to 503 by the caller) instead of hanging.
    An admission timeout is NOT a download failure, so it must NOT arm the backoff.
    """
    key = (str(channel), post_id, file_unique_id)
    fut = _inflight.get(key)
    if fut is None:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _inflight[key] = fut

        async def _runner():
            try:
                # Acquire a live-download permit for the duration of this ONE download.
                # Bounded so a saturated semaphore fast-rejects rather than hangs; a
                # timed-out acquire never held a permit (nothing to release) and is a
                # server-load signal, not a file failure, so backoff is NOT armed.
                _sem_wait_start = time.monotonic()
                try:
                    await asyncio.wait_for(semaphore.acquire(), timeout=30)
                except asyncio.TimeoutError:
                    logger.warning(f"http_semaphore_timeout: {key[0]}/{key[1]}/{key[2]} waited >30s for a download permit")
                    if not fut.done():
                        fut.set_exception(DownloadAdmissionTimeout("download admission timed out"))
                    return
                _sem_wait = time.monotonic() - _sem_wait_start
                if _sem_wait > 0.5:
                    logger.warning(f"diag_semaphore_wait: {key[0]}/{key[1]}/{key[2]} waited {_sem_wait:.3f}s for a download permit")
                # Release the permit around the ACTUAL download only (success or failure).
                try:
                    _dl_start = time.monotonic()
                    result = await download_media_file(channel, post_id, file_unique_id)
                    _dl_elapsed = time.monotonic() - _dl_start
                    logger.info(f"diag_download_timing: {key[0]}/{key[1]}/{key[2]} download_media_file took {_dl_elapsed:.3f}s (semaphore_wait={_sem_wait:.3f}s)")
                    _clear_download_failure(key)
                    if not fut.done():
                        fut.set_result(result)
                finally:
                    semaphore.release()
            except BaseException as e:  # noqa: BLE001 — must forward ANY failure to waiters
                # Arm backoff for real download failures only, never for a shutdown-time
                # cancel (BaseException that is not an Exception).
                # (DownloadAdmissionTimeout returns early above and never reaches here.)
                # A FloodWait is a GLOBAL Telegram throttle, not a per-file fault (parity
                # with background_download_worker), so it must NOT poison this file's negative
                # cache — waiters still receive it and get_media maps it to a retryable 429.
                # Classify the rest: 'permanent' (genuinely gone -> get_media serves a clean 404
                # and readers stop retrying) for an HTTPException(404) OR a raw RPCError with
                # CODE==400 (MsgIdInvalid/ChannelInvalid/PeerIdInvalid — the resource is gone;
                # get_media ALSO maps RPC-400 to 404, so caching it transient would flap 404<->503).
                # Everything else (504 timeout, zero-size, RPC 5xx, other Exception) is
                # 'transient' -> 503 + Retry-After. FloodWait is excluded above (never armed).
                if isinstance(e, Exception) and not isinstance(e, errors.FloodWait):
                    permanent = (isinstance(e, HTTPException) and e.status_code == 404) or (
                        isinstance(e, errors.RPCError) and getattr(e, "CODE", None) == 400
                    )
                    kind = "permanent" if permanent else "transient"
                    _record_download_failure(key, kind)
                if not fut.done():
                    fut.set_exception(e)
            finally:
                _inflight.pop(key, None)

        task = asyncio.create_task(_runner())
        _inflight_tasks.add(task)
        task.add_done_callback(_inflight_tasks.discard)
        # Retrieve the exception in a done-callback to silence "exception was never
        # retrieved" if every waiter disconnects before awaiting the Future.
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())

    # Waiter timeout: a generous safety net above the maximum per-download timeout (the
    # download is itself internally bounded), so a live download always completes first.
    waiter_timeout = float(Config["media_download_timeout_max"]) + 120.0
    # shield so a timed-out / cancelled waiter does NOT cancel the shared Future that the
    # detached task owns and other waiters depend on.
    return await asyncio.wait_for(asyncio.shield(fut), timeout=waiter_timeout)


async def download_media_file(channel: Union[str, int], post_id: int, file_unique_id: str) -> tuple[Union[str, None], bool]:
    """
    Download media file from Telegram and save to cache
    Returns tuple of (file path, delete_after)
    """
    import time as _time
    _fn_start = _time.monotonic()
    # Nested cache path for this post. The directory is created lazily inside _download_atomic
    # (only when a partial is actually written), so a file that never downloads leaves no
    # empty dir behind. Bare os.path.exists checks below are safe on a not-yet-created dir.
    post_dir = media_cache_path(str(channel), post_id)

    # Convert numeric channel ID to int if needed
    channel_id: Union[str, int] = channel
    if isinstance(channel, str) and channel.startswith('-100'):
        channel_id = int(channel)
    
    try:
        message = await client.safe_get_messages(channel_id, post_id)
    except asyncio.TimeoutError:
        logger.error(f"Timeout getting messages for {channel}/{post_id}")
        raise HTTPException(status_code=504, detail="Request timeout")

    # Guard: message may be None (deleted) or an empty stub returned by Pyrogram
    if not message or getattr(message, 'empty', False):
        logger.warning(f"Message {post_id} not found or empty in channel {channel}")
        # The post is gone: drop its media row so the cache sweep stops re-fetching a
        # deleted post forever (mirrors the fid-not-found branch below). Canonical key form
        # (str(channel), post_id, file_unique_id).
        try:
            await asyncio.to_thread(
                remove_media_file_ids_sync,
                DB_PATH, [(str(channel), post_id, file_unique_id)]
            )
            logger.info(f"Removed entry for deleted/empty post {channel}/{post_id}/{file_unique_id} from SQLite")
        except Exception as e:
            logger.error(f"Failed to remove entry for {channel}/{post_id}/{file_unique_id} from SQLite: {str(e)}")
        raise HTTPException(status_code=404, detail="Post not found or deleted")

    # NOTE: POLL messages are NOT short-circuited here. A poll may carry media in its
    # description_media/explanation_media; find_file_id_in_message resolves that fid below
    # and the file downloads through the normal cache path. A poll whose requested media is
    # absent falls into the standard "fid not found" branch (404 + SQLite row removal), so a
    # stale/rendered-but-missing poll media entry stops being re-queued every cache sweep.
    # is_large_video only inspects message.video (None for a poll), so it stays False here.

    # Check if it is a video and if its size exceeds 100 MB
    is_large_video = False
    if message.video:
        try:
            if message.video.file_size > 100 * 1024 * 1024:
                is_large_video = True
        except Exception as e:
            logger.error(f"Failed to get video file size for message {post_id}: {str(e)}")
    
    if is_large_video:
        # For large video, download without permanent caching; use a temporary file.
        temp_file_path = os.path.join(post_dir, f"temp_{file_unique_id}")
        # A temp_{fid} file now only ever appears via atomic rename (see _download_atomic),
        # so its presence GUARANTEES a complete file — serve it directly.
        if os.path.exists(temp_file_path) and os.path.getsize(temp_file_path) > 0:
            logger.info(f"Temporary file {temp_file_path} already exists, serving cached large video")
            return temp_file_path, False
        file_id = await find_file_id_in_message(message, file_unique_id)
        if not file_id:
            logger.error(f"Media with file_unique_id {file_unique_id} not found in message {post_id}")
            raise HTTPException(status_code=404, detail="File not found in message")
        # Scale the timeout with size (≈256 KB/s floor) so slow big-video downloads aren't
        # cut short at the fixed 120s used for regular files.
        try:
            file_size = int(message.video.file_size or 0)
        except Exception:
            file_size = 0
        timeout = _media_download_timeout(file_size)
        logger.info(f"Downloading large video file {file_unique_id} to temporary path {temp_file_path} (timeout={timeout:.0f}s)")
        try:
            file_path = await _download_atomic(file_id, temp_file_path, timeout)
        except asyncio.TimeoutError:
            logger.error(f"Timeout downloading large video {file_unique_id}")
            raise HTTPException(status_code=504, detail="Download timeout")
        logger.info(f"Downloaded large video file {file_unique_id} to temporary path {temp_file_path}")
        return file_path, False

    # Normal caching flow
    cache_path = os.path.join(post_dir, file_unique_id)
    if os.path.exists(cache_path):
        # Check if the cached file is zero size
        if os.path.getsize(cache_path) == 0:
            logger.warning(f"zero_size_cache_found: Found zero-size cached file: {cache_path}. Deleting and attempting redownload.")
            try:
                os.remove(cache_path)
                logger.info(f"Removed zero-size cached file: {cache_path}")
            except OSError as e:
                logger.error(f"cleanup_error: Failed to remove zero-size cached file {cache_path}: {e}")
            # Do not raise error here, proceed to download below
        else:
            # File exists and is not zero size, record access timestamp and return.
            # Record into the accumulator instead of touching SQLite on the hot path; the
            # background flush persists it. Key channel as str(channel) — see _access_updates.
            logger.info(f"Found cached media file: {cache_path}")
            _access_updates[(str(channel), post_id, file_unique_id)] = datetime.now().timestamp()
            return cache_path, False

    file_id = await find_file_id_in_message(message, file_unique_id)
    if not file_id:
        error_message = f"Media with file_unique_id {file_unique_id} not found in message {post_id} for channel {channel}"
        logger.error(error_message)

        # Remove the invalid entry from the SQLite database
        try:
            await asyncio.to_thread(
                remove_media_file_ids_sync,
                DB_PATH, [(str(channel), post_id, file_unique_id)]
            )
            logger.info(f"Removed invalid entry for {channel}/{post_id}/{file_unique_id} from SQLite")
        except Exception as e:
            logger.error(f"Failed to remove entry for {channel}/{post_id}/{file_unique_id} from SQLite: {str(e)}")

        raise HTTPException(status_code=404, detail="File not found in message")

    # Download through a unique `.part.` file and atomically publish to the final cache
    # path. _download_atomic owns the zero-size check, the race-loser cleanup, and the
    # rename-only-if-absent logic, keeping the "final name = complete file" invariant.
    try:
        file_path = await _download_atomic(file_id, cache_path, timeout=float(Config["media_download_timeout_min"]))
    except asyncio.TimeoutError:
        logger.error(f"Timeout downloading media {file_unique_id}")
        raise HTTPException(status_code=504, detail="Download timeout")

    logger.info(f"Downloaded media file {file_unique_id} to {cache_path}")
    return file_path, False


def remove_old_cached_files_sync(media_files: list, cache_dir: str) -> tuple[list, int]:
    """
    Remove files that haven't been accessed for more than 20 days
    Returns tuple of (updated media files list, number of removed files)
    """
    current_time = datetime.now().timestamp()
    updated_media_files = []
    files_removed = 0

    for file_data in media_files:
        try:
            channel = file_data.get('channel')
            post_id = file_data.get('post_id')
            file_unique_id = file_data.get('file_unique_id')
            
            if not all([channel, post_id, file_unique_id]):
                continue

            last_access_time = file_data.get('added', 0)
            days_since_access = (current_time - last_access_time) / (24 * 3600)

            if days_since_access > 20:
                cache_path = os.path.join(cache_dir, str(channel), str(post_id), file_unique_id)
                
                if os.path.exists(cache_path):
                    try:
                        os.remove(cache_path)
                        # try to remove empty parent directories
                        post_dir = os.path.dirname(cache_path)
                        channel_dir = os.path.dirname(post_dir)
                        if not os.listdir(post_dir):
                            os.rmdir(post_dir)
                        if not os.listdir(channel_dir):
                            os.rmdir(channel_dir)
                            
                        files_removed += 1
                        logger.info(f"Removed old cached file: {cache_path}, last access {days_since_access:.1f} days ago")
                    except Exception as e:
                        logger.error(f"Failed to remove cached file {cache_path}: {str(e)}")
                        updated_media_files.append(file_data)
                continue

            updated_media_files.append(file_data)

        except Exception as e:
            logger.error(f"Error processing cache entry {file_data}: {str(e)}")
            continue

    # Clean up temporary files: "temp_"-prefixed (large videos) and ".tmp."-suffixed (race-condition downloads)
    temp_threshold = 3600  # 1 hour in seconds
    for root, _, files in os.walk(cache_dir):
        for file in files:
            is_large_video_temp = file.startswith("temp_")
            # Match the partial-download suffixes: the new `.part.{32 hex}` and the legacy
            # `.tmp.{32 hex}` (old stubs may still be on disk after the rename to .part.).
            is_race_temp = bool(re.match(r'^.+\.(part|tmp)\.[0-9a-f]{32}$', file))
            if not (is_large_video_temp or is_race_temp):
                continue
            file_path = os.path.join(root, file)
            try:
                file_mod_time = os.path.getmtime(file_path)
                if time.time() - file_mod_time > temp_threshold:
                    if is_large_video_temp and not is_race_temp:
                        # Extract channel/post/file_id from path and name
                        rel_path = os.path.relpath(os.path.dirname(file_path), cache_dir)
                        parts = rel_path.split(os.sep)
                        if len(parts) != 2:
                            logger.warning(f"Unexpected path depth for large-video temp file, skipping: {file_path}")
                            continue
                        channel, post_id = parts
                        file_unique_id = file[5:]  # Remove 'temp_' prefix
                        # Remove file
                        os.remove(file_path)
                        files_removed += 1
                        logger.info(f"Removed temporary large video file: {file_path}")
                        # Also remove the temporary file entry from the in-memory list
                        updated_media_files = [
                            f for f in updated_media_files
                            if not (f.get('channel') == channel and
                                f.get('post_id') == int(post_id) and
                                f.get('file_unique_id') == file_unique_id)
                        ]
                    elif is_race_temp:
                        # Race-condition temp file — just delete from disk, no SQLite entry to remove
                        os.remove(file_path)
                        files_removed += 1
                        logger.info(f"Removed stale race-condition temp file: {file_path}")
            except Exception as e:
                logger.error(f"Failed to remove temporary file {file_path}: {str(e)}")

    # Remove empty post-/channel- directories. A permanently-dead file that never downloads
    # (backoff-skipped forever) can leave an empty dir behind now that makedirs is lazy, and
    # empty dirs pile up and slow the sweeper's os.walk and calculate_cache_stats. Walk
    # bottom-up so an emptied post dir lets its (now-empty) channel dir go in the same pass;
    # rmdir only ever removes an EMPTY dir, so a dir holding a live file is left untouched.
    # OSError is swallowed: a non-empty dir is legitimate, and a race with a concurrent fresh
    # mkdir/download (rmdir vs mkdir) must never crash the sweep — _download_atomic recreates
    # the dir on demand.
    for root, _dirs, _files in os.walk(cache_dir, topdown=False):
        if os.path.abspath(root) == os.path.abspath(cache_dir):
            continue  # never remove the cache root itself
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass

    return updated_media_files, files_removed


async def download_new_files(media_files: list, cache_dir: str) -> None:
    """
    Queue files that are not in cache yet for background download
    """
    if not media_files:
        logger.info("No media files found for download")
        return

    files_queued = 0
    for file_data in media_files:
        try:
            channel = file_data.get('channel')
            post_id = file_data.get('post_id')
            file_unique_id = file_data.get('file_unique_id')
            
            if not all([channel, post_id, file_unique_id]):
                logger.error(f"Invalid file data: {file_data}")
                continue

            # Skip files that recently kept failing — do not re-queue them on every cache
            # sweep (that retry-storm is what kept the download slots jammed). The backoff
            # cap is >= the sweep interval, so a dead file is skipped for whole sweeps rather
            # than re-queued every pass. The backoff expires on its own; a successful download
            # elsewhere clears it.
            if _download_backoff_remaining((str(channel), int(post_id), file_unique_id)) > 0:
                continue

            channel_dir = os.path.join(cache_dir, str(channel))
            post_dir = os.path.join(channel_dir, str(post_id))
            # No makedirs here: the dir is created lazily by _download_atomic only when a
            # partial is actually written, so a queued-but-never-downloaded file leaves no
            # empty dir behind. os.path.exists on a not-yet-created dir is False, as intended.

            # Skip if temp file exists (large videos)
            temp_path = os.path.join(post_dir, f"temp_{file_unique_id}")
            if os.path.exists(temp_path):
                continue
                
            cache_path = os.path.join(post_dir, file_unique_id)
            if not os.path.exists(cache_path):
                # Skip files already queued/in-flight so a slow-draining queue (FloodWait)
                # isn't refilled with duplicates on every sweep. Same key shape as the
                # worker's bg_key. No await between this check and the add -> race-free.
                key = (str(channel), int(post_id), file_unique_id)
                if key in _queued_media:
                    continue
                try:
                    # put_nowait so a full queue raises QueueFull instead of blocking
                    # cache_media_files (and thus the sweeper) forever. `await put()`
                    # never raises QueueFull, which made the except below dead code.
                    _queued_media.add(key)
                    download_queue.put_nowait((channel, post_id, file_unique_id))
                    files_queued += 1
                    logger.debug(f"Queued for background download: {channel}/{post_id}/{file_unique_id}")
                except asyncio.QueueFull:
                    _queued_media.discard(key)
                    logger.warning(f"Download queue is full, skipping {channel}/{post_id}/{file_unique_id}")
                    break
        
        except Exception as e:
            logger.error(f"Failed to queue download for {channel}/{post_id}/{file_unique_id}: {str(e)}")
            continue

    if files_queued > 0:
        logger.info(f"Queued {files_queued} files for background download")


async def background_download_worker():
    """Worker that processes downloads from queue"""
    while True:
        # get() is OUTSIDE the try so task_done() in finally always balances exactly one
        # successful get(). Cancellation propagates cleanly here (nothing to unbalance).
        item = await download_queue.get()
        channel, post_id, file_unique_id = item
        bg_key = (str(channel), int(post_id), file_unique_id)
        logger.info(f"Background download: {channel}/{post_id}/{file_unique_id}")
        try:
            # Route through _download_deduped so a live request already downloading THIS file
            # and the background worker share ONE actual download (one Pyrogram slot) instead
            # of both fetching it in parallel. The permit is acquired INSIDE the runner, so we
            # pass the background semaphore rather than wrapping the call in `async with`.
            # Failure accounting (negative cache + permanent/transient classification) and the
            # success clear are owned by _download_deduped's runner — the worker must NOT
            # record/clear here, or a real failure would double-increment the counter.
            await _download_deduped(channel, post_id, file_unique_id, BACKGROUND_DOWNLOAD_SEMAPHORE)
            await asyncio.sleep(2)
        except errors.FloodWait as e:
            # Must be caught BEFORE the generic Exception (FloodWait subclasses RPCError),
            # otherwise the worker would hammer Telegram while under a flood wait. A flood
            # wait is a global throttle, not a per-file fault, so it does NOT arm backoff
            # (the runner excludes it too); the worker sleeps the WHOLE queue to stop hammering.
            logger.warning(f"bg_download_floodwait: {channel}/{post_id}/{file_unique_id} sleeping {e.value}s")
            await asyncio.sleep(min(int(e.value) + 5, 900))
        except DownloadAdmissionTimeout:
            # The background semaphore was saturated — a server-load signal, not a file fault
            # (the runner did NOT arm backoff). Leave the item for a later sweep to re-queue.
            logger.warning(f"bg_download_admission_timeout: {channel}/{post_id}/{file_unique_id} left for a later sweep")
        except Exception as e:
            # The runner already classified and negative-cached this failure; only log here.
            logger.error(f"Background download error for {channel}/{post_id}/{file_unique_id}: {e}")
        finally:
            # Free the dedup slot so this file can be re-enqueued by a later sweep if it is
            # still missing (e.g. the download failed or was throttled). No await here, so the
            # discard and task_done() happen atomically w.r.t. download_new_files.
            _queued_media.discard(bg_key)
            download_queue.task_done()

async def cache_media_files() -> None:
    """Background task for cache management: removes old files and downloads new ones"""
    delay = Config["cache_sweep_interval"]
    while True:
        try:
            # Load all media file ID records from SQLite
            media_files = await asyncio.to_thread(get_all_media_file_ids_sync, DB_PATH)

            cache_dir = MEDIA_CACHE_DIR
            updated_media_files, files_removed = await asyncio.to_thread(remove_old_cached_files_sync, media_files, cache_dir)

            try:
                # Compute the DB diff UNCONDITIONALLY (not only when files_removed > 0):
                # remove_old_cached_files_sync drops an entry older than 20 days from the
                # surviving list even when its file was already gone from disk (that branch
                # does NOT bump files_removed). If we only purged when a real file was removed,
                # such a fileless-but-expired row would stay in SQLite forever. The surviving
                # set reflects what is actually left after the sweep; removed_entries are the
                # rows present in the OLD set but not in the surviving set.
                updated_set = {
                    (r['channel'], r['post_id'], r['file_unique_id'])
                    for r in updated_media_files
                }
                removed_entries = [
                    (r['channel'], r['post_id'], r['file_unique_id'])
                    for r in media_files
                    if (r['channel'], r['post_id'], r['file_unique_id']) not in updated_set
                ]
                if removed_entries:
                    await asyncio.to_thread(remove_media_file_ids_sync, DB_PATH, removed_entries)
                    logger.info(f"cache_sweep: purged {len(removed_entries)} entries "
                                f"({files_removed} files removed from disk)")
            except Exception as e:
                logger.error(f"Failed to remove old entries from SQLite: {str(e)}")

            await download_new_files(updated_media_files, cache_dir)

            # Age-sweep the history/chatinfo cache once per pass (dead channels, orphaned
            # uuid tmp files). MUST run off-loop via to_thread (blocking filesystem walk).
            await asyncio.to_thread(sweep_tgcache)

            await asyncio.sleep(delay)  # Check every delay seconds

        except Exception as e:
            logger.error(f"Cache media files error: {str(e)}")
            await asyncio.sleep(delay)


def calculate_cache_stats() -> dict[str, Any]:
    """
    Calculate cache statistics including file count, total size in MB, and time difference in days.
    Returns a dictionary with keys: 'cache_files_count', 'cache_total_size_mb', 'cache_time_diff_days', 'channels'.
    """
    base_cache_dir = MEDIA_CACHE_DIR
    cache_files_count = 0
    cache_total_size_bytes = 0
    channels_stats = {}
    
    if os.path.isdir(base_cache_dir):
        # Recursively walk through all subdirectories
        for root, _, files in os.walk(base_cache_dir):
            for current_file in files:
                file_path = os.path.join(root, current_file)
                file_size = os.path.getsize(file_path)
                cache_files_count += 1
                cache_total_size_bytes += file_size
                
                # Calculate per-channel statistics
                rel_path = os.path.relpath(root, base_cache_dir)
                channel = rel_path.split(os.sep, maxsplit=1)[0]  # First directory is channel
                
                if channel not in channels_stats:
                    channels_stats[channel] = {
                        'files_count': 0,
                        'size_mb': 0.0
                    }
                
                channels_stats[channel]['files_count'] += 1
                channels_stats[channel]['size_mb'] = round(
                    channels_stats[channel]['size_mb'] + (file_size / (1024 * 1024)), 2
                )
                    
        cache_total_size_mb = round(cache_total_size_bytes / (1024 * 1024), 2)  # rounded size in MB
    else:
        cache_files_count = 0
        cache_total_size_mb = 0

    cache_times = []
    try:
        media_files = get_all_media_file_ids_sync(DB_PATH)
        for entry in media_files:
            if "added" in entry:
                cache_times.append(entry["added"])
    except Exception as e:
        logger.error(f"Error reading media file IDs from SQLite: {str(e)}")
    if cache_times:
        cache_time_diff_seconds = max(cache_times) - min(cache_times)
        cache_time_diff_days = round(cache_time_diff_seconds / 86400, 2)  # rounded to two decimals
    else:
        cache_time_diff_days = 0

    return {
        "cache_files_count": cache_files_count,
        "cache_total_size_mb": cache_total_size_mb,
        "cache_time_diff_days": cache_time_diff_days,
        "channels": channels_stats
    }


def is_local_request(request: Request) -> bool:
    """Return True if the request originates from a local (loopback) address.

    When the service runs behind a trusted reverse proxy (configured via
    TRUSTED_PROXIES env var), the real client IP is taken from X-Real-IP or
    X-Forwarded-For instead of the TCP connection address.
    """
    local_hosts = {"127.0.0.1", "::1"}

    if not request or not request.client or not request.client.host:
        return False

    connection_ip = request.client.host
    trusted_proxies: list[str] = Config.get("trusted_proxies", [])

    if trusted_proxies and connection_ip in trusted_proxies:
        # Connection comes from a known proxy — resolve the real client IP.
        # X-Real-IP is a single IP set by nginx; prefer it.
        real_ip = request.headers.get("x-real-ip", "").strip()
        if not real_ip:
            # X-Forwarded-For may be a comma-separated list; rightmost is the value appended
            # by the trusted proxy itself and cannot be forged by the client.
            forwarded_for = request.headers.get("x-forwarded-for", "").strip()
            real_ip = forwarded_for.split(",")[-1].strip() if forwarded_for else ""
        if not real_ip:
            # Trusted proxy did not supply any forwarding header — misconfiguration.
            # Fail safe: do not grant local access when the real client IP is unknown.
            logger.warning(
                "Trusted proxy %s provided no X-Real-IP or X-Forwarded-For header; "
                "treating as non-local request for safety.",
                connection_ip
            )
            return False
        client_ip = real_ip
    else:
        # Direct connection (no trusted proxy): use TCP connection address.
        client_ip = connection_ip

    # Security invariant: a forwarded client IP (X-Real-IP / X-Forwarded-For) is honored
    # ONLY when the real socket peer is itself in the TRUSTED_PROXIES allowlist; otherwise
    # the socket peer is used verbatim, so an external client cannot spoof "local" to skip auth.
    return client_ip in local_hosts


def _enforce_token(request: Request, token: str | None, endpoint: str) -> None:
    """Authorize a request against the shared-secret token unless it is local.

    Security invariants (issue #56): never log the presented token or the expected
    secret at any level. On failure we log only a short SHA-256 prefix of the
    *presented* token so an attack can be correlated in the logs without disclosing
    either secret. The comparison is constant-time (hmac.compare_digest over the
    UTF-8 bytes, which also tolerates a non-ASCII presented token without raising)
    to deny a timing side-channel on the token value.
    """
    if not Config["token"]:
        return
    if is_local_request(request):
        logger.info("Local request, skipping token check for %s.", endpoint)
        return
    if not hmac.compare_digest((token or "").encode("utf-8"), str(Config["token"]).encode("utf-8")):
        presented_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:8]
        logger.error("invalid_token for %s (presented token sha256:%s)", endpoint, presented_hash)
        raise HTTPException(status_code=403, detail="Invalid token")
    logger.info("Valid token for %s", endpoint)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Returns a simple landing page with service description and GitHub link."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pyrogram Bridge</title>
    <style>
        body {
            font-family: system-ui, -apple-system, sans-serif;
            max-width: 640px;
            margin: 80px auto;
            padding: 0 24px;
            color: #222;
            background: #fafafa;
        }
        h1 { font-size: 2rem; margin-bottom: 0.25em; }
        p  { font-size: 1.1rem; line-height: 1.6; color: #444; }
        a  { color: #0066cc; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <h1>Pyrogram Bridge</h1>
    <p>A Telegram-to-RSS/JSON bridge that exposes Telegram channel posts as RSS feeds and JSON API endpoints.</p>
    <p><a href="https://github.com/vvzvlad/pyrogram-bridge">github.com/vvzvlad/pyrogram-bridge</a></p>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/html/{channel}/{post_id}", response_class=HTMLResponse)
@app.get("/post/html/{channel}/{post_id}", response_class=HTMLResponse)
@app.get("/html/{channel}/{post_id}/{token}", response_class=HTMLResponse)  
@app.get("/post/html/{channel}/{post_id}/{token}", response_class=HTMLResponse)
async def get_post_html(channel: str, post_id: int, request: Request, token: str | None = None, debug: bool = False) -> HTMLResponse:
    _enforce_token(request, token, "HTML post")
        
    try:
        parser = PostParser(client.client)
        html_content = await parser.get_post(channel, post_id, 'html', debug)
        if not html_content:
            raise HTTPException(status_code=404, detail="Post not found")
        return HTMLResponse(content=html_content)
    except Exception as e:
        error_message = f"Failed to get HTML post for channel {channel}, post_id {post_id}: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e


@app.get("/json/{channel}/{post_id}")
@app.get("/post/json/{channel}/{post_id}")
@app.get("/json/{channel}/{post_id}/{token}")
@app.get("/post/json/{channel}/{post_id}/{token}")
async def get_post(channel: str, post_id: int, request: Request, token: str | None = None, debug: bool = False) -> Response:
    _enforce_token(request, token, "JSON post")
            
    try:
        parser = PostParser(client.client)
        json_content = await parser.get_post(channel, post_id, 'json', debug)
        if not json_content:
            raise HTTPException(status_code=404, detail="Post not found")
        return Response(content=json_content, media_type="application/json")
    except Exception as e:
        error_message = f"Failed to get JSON post for channel {channel}, post_id {post_id}: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e


@app.get("/raw_json/{channel}/{post_id}")
@app.get("/raw_json/{channel}/{post_id}/{token}")
async def get_raw_post_json(channel: str, post_id: int, request: Request, token: str | None = None) -> Response:
    _enforce_token(request, token, "raw JSON post")
            
    try:
        # Convert numeric channel ID to int if needed
        channel_id: Union[str, int] = channel
        if isinstance(channel, str) and channel.startswith('-100'):
            channel_id = int(channel)
            
        # Bound the RPC (stage-1 DoD: every Telegram RPC has a timeout). This
        # endpoint is not under the tg_rpc gate, so a hang here only blocks this
        # one request, but leaving it unbounded still violates the invariant.
        message = await asyncio.wait_for(
            client.client.get_messages(channel_id, post_id), timeout=30
        )
        if not message:
            raise HTTPException(status_code=404, detail="Post not found")
            
        # Return message as plain text using Pyrogram's built-in string representation
        return Response(content=str(message), media_type="text/plain")
    except Exception as e:
        error_message = f"Failed to get raw JSON post for channel {channel}, post_id {post_id}: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e


@app.get("/ping")
async def ping() -> JSONResponse:
    """Lightweight liveness probe for the container healthcheck.

    Reflects process/event-loop liveness (always answers in microseconds) and TG liveness
    from the watchdog's last-probe data. It MUST NOT issue any Telegram RPC (no get_me,
    no safe_get_messages), touch SQLite, or walk the filesystem — that is the whole point:
    it stays instant and truthful even while a real TG RPC is hung. It only reads the
    already-recorded watchdog timestamp and the is_connected bool.
    """
    age = client.watchdog_last_ok_age()          # seconds since last OK probe, None if never
    # is_connected is None before client.start() and a bool afterwards; coerce so the JSON
    # "connected" field is always a bool (never null) and the pre-start window reports false.
    connected = bool(client.client.is_connected)
    threshold = Config["tg_ping_unhealthy_after"]
    # age is None right after boot: the watchdog hasn't run its first probe yet. Treat that
    # as healthy (gate on connected only) so a freshly-started container is not killed before
    # its first probe cycle — otherwise start_period would have to cover a full watchdog interval.
    #
    # The staleness branch (age >= threshold => degraded) is only meaningful while the watchdog
    # is running to refresh age. With the watchdog DISABLED (TG_WATCHDOG_ENABLED=false) nothing
    # refreshes age — yet a disconnect-flap restart can still stamp it once (see _restart_client,
    # which runs before the watchdog-enabled gate), after which age only grows. Letting that
    # stale age drive /ping to 503 would spuriously fail the container healthcheck on a live
    # connection and trigger an autoheal restart. So gate staleness on the watchdog being on;
    # with it off, /ping is a pure connectivity check (no zombie-session detection — that
    # TG-liveness signal only exists while the watchdog runs).
    healthy = connected and (
        not Config["tg_watchdog_enabled"] or age is None or age < threshold
    )
    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "connected": connected,
            "last_probe_age_s": round(age, 1) if age is not None else None,
            "threshold_s": threshold,
        },
        status_code=200 if healthy else 503,
    )

@app.get("/health")
@app.get("/health/{token}")
async def health_check(request: Request, token: str | None = None) -> Response:
    _enforce_token(request, token, "health check")
            
    try:
        # Bound the Telegram RPC so a hung get_me cannot hang the healthcheck.
        me = await asyncio.wait_for(client.client.get_me(), timeout=10)

        # Offload heavy filesystem scanning to threadpool
        cache_stats = await asyncio.to_thread(calculate_cache_stats)
        
        config_info = {}    
        for config_key, config_value in Config.items():
            if any(sensitive in config_key.lower() for sensitive in ['token', 'tg_api_id', 'tg_api_hash']):
                config_info[config_key] = mask_sensitive_value(str(config_value)) if config_value else None
            else:
                config_info[config_key] = config_value

        data = {
            "status": "ok",
            "tg_connected": client.client.is_connected,
            "tg_name": me.username,
            "tg_id": me.id,
            "tg_phone": me.phone_number,
            "tg_first_name": me.first_name,
            "tg_last_name": me.last_name,
            # Posts that failed to render and were surfaced as a degraded placeholder
            # instead of being silently dropped (issue #60). A non-zero, growing value
            # flags a render regression that would otherwise be invisible.
            "render_failed": get_render_failed_count(),
            "config": config_info,
            **cache_stats
        }
        return Response(content=json.dumps(data), media_type="application/json")    
    except Exception as e:
        error_message = f"Failed to get health check: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e

@app.get("/media/{channel}/{post_id}/{file_unique_id}/{digest}", response_model=None)
@app.get("/media/{channel}/{post_id}/{file_unique_id}", response_model=None)
async def get_media(channel: str, post_id: int, file_unique_id: str, request: Request, digest: str | None = None, exp: int | None = None) -> Response:
    try:
        url = f"{channel}/{post_id}/{file_unique_id}"
        # exp is present only on optional TTL-signed URLs (MEDIA_URL_TTL_DAYS); default URLs
        # carry no exp query param, so this stays behaviour-identical when TTL is unset.
        verified = verify_media_digest(url, digest) if exp is None else verify_media_digest(url, digest, exp)
        if not verified:
            # Never log the expected digest: it is a signature over a secret key and would
            # leak signing-oracle output for a chosen url (#56). The presented digest is
            # attacker-supplied, so it is safe to log for correlation.
            logger.warning(f"Invalid media digest for {url} (presented digest {digest} rejected)")
            raise HTTPException(status_code=403, detail="Invalid URL signature")
            
        # Canonical filesystem/DB/API key. Computed AFTER the digest check (which must run
        # against the ORIGINAL url string) so 'Durov', 'durov' and '@durov' collapse to one
        # on-disk tree, one _access_updates/media_key identity and one download identity. The
        # canonical form is API-safe: usernames are case-insensitive on Telegram's side and
        # numeric '-100...' ids are preserved verbatim (download_media_file re-ints them).
        fs_channel = canonical_channel_key(channel)

        try: # Wrap the download and prepare call
            # Pre-download cache check: serve already-cached files without acquiring a
            # download permit or touching Telegram. The permit now lives inside
            # _download_deduped's runner, so this handler never holds one.
            cache_path = media_cache_path(fs_channel, post_id, file_unique_id)
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                # File is already in cache — serve directly.
                logger.info(f"pre_semaphore_cache_hit: {fs_channel}/{post_id}/{file_unique_id}")
                # Record the access time into the accumulator instead of firing a per-hit
                # SQLite write. Key channel as the canonical fs_channel — see _access_updates.
                _access_updates[(fs_channel, post_id, file_unique_id)] = datetime.now().timestamp()
                return await prepare_file_response(cache_path, request=request,
                                                   media_key=(fs_channel, post_id, file_unique_id))

            # Large videos (>100MB) are cached as temp_<fid>, NOT <fid>. Serve an existing
            # temp file directly too — before any permit or safe_get_messages RPC — since
            # links to such videos legitimately appear in feeds (post_parser.py:984-995).
            # No _access_updates: large videos aren't tracked in SQLite. The temp_* mtime
            # keepalive touch happens inside prepare_file_response.
            temp_cache_path = media_cache_path(fs_channel, post_id, f"temp_{file_unique_id}")
            if os.path.exists(temp_cache_path) and os.path.getsize(temp_cache_path) > 0:
                logger.info(f"pre_semaphore_temp_cache_hit: {fs_channel}/{post_id}/{file_unique_id}")
                return await prepare_file_response(temp_cache_path, request=request,
                                                   media_key=(fs_channel, post_id, file_unique_id))

            # A file that recently kept failing is in backoff: fast-reject instead of
            # occupying a scarce download slot (and a Pyrogram transmission permit) on a
            # request that will very likely hang again. Cached files already returned above,
            # so this only guards the live-download path.
            backoff_remaining = _download_backoff_remaining((fs_channel, post_id, file_unique_id))
            if backoff_remaining > 0:
                # A permanent failure (deleted post / genuinely-gone media) serves a clean 404
                # with NO Retry-After so the reader stops retrying, instead of looping 404->503
                # forever. A transient failure (throttle/timeout/DC issue) still gets a
                # 503 + Retry-After so the reader keeps retrying.
                if _download_failure_kind((fs_channel, post_id, file_unique_id)) == "permanent":
                    logger.info(f"media_backoff_permanent: {channel}/{post_id}/{file_unique_id} permanently unavailable")
                    raise HTTPException(status_code=404, detail="File not found")
                logger.info(f"media_backoff_skip: {channel}/{post_id}/{file_unique_id} in backoff {backoff_remaining:.0f}s")
                return Response(status_code=503, content="Media temporarily unavailable, retry later",
                                headers={"Retry-After": str(int(backoff_remaining) + 1)})

            # The live-download permit is acquired INSIDE _download_deduped's runner, so ONE
            # live download holds ONE permit no matter how many requests await the shared
            # Future. A saturated semaphore surfaces as DownloadAdmissionTimeout (-> 503
            # below); the waiter safety-net timeout surfaces as asyncio.TimeoutError (-> 504).
            file_path, delete_after = await _download_deduped(fs_channel, post_id, file_unique_id,
                                                              HTTP_DOWNLOAD_SEMAPHORE)
            if not file_path:
                raise HTTPException(status_code=404, detail="File not found")
            if file_path:
                return await prepare_file_response(file_path, request=request, delete_after=delete_after,
                                                   media_key=(fs_channel, post_id, file_unique_id))
        except ZeroSizeFileError as e: # Catch zero-size file errors
            logger.warning(f"zero_size_file_encountered: {str(e)}. Instructing client to retry.")
            return Response(
                status_code=503, # Service Unavailable
                content="File processing resulted in zero size, please try again in 10 seconds.",
                headers={"Retry-After": "10"}
            )
        except DownloadAdmissionTimeout:
            # Server saturated: no permit within the admission window. Retryable and NOT a
            # file failure, so no backoff was armed — tell the client to retry shortly.
            logger.warning(f"download_admission_timeout: {channel}/{post_id}/{file_unique_id} waited >30s for a download permit")
            return Response(status_code=503, content="Server busy, please retry",
                            headers={"Retry-After": "30"})
        except asyncio.TimeoutError:
            # Waiter safety-net timeout in _download_deduped (the shared Future outlived the
            # generous per-download bound). Report a retryable 504, not a generic 500.
            logger.warning(f"media_waiter_timeout: {channel}/{post_id}/{file_unique_id} exceeded the download wait budget")
            return Response(status_code=504, content="Download timeout",
                            headers={"Retry-After": "10"})
            
    except HTTPException:
        raise
    except errors.FloodWait as e:
        # MUST precede `except errors.RPCError` (FloodWait subclasses RPCError): a temporary
        # Telegram throttle must return a retryable 429, never a permanent 404.
        retry_after = min(int(e.value) + random.randint(1, 30), 300)
        logger.warning(f"media_flood_wait: {channel}/{post_id}/{file_unique_id} retry after {retry_after}s")
        return Response(status_code=429, content="Telegram flood wait",
                        headers={"Retry-After": str(retry_after)})
    except errors.RPCError as e:
        # Distinguish permanent from transient RPC errors. A 4xx-class error (CODE==400:
        # MsgIdInvalid, ChannelInvalid, PeerIdInvalid, ...) means the request can never
        # succeed -> 404. Anything else (5xx: InternalServerError, RpcCallFail, Timeout, ...)
        # is Telegram-side and retryable -> 503 + Retry-After, so a transient RPC blip does
        # not blacken a live file into a permanent 404. FloodWait is caught above (it
        # subclasses RPCError) and never reaches here.
        code = getattr(e, "CODE", None)
        logger.error(f"Media request RPC error for {channel}/{post_id}/{file_unique_id}: {type(e).__name__} - CODE={code} - {str(e)}")
        if code == 400:
            raise HTTPException(status_code=404, detail="File not found in Telegram") from e
        return Response(status_code=503, content="Telegram temporarily unavailable, retry later",
                        headers={"Retry-After": "60"})
    except ZeroSizeFileError as e: # Catch zero-size file errors FROM DOWNLOAD
        error_message = f"Failed to obtain valid media file for {channel}/{post_id}/{file_unique_id} after download attempt: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e
    except Exception as e:
        error_message = f"Failed to get media for {channel}/{post_id}/{file_unique_id}: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e

    # If the code reaches here, something is wrong with the logic above.
    # This part should theoretically be unreachable.
    logger.error(f"get_media reached unexpected state for {channel}/{post_id}/{file_unique_id}")
    raise HTTPException(status_code=500, detail="Internal server error: Unexpected state reached in media handling.")

# --------------------------------------------------------------------------- #
# Conditional GET for feeds (issue #62)
#
# Miniflux (and any conformant reader) sends If-None-Match / If-Modified-Since on
# every poll. Honoring them lets us answer an unchanged feed with 304 + no body,
# saving the response bandwidth and the reader-side parse. The server still renders
# the feed to derive the ETag — the issue mandates the ETag be a sha256 of the
# ACTUAL serialized body (a content signature), so it stays in lock-step with what
# we would have sent; the win is on the wire and in the reader, not on the render.
# --------------------------------------------------------------------------- #

# Cache-Control uses `private` on purpose: the feed URL carries an auth token and must
# not be cached by shared/intermediary proxies.
RSS_CACHE_MAX_AGE = 300  # seconds

# feedgen stamps <lastBuildDate> with datetime.now() on every serialize, so the raw
# body differs on each request even when the posts are identical. That field is
# build-time metadata, not content — strip it before hashing so the ETag reflects the
# feed content and stays stable across polls (otherwise 304 would never fire).
_LAST_BUILD_DATE_RE = re.compile(r"<lastBuildDate>.*?</lastBuildDate>", re.DOTALL)
_PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.DOTALL)


def _feed_etag(body: str) -> str:
    """Strong ETag = sha256 of the content-canonical body (volatile lastBuildDate removed)."""
    canonical = _LAST_BUILD_DATE_RE.sub("", body)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f'"{digest}"'


def _feed_last_modified(body: str) -> str | None:
    """Last-Modified = the freshest entry's pubDate, as an RFC 1123 (GMT) header value.

    Returns None when the body carries no parseable pubDate (e.g. the HTML feed, which
    has no per-entry dates) — the ETag alone still drives conditional GET in that case.
    """
    latest: datetime | None = None
    for raw in _PUBDATE_RE.findall(body):
        try:
            dt = parsedate_to_datetime(raw.strip())
        except (TypeError, ValueError):
            continue
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if latest is None or dt > latest:
            latest = dt
    if latest is None:
        return None
    return format_datetime(latest.astimezone(timezone.utc), usegmt=True)


def _feed_not_modified(request: Request, etag: str, last_modified: str | None) -> bool:
    """Decide 304 from the request validators (RFC 7232).

    If-None-Match takes precedence over If-Modified-Since when both are present.
    """
    inm = request.headers.get("if-none-match")
    if inm is not None:
        for tok in inm.split(","):
            tok = tok.strip()
            if tok == "*":
                return True
            if tok.startswith("W/"):  # weak comparison: drop the weak indicator
                tok = tok[2:].strip()
            if tok == etag:
                return True
        return False

    ims = request.headers.get("if-modified-since")
    if ims is not None and last_modified is not None:
        try:
            ims_dt = parsedate_to_datetime(ims)
            lm_dt = parsedate_to_datetime(last_modified)
        except (TypeError, ValueError):
            return False
        if ims_dt is None or lm_dt is None:
            return False
        if ims_dt.tzinfo is None:
            ims_dt = ims_dt.replace(tzinfo=timezone.utc)
        if lm_dt.tzinfo is None:
            lm_dt = lm_dt.replace(tzinfo=timezone.utc)
        # Not modified if the freshest entry is no newer than the client's copy.
        return lm_dt <= ims_dt

    return False


def _build_feed_response(request: Request, body: str, media_type: str) -> Response:
    """Attach ETag/Last-Modified/Cache-Control and short-circuit to 304 when unchanged."""
    etag = _feed_etag(body)
    last_modified = _feed_last_modified(body)
    headers = {
        "ETag": etag,
        "Cache-Control": f"private, max-age={RSS_CACHE_MAX_AGE}",
    }
    if last_modified is not None:
        headers["Last-Modified"] = last_modified
    if _feed_not_modified(request, etag, last_modified):
        # 304 carries the validators + Cache-Control but no body (RFC 7232 §4.1).
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type=media_type, headers=headers)


@app.get("/rss/{channel}", response_class=Response)
@app.get("/rss/{channel}/{token}", response_class=Response)
async def get_rss_feed(channel: str,
                        request: Request,
                        token: str | None = None, 
                        limit: int = 50, 
                        output_type: str = 'rss', 
                        exclude_flags: str | None = None,
                        exclude_text: str | None = None,
                        merge_seconds: int = 5,
                        ) -> Response:
    _enforce_token(request, token, "RSS endpoint")
        
    try:
        start_time = time.time()

        if output_type == 'rss':
            rss_content = await generate_channel_rss(channel,
                                                    client=client.client, 
                                                    limit=limit, 
                                                    exclude_flags=exclude_flags,
                                                    exclude_text=exclude_text,
                                                    merge_seconds=merge_seconds)
            elapsed_time = time.time() - start_time
            logger.info(f"rss_generation_timing: channel {channel}, generated in {elapsed_time:.3f} seconds")
            return _build_feed_response(request, rss_content, "application/xml")
        elif output_type == 'html':
            rss_content = await generate_channel_html(channel,
                                                    client=client.client, 
                                                    limit=limit, 
                                                    exclude_flags=exclude_flags,
                                                    exclude_text=exclude_text,
                                                    merge_seconds=merge_seconds)
            elapsed_time = time.time() - start_time
            logger.info(f"html_generation_timing: channel {channel}, generated in {elapsed_time:.3f} seconds")
            return _build_feed_response(request, rss_content, "text/html")
        else:
            raise HTTPException(status_code=400, detail=f"invalid_output_type: {output_type}")
    except ValueError as e:
        error_message = f"invalid_parameters_error: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=400, detail=error_message) from e
    except errors.FloodWait as e:
        wait_time = e.value
        random_additional_wait = random.uniform(0, wait_time * 2.5)
        total_wait_time = wait_time + random_additional_wait
        if total_wait_time > 190: total_wait_time = 190

        logger.warning(f"flood_wait_error: channel {channel}, retry after {total_wait_time:.1f} seconds (base: {wait_time}s, random: {random_additional_wait:.1f}s)")
        return Response(
            status_code=429,
            headers={"Retry-After": str(int(total_wait_time))},
            content="Too many requests, please try again later"
        )
    except Exception as e:
        error_message = f"rss_generation_error: channel {channel}, error {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e

@app.get("/flags", response_model=List[str])
@app.get("/flags/{token}", response_model=List[str])
async def get_available_flags(request: Request, token: str | None = None) -> Response:
    """Returns a list of all possible flags that can be assigned to posts."""
    _enforce_token(request, token, "flags endpoint")

    try:
        flags = PostParser.get_all_possible_flags()
        return Response(content=json.dumps(flags), media_type="application/json")
    except Exception as e:
        error_message = f"Failed to get flags list: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e 
