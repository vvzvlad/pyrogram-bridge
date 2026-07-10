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
import signal
import uvloop
import time
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

from pyrogram import Client
from pyrogram.handlers import DisconnectHandler
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

class TelegramClient:
    def __init__(self):
        self._ensure_session_directory()
        self.client = Client(
            name="pyro_bridge",
            api_id=settings["tg_api_id"],
            api_hash=settings["tg_api_hash"],
            workdir=settings["session_path"],
            proxy=settings["proxy"],  # MTProto proxy config, None if not set
            max_concurrent_transmissions=settings["tg_max_concurrent_transmissions"],
        )
        self.max_disconnects = settings["tg_disconnect_flap_limit"]      # Max disconnects within the flap window before restart
        self._shutting_down = False  # Guard to prevent re-triggering restart during shutdown
        self._restarting = False            # Guard: an intentional in-process restart is in progress
        # Consecutive media-download timeouts. A zombie media-DC connection makes every
        # download time out while the main-DC watchdog stays green; when this streak
        # reaches the threshold we reuse _restart_client() to rebuild the connection. Any
        # successful download resets it (see note_download_ok / note_download_timeout).
        self._download_timeout_streak = 0
        self.media_timeout_restart_threshold = settings["media_timeout_restart_threshold"]
        self._media_recovery_task = None   # strong ref to a scheduled recovery restart task
        self._on_restart_verified = None   # optional callback fired after a VERIFIED restart (see set_restart_callback)
        self._disconnect_times = []         # Monotonic timestamps of recent disconnects (sliding window)
        self.disconnect_window = settings["tg_disconnect_flap_window"]   # Seconds; window for flap detection
        self._watchdog_task = None
        self.watchdog_enabled = settings["tg_watchdog_enabled"]
        self.watchdog_interval = settings["tg_watchdog_interval"]
        self.watchdog_timeout = settings["tg_watchdog_timeout"]
        self.watchdog_failures = settings["tg_watchdog_failures"]
        self.watchdog_restart_timeout = settings["tg_watchdog_restart_timeout"]
        self.watchdog_heartbeat_every = settings["tg_watchdog_heartbeat_every"]
        # Watchdog diagnostics counters (cumulative for the process lifetime)
        self._wd_probe_count = 0          # successful liveness probes
        self._wd_probe_fail_count = 0     # failed liveness probes
        self._wd_restart_count = 0        # successful in-process restarts
        self._wd_fallback_count = 0       # SIGTERM fallbacks after a failed in-process restart
        self._wd_flap_trigger_count = 0   # times the disconnect-flap threshold was reached
        self._wd_last_ok_monotonic = None # monotonic timestamp of the last successful probe
        self._setup_connection_handlers()

    def _ensure_session_directory(self):
        try:
            os.makedirs(settings["session_path"], exist_ok=True)
            logger.debug(f'Session directory created/verified: {settings["session_path"]}')
        except Exception as e:
            logger.error(f'Failed to create session directory {settings["session_path"]}: {str(e)}')
            raise

    def _setup_connection_handlers(self):
        """Sets up connection/disconnection handlers"""
        self.client.add_handler(DisconnectHandler(self._on_disconnect))
        logger.info("connection_handlers: connection handlers set up")

    async def _on_disconnect(self, _client, _session=None):
        """Handles disconnection events from Telegram servers (best-effort safety net).

        NOTE: this handler only fires when Pyrogram calls session.stop(). It does NOT fire in
        the 'zombie session' case where the recv loop dies silently — that case is handled by
        the active watchdog (_watchdog_loop). Keep both paths.
        """
        # Ignore disconnects caused by intentional shutdown or by our own in-process restart
        # (client.restart() -> client.stop() -> session.stop() re-dispatches this handler).
        if self._shutting_down or self._restarting:
            logger.debug("connection_handler: ignoring disconnect event (shutdown/restart in progress)")
            return

        now = time.monotonic()
        # Sliding window: drop disconnects older than the window, then record this one.
        self._disconnect_times = [t for t in self._disconnect_times if now - t <= self.disconnect_window]
        self._disconnect_times.append(now)
        count = len(self._disconnect_times)
        logger.warning(f"connection_handler: connection lost ({count} within {self.disconnect_window}s window)")

        if count >= self.max_disconnects:
            self._wd_flap_trigger_count += 1
            logger.critical(
                f"connection_handler: disconnect flap limit reached "
                f"({count} disconnects within {self.disconnect_window}s window, limit={self.max_disconnects}), restarting client"
            )
            await self._restart_client(reason=f"disconnect flap ({count} in {self.disconnect_window}s)")

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

        The disconnect-only recovery never triggers when Pyrogram's recv loop dies silently
        (is_connected stays True, no Disconnect event). This loop periodically issues a real
        lightweight API call (get_me) bounded by a short timeout; after N consecutive failures
        it forces an in-process restart. Emits diagnostics so the behaviour can be reconstructed
        from logs afterwards.
        """
        consecutive_failures = 0
        logger.info(
            f"watchdog: started (interval={self.watchdog_interval}s, timeout={self.watchdog_timeout}s, "
            f"failures={self.watchdog_failures}, heartbeat_every={self.watchdog_heartbeat_every} probes)"
        )
        try:
            while True:
                await asyncio.sleep(self.watchdog_interval)
                if self._shutting_down:
                    break
                if self._restarting:
                    # A restart is already underway; skip this probe cycle.
                    logger.debug("watchdog: skip probe (restart in progress)")
                    continue
                probe_started = time.monotonic()
                try:
                    me = await asyncio.wait_for(self.client.get_me(), timeout=self.watchdog_timeout)
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
                    if self.watchdog_heartbeat_every > 0 and self._wd_probe_count % self.watchdog_heartbeat_every == 0:
                        logger.info(
                            f"watchdog: heartbeat — probes={self._wd_probe_count}, "
                            f"probe_failures={self._wd_probe_fail_count}, restarts={self._wd_restart_count}, "
                            f"sigterm_fallbacks={self._wd_fallback_count}, flap_triggers={self._wd_flap_trigger_count}, "
                            f"last_probe_latency={latency_ms:.0f}ms, is_connected={self.client.is_connected}"
                        )
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
                        f"watchdog: liveness probe FAILED ({consecutive_failures}/{self.watchdog_failures}) "
                        f"after {latency_ms:.0f}ms: {type(e).__name__}: {e} "
                        f"(is_connected={self.client.is_connected}, last_ok={last_ok}, "
                        f"total_failures={self._wd_probe_fail_count})"
                    )
                    if consecutive_failures >= self.watchdog_failures:
                        consecutive_failures = 0
                        await self._restart_client(reason=f"watchdog: {self.watchdog_failures} consecutive failed probes")
        except asyncio.CancelledError:
            logger.info(
                f"watchdog: stopped (probes={self._wd_probe_count}, probe_failures={self._wd_probe_fail_count}, "
                f"restarts={self._wd_restart_count}, sigterm_fallbacks={self._wd_fallback_count}, "
                f"flap_triggers={self._wd_flap_trigger_count})"
            )
            raise
        except Exception as e:
            logger.critical(f"watchdog: loop crashed unexpectedly ({type(e).__name__}: {e}); liveness protection is now DISABLED until next start")

    def set_restart_callback(self, callback) -> None:
        """Register a callback invoked after a VERIFIED in-process restart (verify_get_me OK).

        Used by api_server to clear its download negative cache: a completed restart rebuilds
        the whole client (all DCs, including the media DC), so any per-file download backoff is
        stale and a previously-failing file may now download. Kept as a plain hook so
        telegram_client never has to import api_server (which would be a circular import).
        The callback is synchronous, best-effort, and never fires on a failed/aborted restart.
        """
        self._on_restart_verified = callback

    async def _restart_client(self, reason: str = "unspecified"):
        """Recover the client without killing the process when possible.

        Performs an in-process restart (rebuilds session + recv loop), bounded by a timeout so a
        dead network layer cannot make it hang forever. Falls back to a full process restart
        (SIGTERM) if the in-process restart fails or times out.
        """
        if self._restarting or self._shutting_down:
            logger.debug(f"recovery: restart requested (reason='{reason}') but already restarting/shutting down — skipped")
            return
        self._restarting = True
        restart_started = time.monotonic()
        was_connected = self.client.is_connected
        logger.critical(
            f"recovery: TRIGGERED — reason='{reason}', is_connected={was_connected}, "
            f"in-process restarts so far={self._wd_restart_count}, sigterm fallbacks so far={self._wd_fallback_count}"
        )
        try:
            if was_connected:
                logger.warning("recovery: calling client.restart() (in-process teardown + reconnect)")
                await asyncio.wait_for(self.client.restart(), timeout=self.watchdog_restart_timeout)
            else:
                logger.warning("recovery: client not connected, calling client.start()")
                await asyncio.wait_for(self.client.start(), timeout=self.watchdog_restart_timeout)
            duration = time.monotonic() - restart_started
            self._wd_restart_count += 1
            # Verification probe to prove the network layer is actually back. It also gates the
            # verified-restart callback below: we only treat the restart as a real recovery
            # (and clear the download negative cache) when this probe actually succeeds.
            verify_ok = False
            try:
                me = await asyncio.wait_for(self.client.get_me(), timeout=self.watchdog_timeout)
                self._wd_last_ok_monotonic = time.monotonic()
                verify_ok = True
                verify = f", verify_get_me ok (me_id={getattr(me, 'id', None)})"
            except Exception as ve:
                verify = f", verify_get_me FAILED ({type(ve).__name__}: {ve})"
            logger.warning(
                f"recovery: in-process restart SUCCEEDED in {duration:.1f}s "
                f"(is_connected={self.client.is_connected}{verify}, total in-process restarts={self._wd_restart_count})"
            )
            self._disconnect_times.clear()
            # On a VERIFIED restart the whole client (all DCs, incl. the media DC) is
            # re-established, so any per-file download backoff is stale — fire the registered
            # hook (api_server clears its negative cache) so recovered media load immediately
            # instead of fast-503'ing until the backoff expires. Only on verify success, never
            # on a failed/aborted restart. Best-effort: a callback error must not abort recovery.
            if verify_ok and self._on_restart_verified is not None:
                try:
                    self._on_restart_verified()
                except Exception as cbe:
                    logger.warning(f"recovery: restart-verified callback raised {type(cbe).__name__}: {cbe}")
            # Re-arm the watchdog in case it had previously crashed (self-healing).
            self._start_watchdog()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            duration = time.monotonic() - restart_started
            self._wd_fallback_count += 1
            logger.error(
                f"recovery: in-process restart FAILED after {duration:.1f}s ({type(e).__name__}: {e}); "
                f"falling back to process restart (SIGTERM), total fallbacks={self._wd_fallback_count}"
            )
            self._restart_app()
        finally:
            self._restarting = False

    async def start(self):
        try:
            if not self.client.is_connected:
                await self.client.start()
                logger.info("Telegram client connected successfully")
                # Reset flap history on a fresh successful connection
                self._disconnect_times.clear()
                logger.info("connection_handler: connection established")
            self._start_watchdog()
        except Exception as e:
            logger.error(f"Failed to start Telegram client: {str(e)}")
            raise

    def _restart_app(self):
        """Restarts the application by sending SIGTERM to the process"""
        self._shutting_down = True  # Set flag before sending signal to suppress subsequent disconnect events
        logger.warning("connection_handler: restarting application by sending SIGTERM")
        try:
            # Use SIGTERM for proper Docker container restart
            logger.critical("connection_handler: sending SIGTERM signal")
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception as e:
            logger.error(f"connection_handler: error during restart: {str(e)}")
            # Emergency termination
            os._exit(1)

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
        """Count a media-download timeout; force a connection-rebuilding restart on a streak.

        A zombie media-DC connection makes EVERY download time out while the main-DC
        watchdog probe (get_me) stays green, so this is the only signal that can trigger
        recovery. The restart runs as a detached task so it never blocks the download path;
        the streak is reset immediately so we schedule at most one restart per streak.
        """
        self._download_timeout_streak += 1
        logger.warning(
            f"media_download_timeout: streak {self._download_timeout_streak}/{self.media_timeout_restart_threshold}"
        )
        if self._download_timeout_streak < self.media_timeout_restart_threshold:
            return
        self._download_timeout_streak = 0
        if self._restarting or self._shutting_down:
            return
        if self._media_recovery_task is not None and not self._media_recovery_task.done():
            return  # a recovery restart is already scheduled/running
        logger.critical(
            f"media_download: {self.media_timeout_restart_threshold} consecutive download timeouts — "
            f"scheduling media-connection restart (main-DC watchdog cannot see this)"
        )
        self._media_recovery_task = asyncio.create_task(
            self._restart_client(reason="media download timeout streak")
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
                # During shutdown the client may be in a half-restarted state
                # (e.g. a watchdog restart was cancelled mid-flight); ignore stop errors.
                logger.warning(f"Telegram client stop during shutdown raised {type(e).__name__}: {e}")
