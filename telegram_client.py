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
        )
        self.max_disconnects = settings["tg_disconnect_flap_limit"]      # Max disconnects within the flap window before restart
        self._shutting_down = False  # Guard to prevent re-triggering restart during shutdown
        self._restarting = False            # Guard: an intentional in-process restart is in progress
        self._disconnect_times = []         # Monotonic timestamps of recent disconnects (sliding window)
        self.disconnect_window = settings["tg_disconnect_flap_window"]   # Seconds; window for flap detection
        self._watchdog_task = None
        self.watchdog_enabled = settings["tg_watchdog_enabled"]
        self.watchdog_interval = settings["tg_watchdog_interval"]
        self.watchdog_timeout = settings["tg_watchdog_timeout"]
        self.watchdog_failures = settings["tg_watchdog_failures"]
        self.watchdog_restart_timeout = settings["tg_watchdog_restart_timeout"]
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
            logger.critical(f"connection_handler: disconnect flap limit reached ({self.max_disconnects} within {self.disconnect_window}s), restarting client")
            await self._restart_client()

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
        it forces an in-process restart.
        """
        consecutive_failures = 0
        logger.info(f"watchdog: started (interval={self.watchdog_interval}s, timeout={self.watchdog_timeout}s, failures={self.watchdog_failures})")
        try:
            while True:
                await asyncio.sleep(self.watchdog_interval)
                if self._shutting_down:
                    break
                if self._restarting:
                    # A restart is already underway; skip this probe cycle.
                    continue
                try:
                    await asyncio.wait_for(self.client.get_me(), timeout=self.watchdog_timeout)
                    if consecutive_failures:
                        logger.info(f"watchdog: liveness restored after {consecutive_failures} failed probe(s)")
                    consecutive_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    consecutive_failures += 1
                    logger.warning(f"watchdog: liveness probe failed ({consecutive_failures}/{self.watchdog_failures}): {type(e).__name__}: {e}")
                    if consecutive_failures >= self.watchdog_failures:
                        consecutive_failures = 0
                        await self._restart_client()
        except asyncio.CancelledError:
            logger.info("watchdog: stopped")
            raise
        except Exception as e:
            logger.critical(f"watchdog: loop crashed unexpectedly ({type(e).__name__}: {e}); liveness protection is now DISABLED until next start")

    async def _restart_client(self):
        """Recover the client without killing the process when possible.

        Performs an in-process restart (rebuilds session + recv loop), bounded by a timeout so a
        dead network layer cannot make it hang forever. Falls back to a full process restart
        (SIGTERM) if the in-process restart fails or times out.
        """
        if self._restarting or self._shutting_down:
            return
        self._restarting = True
        try:
            if self.client.is_connected:
                logger.critical("recovery: restarting Telegram client in-process (client.restart)")
                await asyncio.wait_for(self.client.restart(), timeout=self.watchdog_restart_timeout)
            else:
                logger.critical("recovery: client not connected, starting it in-process (client.start)")
                await asyncio.wait_for(self.client.start(), timeout=self.watchdog_restart_timeout)
            logger.info("recovery: in-process client recovery completed successfully")
            self._disconnect_times.clear()
            # Re-arm the watchdog in case it had previously crashed (self-healing).
            self._start_watchdog()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"recovery: in-process restart failed ({type(e).__name__}: {e}); falling back to process restart")
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

    async def safe_download_media(self, file_id, file_name, max_retries=2):
        """Wrapper with retry logic for download errors"""
        for attempt in range(max_retries):
            try:
                return await asyncio.wait_for(
                    self.client.download_media(file_id, file_name=file_name),
                    timeout=120.0
                )
            except Exception as e:
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
