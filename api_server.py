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
from typing import List, Union, Any

import json
from datetime import datetime
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
from rss_generator import generate_channel_rss, generate_channel_html
from post_parser import PostParser
from url_signer import verify_media_digest, generate_media_digest
from file_io import (DB_PATH, init_db_sync, get_all_media_file_ids_sync,
                     update_media_file_access_sync, update_media_file_access_bulk_sync,
                     remove_media_file_ids_sync,
                     get_mime_type_sync, set_mime_type_sync)

# Global python-magic instance for MIME type detection
magic_mime = magic.Magic(mime=True)

# Define custom exception for zero-size files
class ZeroSizeFileError(Exception):
    """Custom exception for zero-size files found or downloaded."""

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
# --- Failing-download backoff (negative cache) --------------------------------
# A media file whose download keeps failing (hang/timeout/not-found) must not be
# retried on every 60s cache sweep, nor keep occupying a scarce Pyrogram
# transmission slot. We remember recent failures per (channel, post_id,
# file_unique_id) and skip / fast-reject re-attempts until an exponentially
# growing backoff elapses. Cleared on the first successful download. All access is
# from the single event-loop thread, so a plain dict needs no lock.
_DOWNLOAD_BACKOFF_BASE = 60.0     # seconds; backoff after the first failure
_DOWNLOAD_BACKOFF_MAX = 3600.0    # seconds; cap on the backoff
# key -> (consecutive_failures, retry_not_before_monotonic)
_download_failures: dict[tuple[str, int, str], tuple[int, float]] = {}

def _download_backoff_remaining(key: tuple[str, int, str]) -> float:
    """Seconds until `key` may be retried; 0.0 if allowed now (or never failed)."""
    entry = _download_failures.get(key)
    if entry is None:
        return 0.0
    return max(0.0, entry[1] - time.monotonic())

def _record_download_failure(key: tuple[str, int, str]) -> None:
    """Register a failed download and (re)arm an exponential backoff for `key`."""
    fails = _download_failures.get(key, (0, 0.0))[0] + 1
    backoff = min(_DOWNLOAD_BACKOFF_MAX, _DOWNLOAD_BACKOFF_BASE * (2 ** (fails - 1)))
    _download_failures[key] = (fails, time.monotonic() + backoff)
    logger.warning(f"download_backoff_armed: {key[0]}/{key[1]}/{key[2]} failed {fails}x, next retry in {backoff:.0f}s")

