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
import asyncio
import sys
import threading
import uvloop
import time
from typing import Any, NamedTuple, NoReturn, Optional
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

from pyrogram import Client, raw, errors, types
from pyrogram.handlers import DisconnectHandler
from config import get_settings

import kurigram_compat
# Install the defensive Rich* parse wrappers BEFORE any Client is created / any message is
# parsed (phase 1 of the Rich Messages epic, #83/#84). No-op on a Kurigram without Rich*
# classes, so a rollback to 2.2.23 does not crash-loop the container.
kurigram_compat.install()

logger = logging.getLogger(__name__)
settings = get_settings()

WATCHDOG_INTERVAL_S = 60
WATCHDOG_PROBE_TIMEOUT_S = 10
WATCHDOG_FAILURES = 3
WATCHDOG_HEARTBEAT_EVERY = 30
# Max concurrent Telegram file transmissions (Pyrogram get_file/save_file
# semaphores). Kurigram's default is 1, which lets a single hung download block
# ALL media downloads process-wide; 3 aligns with HTTP_DOWNLOAD_SEMAPHORE. This is
# only a blast-radius limiter — the real cure for a zombie media connection is the
# download-timeout-triggered restart below.
MAX_CONCURRENT_TRANSMISSIONS = 3
# After this many CONSECUTIVE media-download timeouts, exit the process so the
# container restart rebuilds the (zombie) media-DC connection. Any successful download
# resets the streak, so this only fires on a genuine death loop, not on the odd
# slow large-video timeout. The watchdog cannot catch this — it probes the main DC.
MEDIA_TIMEOUT_EXIT_THRESHOLD = 5
SESSION_PATH = "data"

# --------------------------------------------------------------------------- #
# Rich Messages phase 3 (#83 / #86): part=True full-content re-fetch.
# --------------------------------------------------------------------------- #
# Per-call timeout (seconds) for the raw messages.GetRichMessage re-fetch. Deliberately
# short and well under Config["tg_rpc_timeout"]: a partial rich message is a cheap-to-degrade
# enrichment, so a slow DC must NOT hold the global RPC gate — the feed breaker trips on the
# first timeout and the render proceeds with the partial tree + part plaque (fixed on the
# next re-fetch after the history TTL). Justified by #83: enrichment must never starve feeds.
RICH_ENRICH_RPC_TIMEOUT = 5

# Re-fetch attempt / failure counters, surfaced read-only to /health (same pattern as the
# #84 kurigram_compat degradation counters). Process-wide, lock-guarded: feeds may re-fetch
# concurrently. A growing failed/attempt ratio flags a DC or throttle problem otherwise
# invisible to the reader (part plaques would just silently persist).
_rich_fetch_lock = threading.Lock()
_rich_part_fetch_attempts = 0
_rich_part_fetch_failed = 0


def _incr_rich_fetch_attempt() -> None:
    global _rich_part_fetch_attempts
    with _rich_fetch_lock:
        _rich_part_fetch_attempts += 1


def _incr_rich_fetch_failed() -> None:
    global _rich_part_fetch_failed
    with _rich_fetch_lock:
        _rich_part_fetch_failed += 1


def get_rich_part_fetch_attempt_count() -> int:
    """Total GetRichMessage re-fetch attempts (feed + /media cascade) since process start."""
    with _rich_fetch_lock:
        return _rich_part_fetch_attempts


def get_rich_part_fetch_failed_count() -> int:
    """Re-fetch attempts that did not return a rich message (FloodWait/timeout/RPC/parse)."""
    with _rich_fetch_lock:
        return _rich_part_fetch_failed


def reset_rich_fetch_counters() -> None:
    """Reset the re-fetch counters. Test isolation only — not used in production."""
    global _rich_part_fetch_attempts, _rich_part_fetch_failed
    with _rich_fetch_lock:
        _rich_part_fetch_attempts = 0
        _rich_part_fetch_failed = 0


