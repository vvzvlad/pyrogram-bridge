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
import time
import uvloop
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
        self.last_disconnect_time = 0
        self.disconnect_window = 60  # Reset counter if disconnects are more than 60 seconds apart
        self.reconnect_delay = 5  # Wait 5 seconds before reconnecting
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

    async def _on_disconnect(self, _client):
        """Handles disconnection from Telegram servers with auto-reconnect"""
        current_time = time.time()
        
        # Reset counter if disconnects are far apart
        if self.last_disconnect_time > 0 and current_time - self.last_disconnect_time > self.disconnect_window:
            logger.info(f"connection_handler: resetting disconnect counter (last disconnect was {current_time - self.last_disconnect_time:.1f}s ago)")
            self.disconnect_count = 0
            
        self.last_disconnect_time = current_time
        self.disconnect_count += 1
        
        logger.warning(f"connection_handler: connection lost (#{self.disconnect_count})")
        
        if self.disconnect_count >= self.max_disconnects:
            logger.critical(f"connection_handler: reached disconnect limit ({self.max_disconnects}) within {self.disconnect_window}s window, terminating")
            # Explicitly stop the client before exit
            if self.client.is_connected:
                await self.client.stop()
            # Exit with error code
            sys.exit(1)
            
        # Attempt to reconnect only if we haven't reached max_disconnects
        logger.info(f"connection_handler: attempting reconnection in {self.reconnect_delay}s...")
        await asyncio.sleep(self.reconnect_delay)
        
        try:
            if not self.client.is_connected:
                await self.client.start()
                logger.info("connection_handler: reconnection successful")
        except Exception as e:
            logger.error(f"connection_handler: reconnection failed: {str(e)}")

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