def _clear_download_failure(key: tuple[str, int, str]) -> None:
    """Forget any recorded failure for `key` after a successful download."""
    if _download_failures.pop(key, None) is not None:
        logger.info(f"download_backoff_cleared: {key[0]}/{key[1]}/{key[2]} recovered")
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

    base_cache_dir = os.path.abspath("./data/cache")
    os.makedirs(base_cache_dir, exist_ok=True) # Create cache directory

    # Initialize SQLite database (creates table if not present)
    await asyncio.to_thread(init_db_sync, DB_PATH)

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
        # explanation_media (MessageContent objects). The bridge renders only
        # description_media, but explanation_media is searched too: if a signed URL
        # for it ever exists, the download must still work. getattr-only access —
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
        # Try to load the cached MIME type from the database (avoids repeated python-magic I/O)
        channel_key, post_id_key, file_unique_id_key = media_key
        media_type = await asyncio.to_thread(get_mime_type_sync, DB_PATH, channel_key, post_id_key, file_unique_id_key)

    if not media_type:
        # Cache miss or no media_key — detect with python-magic in a thread to avoid blocking the event loop
        try:
            media_type = await asyncio.to_thread(magic_mime.from_file, file_path)
        except Exception as e:
            logger.warning(f"Failed to determine MIME type using python-magic: {str(e)}")
            media_type = None

        # Persist the detected MIME type so the next request can skip python-magic
        if media_type and media_key is not None:
            await asyncio.to_thread(set_mime_type_sync, DB_PATH, channel_key, post_id_key, file_unique_id_key, media_type)

    if not media_type: media_type, _ = mimetypes.guess_type(file_path)  # Fallback to mimetypes if python-magic failed
    if not media_type: media_type = "application/octet-stream"  # Final fallback to octet-stream

    logger.debug(f"Determined media type for {os.path.basename(file_path)}: {media_type}")

    # Delete the temporary file once the response has been fully sent (stage-2 delete_after).
    # FileResponse runs this BackgroundTask after streaming the body.
    background = BackgroundTask(delayed_delete_file, file_path) if delete_after else None

    # FileResponse handles Range/If-Range/206/416/multipart and sets
    # Accept-Ranges/ETag/Last-Modified itself (from the stat_result we pass). Do NOT
    # hand-build Content-Disposition: FileResponse forms it from filename= —
    # `inline; filename="x"` for an ASCII name, adding `filename*=UTF-8''x` only for a
    # non-ASCII name. It uses setdefault, so a manual header would OVERRIDE it, not double
    # it; letting FileResponse own it keeps the RFC 5987 encoding correct.
    #
    # Files are addressed by file_unique_id which is immutable in Telegram, so it is safe
    # to cache them aggressively on the client side.
    return FileResponse(
        file_path,
        media_type=media_type,
        filename=os.path.basename(file_path),
        content_disposition_type="inline",
        stat_result=stat_result,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
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


async def _download_deduped(channel: Union[str, int], post_id: int, file_unique_id: str) -> tuple[Union[str, None], bool]:
    """Deduplicate concurrent downloads of the same media by (channel, post_id, fid).

    The first request for a key runs download_media_file in a DETACHED task and shares its
    Future; concurrent requests await the same Future (bounded by wait_for). The detached
    task sets the Future's result/exception (on success/failure) and its finally ALWAYS
    pops the key — so both happen before the task ends: a completed Future never leaves the
    key stuck, and a client disconnect (cancelling only the awaiting request coroutine) can
    neither cancel the download nor hang other waiters.
    """
    key = (str(channel), post_id, file_unique_id)
    fut = _inflight.get(key)
    if fut is None:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _inflight[key] = fut

        async def _runner():
            try:
                result = await download_media_file(channel, post_id, file_unique_id)
                _clear_download_failure(key)
                if not fut.done():
                    fut.set_result(result)
            except BaseException as e:  # noqa: BLE001 — must forward ANY failure to waiters
                # Arm backoff for real failures only, never for a shutdown-time cancel.
                if isinstance(e, Exception):
                    _record_download_failure(key)
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
    base_cache_dir = os.path.abspath("./data/cache")
    
    # Create nested cache structure
    channel_dir = os.path.join(base_cache_dir, str(channel))
    post_dir = os.path.join(channel_dir, str(post_id))
    os.makedirs(post_dir, exist_ok=True)
    
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
        raise HTTPException(status_code=404, detail="Post not found or deleted")

    if message.media == MessageMediaType.POLL:
        return None, False
    
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

            # Skip files that recently kept failing — do not re-queue them on every 60s
            # sweep (that retry-storm is what kept the download slots jammed). The backoff
            # expires on its own; a successful download elsewhere clears it.
            if _download_backoff_remaining((str(channel), int(post_id), file_unique_id)) > 0:
                continue

            channel_dir = os.path.join(cache_dir, str(channel))
            post_dir = os.path.join(channel_dir, str(post_id))
            os.makedirs(post_dir, exist_ok=True)
            
            # Skip if temp file exists (large videos)
            temp_path = os.path.join(post_dir, f"temp_{file_unique_id}")
            if os.path.exists(temp_path):
                continue
                
            cache_path = os.path.join(post_dir, file_unique_id)
            if not os.path.exists(cache_path):
                try:
                    # put_nowait so a full queue raises QueueFull instead of blocking
                    # cache_media_files (and thus the sweeper) forever. `await put()`
                    # never raises QueueFull, which made the except below dead code.
                    download_queue.put_nowait((channel, post_id, file_unique_id))
                    files_queued += 1
                    logger.debug(f"Queued for background download: {channel}/{post_id}/{file_unique_id}")
                except asyncio.QueueFull:
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
            async with BACKGROUND_DOWNLOAD_SEMAPHORE:  # limit concurrent background downloads
                await download_media_file(channel, post_id, file_unique_id)
            _clear_download_failure(bg_key)
            await asyncio.sleep(2)
        except errors.FloodWait as e:
            # Must be caught BEFORE the generic Exception (FloodWait subclasses RPCError),
            # otherwise the worker would hammer Telegram while under a flood wait. A flood
            # wait is a global throttle, not a per-file fault, so it does NOT arm backoff.
            logger.warning(f"bg_download_floodwait: {channel}/{post_id}/{file_unique_id} sleeping {e.value}s")
            await asyncio.sleep(min(int(e.value) + 5, 900))
        except Exception as e:
            _record_download_failure(bg_key)
            logger.error(f"Background download error for {channel}/{post_id}/{file_unique_id}: {e}")
        finally:
            download_queue.task_done()

async def cache_media_files() -> None:
    """Background task for cache management: removes old files and downloads new ones"""
    delay = 60
    while True:
        try:
            # Load all media file ID records from SQLite
            media_files = await asyncio.to_thread(get_all_media_file_ids_sync, DB_PATH)

            cache_dir = os.path.abspath("./data/cache")
            updated_media_files, files_removed = await asyncio.to_thread(remove_old_cached_files_sync, media_files, cache_dir)

            if files_removed > 0:
                try:
                    # Determine which entries were removed and delete them from SQLite
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
                    logger.info(f"Removed {files_removed} old files from cache")
                except Exception as e:
                    logger.error(f"Failed to remove old entries from SQLite: {str(e)}")

            await download_new_files(updated_media_files, cache_dir)
            await asyncio.sleep(delay)  # Check every delay seconds

        except Exception as e:
            logger.error(f"Cache media files error: {str(e)}")
            await asyncio.sleep(delay)


def calculate_cache_stats() -> dict[str, Any]:
    """
    Calculate cache statistics including file count, total size in MB, and time difference in days.
    Returns a dictionary with keys: 'cache_files_count', 'cache_total_size_mb', 'cache_time_diff_days', 'channels'.
    """
    base_cache_dir = os.path.abspath("./data/cache")
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

    return client_ip in local_hosts


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
    if Config["token"] and not is_local_request(request):
        if token != Config["token"]:
            logger.error(f"Invalid token for HTML post: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for HTML post: {token}")
    elif Config["token"] and is_local_request(request):
        logger.info(f"Local request, skipping token check for HTML post.")
        
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
    if Config["token"] and not is_local_request(request):
        if token != Config["token"]:
            logger.error(f"Invalid token for JSON post: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for JSON post: {token}")
    elif Config["token"] and is_local_request(request):
        logger.info(f"Local request, skipping token check for JSON post.")
            
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
    if Config["token"] and not is_local_request(request):
        if token != Config["token"]:
            logger.error(f"Invalid token for raw JSON post: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for raw JSON post: {token}")
    elif Config["token"] and is_local_request(request):
        logger.info(f"Local request, skipping token check for raw JSON post.")
            
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
    if Config["token"] and not is_local_request(request):
        if token != Config["token"]:
            logger.error(f"Invalid token for health check: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for health check: {token}")
    elif Config["token"] and is_local_request(request):
        logger.info(f"Local request, skipping token check for health check.")
            
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
async def get_media(channel: str, post_id: int, file_unique_id: str, request: Request, digest: str | None = None) -> Response:
    try:
        url = f"{channel}/{post_id}/{file_unique_id}"
        if not verify_media_digest(url, digest):
            expected_digest = generate_media_digest(url)
            logger.error(f"Invalid digest for media {url}: {digest}, expected: {expected_digest}")
            raise HTTPException(status_code=403, detail="Invalid URL signature")
        #else:
        #    logger.info(f"Valid digest for media {url}: {digest}")   
            
        # Convert numeric channel ID to int if needed
        channel_id: Union[str, int] = channel
        if isinstance(channel, str) and channel.startswith('-100'):
            channel_id = int(channel)
            
        try: # Wrap the download and prepare call
            import time as _time

            # Pre-semaphore cache check: serve already-cached files without acquiring the semaphore
            base_cache_dir = os.path.abspath("./data/cache")
            channel_dir = os.path.join(base_cache_dir, str(channel))
            post_dir = os.path.join(channel_dir, str(post_id))
            cache_path = os.path.join(post_dir, file_unique_id)
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                # File is already in cache — skip semaphore and serve directly
                logger.info(f"pre_semaphore_cache_hit: {channel}/{post_id}/{file_unique_id}")
                # Record the access time into the accumulator instead of firing a per-hit
                # SQLite write. Key channel as str(channel) — see _access_updates.
                _access_updates[(str(channel), post_id, file_unique_id)] = datetime.now().timestamp()
                return await prepare_file_response(cache_path, request=request,
                                                   media_key=(str(channel), post_id, file_unique_id))

            # A file that recently kept failing is in backoff: fast-reject instead of
            # occupying a scarce download slot (and a Pyrogram transmission permit) on a
            # request that will very likely hang again. Cached files already returned above,
            # so this only guards the live-download path.
            backoff_remaining = _download_backoff_remaining((str(channel), post_id, file_unique_id))
            if backoff_remaining > 0:
                logger.info(f"media_backoff_skip: {channel}/{post_id}/{file_unique_id} in backoff {backoff_remaining:.0f}s")
                return Response(status_code=503, content="Media temporarily unavailable, retry later",
                                headers={"Retry-After": str(int(backoff_remaining) + 1)})

            _sem_wait_start = _time.monotonic()
            # Bound the wait for a live-download permit: a saturated semaphore must not
            # hang the request indefinitely. wait_for wraps ONLY the acquire; the permit
            # is released in the finally below only if the acquire actually succeeded (a
            # timed-out acquire never holds a permit, so it must not release one).
            #
            # CONSCIOUS TRADE-OFF (stage-2 review): the permit is held by the REQUEST, not
            # the detached download task. So this bounds concurrent *requests* (admission
            # control with a fast 503 on saturation), not strictly concurrent *downloads*:
            # if a client disconnects, its request coroutine releases the permit while the
            # detached download keeps running, so a burst of disconnects across DIFFERENT
            # files can transiently exceed the limit of live Telegram downloads. This is
            # deliberate — the download surviving a disconnect is the whole point of the
            # in-flight registry, and moving the permit into the runner would forfeit the
            # fast 503 (a saturated request would instead hang on the future for up to the
            # waiter timeout). Each download is itself timeout-bounded, so the overrun is
            # transient. If prod observation (stage 7) shows real download-count overrun
            # under disconnect churn, strengthen with a separate download-side limiter.
            try:
                await asyncio.wait_for(HTTP_DOWNLOAD_SEMAPHORE.acquire(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning(f"http_semaphore_timeout: {channel}/{post_id}/{file_unique_id} waited >30s for HTTP_DOWNLOAD_SEMAPHORE")
                return Response(status_code=503, content="Server busy, please retry",
                                headers={"Retry-After": "30"})
            try:
                _sem_wait = _time.monotonic() - _sem_wait_start
                if _sem_wait > 0.5:
                    logger.warning(f"diag_semaphore_wait: {channel}/{post_id}/{file_unique_id} waited {_sem_wait:.3f}s for HTTP_DOWNLOAD_SEMAPHORE")
                _dl_start = _time.monotonic()
                file_path, delete_after = await _download_deduped(channel_id, post_id, file_unique_id)
                _dl_elapsed = _time.monotonic() - _dl_start
                logger.info(f"diag_download_timing: {channel}/{post_id}/{file_unique_id} download_media_file took {_dl_elapsed:.3f}s (semaphore_wait={_sem_wait:.3f}s)")
            finally:
                HTTP_DOWNLOAD_SEMAPHORE.release()
            if not file_path:
                raise HTTPException(status_code=404, detail="File not found")
            if file_path:
                return await prepare_file_response(file_path, request=request, delete_after=delete_after,
                                                   media_key=(str(channel), post_id, file_unique_id))
        except ZeroSizeFileError as e: # Catch zero-size file errors
            logger.warning(f"zero_size_file_encountered: {str(e)}. Instructing client to retry.")
            return Response(
                status_code=503, # Service Unavailable
                content="File processing resulted in zero size, please try again in 10 seconds.",
                headers={"Retry-After": "10"}
            )
            
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
        logger.error(f"Media request RPC error for {channel}/{post_id}/{file_unique_id}: {type(e).__name__} - {str(e)}")
        raise HTTPException(status_code=404, detail="File not found in Telegram") from e
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
    if Config["token"] and not is_local_request(request):
        if token != Config["token"]:
            logger.error(f"invalid_token_error: token {token}, expected {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for RSS endpoint: {token}")
    elif Config["token"] and is_local_request(request):
        logger.info(f"Local request, skipping token check for RSS endpoint.")
        
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
            return Response(content=rss_content, media_type="application/xml")
        elif output_type == 'html':
            rss_content = await generate_channel_html(channel,
                                                    client=client.client, 
                                                    limit=limit, 
                                                    exclude_flags=exclude_flags,
                                                    exclude_text=exclude_text,
                                                    merge_seconds=merge_seconds)
            elapsed_time = time.time() - start_time
            logger.info(f"html_generation_timing: channel {channel}, generated in {elapsed_time:.3f} seconds")
            return Response(content=rss_content, media_type="text/html")
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
    if Config["token"] and not is_local_request(request):
        if token != Config["token"]:
            logger.error(f"Invalid token for flags endpoint: {token}, expected: {Config['token']}")
            raise HTTPException(status_code=403, detail="Invalid token")
        else:
            logger.info(f"Valid token for flags endpoint: {token}")
    elif Config["token"] and is_local_request(request):
        logger.info(f"Local request, skipping token check for flags endpoint.")

    try:
        flags = PostParser.get_all_possible_flags()
        return Response(content=json.dumps(flags), media_type="application/json")
    except Exception as e:
        error_message = f"Failed to get flags list: {str(e)}"
        logger.error(error_message)
        raise HTTPException(status_code=500, detail=error_message) from e 