class RichFetchResult(NamedTuple):
    """Typed outcome of a single ``safe_get_rich_message`` re-fetch.

    ``rich_message`` is the parsed high-level object on success (possibly a #84 parse_failed
    SENTINEL — a part post whose fuller vectors still crashed), else None. ``outcome`` makes
    a FloodWait DISTINGUISHABLE from a plain timeout DISTINGUISHABLE from a generic error, so
    the feed breaker can react correctly (a global throttle and a slow DC both stop enrichment
    for the whole render; a one-off RPC error only skips the current post):

        "ok"         -> a RichMessage (or sentinel) was returned
        "floodwait"  -> Telegram FLOOD_WAIT (global throttle)  -> breaker
        "timeout"    -> the RPC exceeded ``timeout`` (slow DC)  -> breaker
        "error"      -> any other RPC / empty / parse failure   -> skip this post only
    """
    rich_message: Optional[Any]
    outcome: str


async def safe_get_rich_message(client: Client, chat_id, msg_id,
                                timeout: float = RICH_ENRICH_RPC_TIMEOUT) -> RichFetchResult:
    """Fetch the FULL rich content of a part=True post via raw ``messages.GetRichMessage``.

    ``client`` is a LIVE (raw) pyrogram Client — the transport is passed explicitly (mirroring
    ``tg_cache._reply_enrichment(client, ...)``) because both call sites hold the raw client:
    feed enrichment in tg_cache and the /media cascade in api_server (via ``client.client``).
    Like ``safe_get_messages`` this helper owns NO RPC gate: the CALLER decides whether to run
    under ``tg_rpc_bounded`` (feed path: yes, serialise with other feed RPCs; reader /media
    path: no, a media request must not queue behind feed enrichment).

    NEVER raises: every failure degrades to a typed ``RichFetchResult(None, ...)`` + a WARNING
    ``rich_part_fetch_failed``. This is load-bearing on the /media path — a BARE invoke there
    must not surface an ``RPCError(CODE==400)`` (e.g. MSG_ID_INVALID from a transient blip),
    which the download classifier would blacken into a PERMANENT 404 with row deletion.

    Pipeline: resolve_peer(chat_id) -> invoke(GetRichMessage(peer, id)) -> messages.Messages ->
    ``.messages[0].rich_message`` -> the #84-patched ``types.RichMessage._parse`` (recovers the
    ``part`` flag; a parse crash degrades to a sentinel).
    """
    _incr_rich_fetch_attempt()
    try:
        peer = await client.resolve_peer(chat_id)
        result = await asyncio.wait_for(
            client.invoke(raw.functions.messages.GetRichMessage(peer=peer, id=msg_id)),
            timeout=timeout,
        )
        msgs = getattr(result, "messages", None) or []
        if not msgs:
            _incr_rich_fetch_failed()
            logger.warning(f"rich_part_fetch_failed: empty Messages for {chat_id}/{msg_id}")
            return RichFetchResult(None, "error")
        raw_rich = getattr(msgs[0], "rich_message", None)
        if raw_rich is None:
            _incr_rich_fetch_failed()
            logger.warning(f"rich_part_fetch_failed: no rich_message on {chat_id}/{msg_id}")
            return RichFetchResult(None, "error")
        # The #84 message-contour wrapper never raises (crash -> sentinel), but guard anyway.
        parsed = await types.RichMessage._parse(client, raw_rich)
        if parsed is None:
            _incr_rich_fetch_failed()
            logger.warning(f"rich_part_fetch_failed: parse returned None for {chat_id}/{msg_id}")
            return RichFetchResult(None, "error")
        return RichFetchResult(parsed, "ok")
    except errors.FloodWait as e:
        _incr_rich_fetch_failed()
        logger.warning(f"rich_part_fetch_failed: FloodWait {getattr(e, 'value', None)}s for {chat_id}/{msg_id}")
        return RichFetchResult(None, "floodwait")
    except asyncio.TimeoutError:
        _incr_rich_fetch_failed()
        logger.warning(f"rich_part_fetch_failed: timeout after {timeout}s for {chat_id}/{msg_id}")
        return RichFetchResult(None, "timeout")
    except Exception as e:
        _incr_rich_fetch_failed()
        logger.warning(f"rich_part_fetch_failed: {type(e).__name__}: {e} for {chat_id}/{msg_id}")
        return RichFetchResult(None, "error")


