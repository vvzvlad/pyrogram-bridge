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
        )
        self.disconnect_count = 0
        self.max_disconnects = 3
        self._shutting_down = False  # Guard to prevent re-triggering restart during shutdown
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
        """Handles disconnection from Telegram servers"""
        # Ignore disconnects that are caused by the intentional shutdown sequence
        if self._shutting_down:
            logger.debug("connection_handler: ignoring disconnect event during shutdown")
            return
        self.disconnect_count += 1
        logger.warning(f"connection_handler: connection lost (#{self.disconnect_count})")
        
        if self.disconnect_count >= self.max_disconnects:
            logger.critical(f"connection_handler: reached disconnect limit ({self.max_disconnects})")
            self._restart_app()

    async def start(self):
        try:
            if not self.client.is_connected:
                await self.client.start()
                logger.info("Telegram client connected successfully")
                # Reset disconnect counter on successful connection
                self.disconnect_count = 0
                logger.info("connection_handler: connection established")
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
                if "KeyError" in str(e) and attempt < max_retries - 1:
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
                if "KeyError" in str(e) and attempt < max_retries - 1:
                    logger.warning(f"Download auth error on attempt {attempt + 1}, retrying...")
                    await asyncio.sleep(5)
                    continue
                raise

    async def stop(self):
        if self.client.is_connected:
            await self.client.stop()
            logger.info("Telegram client disconnected")