class TelegramClient:
    def __init__(self):
        self._ensure_session_directory()
        self.client = Client(
            name="pyro_bridge",
            api_id=settings["tg_api_id"],
            api_hash=settings["tg_api_hash"],
            workdir=SESSION_PATH,
            proxy=settings["proxy"],  # MTProto proxy config, None if not set
            max_concurrent_transmissions=MAX_CONCURRENT_TRANSMISSIONS,
        )
        self._shutting_down = False  # Guard to prevent re-triggering restart during shutdown
        # Consecutive media-download timeouts. A zombie media-DC connection makes every
        # download time out while the main-DC watchdog stays green; when this streak
        # reaches the threshold the process exits (_exit_process) and the container restart
        # rebuilds the connection. Any successful download resets it (see note_download_ok /
        # note_download_timeout).
        self._download_timeout_streak = 0
        self._watchdog_task = None
        self.watchdog_enabled = settings["tg_watchdog_enabled"]
        # Watchdog diagnostics counters (cumulative for the process lifetime)
        self._wd_probe_count = 0          # successful liveness probes
        self._wd_probe_fail_count = 0     # failed liveness probes
        self._wd_last_ok_monotonic = None # monotonic timestamp of the last successful probe
        self._setup_connection_handlers()

    def _ensure_session_directory(self):
        try:
            os.makedirs(SESSION_PATH, exist_ok=True)
            logger.debug(f'Session directory created/verified: {SESSION_PATH}')
        except Exception as e:
            logger.error(f'Failed to create session directory {SESSION_PATH}: {str(e)}')
            raise

    def _setup_connection_handlers(self):
        """Sets up connection/disconnection handlers"""
        self.client.add_handler(DisconnectHandler(self._on_disconnect))
        logger.info("connection_handlers: connection handlers set up")

    async def _on_disconnect(self, _client, _session=None):
        """Logs disconnection events from Telegram servers; never triggers recovery.

        Kurigram reconnects on its own and dispatches this handler on every reconnect attempt
        (Session.restart() calls _stop()), for media sessions too, so a disconnect is not a
        sign of a hang. The handler also does NOT fire in the 'zombie session' case where the
        recv loop dies silently — a hung session is caught by the active watchdog
        (_watchdog_loop).
        """
        # Ignore disconnects caused by intentional shutdown
        # (client.stop() -> session.stop() re-dispatches this handler).
        if self._shutting_down:
            logger.debug("connection_handler: ignoring disconnect event (shutdown in progress)")
            return
        logger.warning("connection_handler: connection lost")

    def _start_watchdog(self):
        """Starts the active liveness watchdog task (idempotent)."""
        if not self.watchdog_enabled:
            logger.info("watchdog: disabled via configuration")
            return
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _watchdog_loop(self):
        """Active liveness probe for the 'zombie session' state.

        Pyrogram's recv loop can die silently (is_connected stays True, no Disconnect event).
        This loop periodically issues a real lightweight API call (get_me) bounded by a short
        timeout; after N consecutive failures it exits the process (_exit_process) so Docker
        restarts the container. After every successful probe it saves the Kurigram session.
        Emits diagnostics so the behaviour can be reconstructed from logs afterwards.
        """
        consecutive_failures = 0
        logger.info(
            f"watchdog: started (interval={WATCHDOG_INTERVAL_S}s, timeout={WATCHDOG_PROBE_TIMEOUT_S}s, "
            f"failures={WATCHDOG_FAILURES}, heartbeat_every={WATCHDOG_HEARTBEAT_EVERY} probes)"
        )
        try:
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                if self._shutting_down:
                    break
                probe_started = time.monotonic()
                try:
                    me = await asyncio.wait_for(self.client.get_me(), timeout=WATCHDOG_PROBE_TIMEOUT_S)
                    latency_ms = (time.monotonic() - probe_started) * 1000
                    self._wd_probe_count += 1
                    self._wd_last_ok_monotonic = time.monotonic()
                    if consecutive_failures:
                        logger.warning(
                            f"watchdog: liveness RESTORED after {consecutive_failures} failed probe(s) "
                            f"(latency={latency_ms:.0f}ms, me_id={getattr(me, 'id', None)})"
                        )
                        consecutive_failures = 0
                    else:
                        logger.debug(f"watchdog: probe ok (latency={latency_ms:.0f}ms, probe #{self._wd_probe_count})")
                    # Periodic proof-of-life heartbeat at INFO, so the ABSENCE of heartbeats in the
                    # logs is itself a signal that the watchdog died.
                    if WATCHDOG_HEARTBEAT_EVERY > 0 and self._wd_probe_count % WATCHDOG_HEARTBEAT_EVERY == 0:
                        logger.info(
                            f"watchdog: heartbeat — probes={self._wd_probe_count}, "
                            f"probe_failures={self._wd_probe_fail_count}, "
                            f"last_probe_latency={latency_ms:.0f}ms, is_connected={self.client.is_connected}"
                        )
                    # Kurigram writes the peers it learns (update_peers / update_usernames)
                    # without a commit, and _exit_process skips client.stop(), which would save
                    # them. Commit them here so a hard exit loses at most one interval of them.
                    # A failed save does not fail the probe.
                    try:
                        await self.client.storage.save()
                    except Exception as e:
                        logger.warning(f"watchdog: session save failed: {type(e).__name__}: {e}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    latency_ms = (time.monotonic() - probe_started) * 1000
                    consecutive_failures += 1
                    self._wd_probe_fail_count += 1
                    if self._wd_last_ok_monotonic is not None:
                        last_ok = f"{time.monotonic() - self._wd_last_ok_monotonic:.0f}s ago"
                    else:
                        last_ok = "never"
                    logger.warning(
                        f"watchdog: liveness probe FAILED ({consecutive_failures}/{WATCHDOG_FAILURES}) "
                        f"after {latency_ms:.0f}ms: {type(e).__name__}: {e} "
                        f"(is_connected={self.client.is_connected}, last_ok={last_ok}, "
                        f"total_failures={self._wd_probe_fail_count})"
                    )
                    if consecutive_failures >= WATCHDOG_FAILURES:
                        self._exit_process(f"watchdog: {WATCHDOG_FAILURES} consecutive failed probes")
        except asyncio.CancelledError:
            logger.info(
                f"watchdog: stopped (probes={self._wd_probe_count}, probe_failures={self._wd_probe_fail_count})"
            )
            raise
        except Exception as e:
            # A crashed loop would leave the hang undetected until the next start: exit now so
            # the new process starts with a working watchdog.
            self._exit_process(f"watchdog loop crashed: {type(e).__name__}: {e}")

    def _exit_process(self, reason: str) -> NoReturn:
        """Exit the process at once so Docker's restart policy brings the container back up.

        os._exit skips uvicorn, the lifespan and client.stop(), so nothing can hang on the way
        out. Code 1, because an `on-failure` restart policy restarts only on a non-zero code.
        """
        logger.critical(
            f"recovery: exiting process for a container restart — reason='{reason}', "
            f"probes={self._wd_probe_count}, probe_failures={self._wd_probe_fail_count}"
        )
        os._exit(1)

    async def start(self):
        try:
            if not self.client.is_connected:
                await self.client.start()
                logger.info("Telegram client connected successfully")
                logger.info("connection_handler: connection established")
            self._start_watchdog()
        except Exception as e:
            logger.error(f"Failed to start Telegram client: {str(e)}")
            raise

    def watchdog_last_ok_age(self) -> float | None:
        """Seconds since the last successful watchdog probe, or None if none succeeded yet.

        Reads only the already-recorded monotonic timestamp set by the watchdog loop; it
        never issues a Telegram RPC, so it is safe to call from the hot /ping path even
        while a real RPC is hung.
        """
        if self._wd_last_ok_monotonic is None:
            return None
        return time.monotonic() - self._wd_last_ok_monotonic

    def note_download_ok(self) -> None:
        """Reset the media-download timeout streak after any successful download."""
        if self._download_timeout_streak:
            logger.info(f"media_download: recovered, resetting timeout streak (was {self._download_timeout_streak})")
        self._download_timeout_streak = 0

    def note_download_timeout(self) -> None:
        """Count a media-download timeout; exit the process on a streak.

        A zombie media-DC connection makes EVERY download time out while the main-DC
        watchdog probe (get_me) stays green, so this is the only signal that can trigger
        recovery: at the threshold the process exits (_exit_process) and the container
        restart rebuilds every connection.
        """
        self._download_timeout_streak += 1
        logger.warning(
            f"media_download_timeout: streak {self._download_timeout_streak}/{MEDIA_TIMEOUT_EXIT_THRESHOLD}"
        )
        if self._download_timeout_streak < MEDIA_TIMEOUT_EXIT_THRESHOLD:
            return
        self._download_timeout_streak = 0
        if self._shutting_down:
            return
        self._exit_process(
            f"media download: {MEDIA_TIMEOUT_EXIT_THRESHOLD} consecutive timeouts "
            f"(the main-DC watchdog cannot see this)"
        )

    async def safe_get_messages(self, channel_id, post_id, max_retries=2):
        """Wrapper with retry logic for auth errors"""
        for attempt in range(max_retries):
            try:
                return await asyncio.wait_for(
                    self.client.get_messages(channel_id, post_id),
                    timeout=30.0
                )
            except Exception as e:
                if isinstance(e, KeyError) and attempt < max_retries - 1:
                    logger.warning(f"Auth error on attempt {attempt + 1}, retrying in 5s...")
                    await asyncio.sleep(5)
                    continue
                raise

    async def safe_download_media(self, file_id, file_name, max_retries=2, timeout: float = 120.0):
        """Wrapper with retry logic for download errors.

        `timeout` bounds each download attempt; for large videos the caller scales it
        with file size (see api_server._media_download_timeout). A timeout cancels the
        underlying download (freeing the Pyrogram transmission slot); a streak of timeouts
        escalates to a connection-rebuilding restart via note_download_timeout()."""
        for attempt in range(max_retries):
            try:
                result = await asyncio.wait_for(
                    self.client.download_media(file_id, file_name=file_name),
                    timeout=timeout
                )
                self.note_download_ok()
                return result
            except asyncio.TimeoutError:
                # Hung download: the wait_for above already cancelled it and released the
                # get_file semaphore. Count it toward the media-connection restart streak.
                self.note_download_timeout()
                raise
            except Exception as e:
                # INTENTIONAL: the restart streak counts CONSECUTIVE TIMEOUTS only. Non-timeout
                # download errors (RPCError, FILE_REFERENCE_EXPIRED, etc.) reach here and call
                # neither note_download_ok nor note_download_timeout, so they are streak-neutral:
                # they neither advance nor reset it. This is deliberate — a zombie media-DC
                # manifests as timeouts, not RPC errors, so only timeouts should escalate to a
                # connection-rebuilding restart; a burst of unrelated RPC errors must not.
                if isinstance(e, KeyError) and attempt < max_retries - 1:
                    logger.warning(f"Download auth error on attempt {attempt + 1}, retrying...")
                    await asyncio.sleep(5)
                    continue
                raise

    async def stop(self):
        # Suppress disconnect handling during intentional shutdown
        # (client.stop() dispatches the DisconnectHandler).
        self._shutting_down = True
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None
        if self.client.is_connected:
            try:
                await self.client.stop()
                logger.info("Telegram client disconnected")
            except Exception as e:
                # During shutdown the client may be in a half-restarted state; ignore stop errors.
                logger.warning(f"Telegram client stop during shutdown raised {type(e).__name__}: {e}")
