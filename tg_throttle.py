#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-caught, global-statement, missing-function-docstring, missing-class-docstring
# pylint: disable=logging-fstring-interpolation, line-too-long

import os
import time
import asyncio
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


def _parse_int_env(name: str, default: int, minimum: int) -> int:
    """Parse an int env var, falling back to default on missing/invalid/out-of-range value."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"tg_throttle: {name} is not a valid integer ({raw!r}); using default {default}")
        return default
    if value < minimum:
        logger.warning(f"tg_throttle: {name}={value} below minimum {minimum}; using default {default}")
        return default
    return value


# Global throttle for live Telegram MTProto RPC calls. Serializes bursts (e.g. miniflux
# batching ~47 feeds at once) so they do not trip Telegram's FLOOD_WAIT (420).
_CONCURRENCY = _parse_int_env("TG_RPC_CONCURRENCY", 1, 1)               # max concurrent Telegram RPCs
_MIN_INTERVAL = _parse_int_env("TG_RPC_MIN_INTERVAL_MS", 500, 0) / 1000.0  # min gap between RPC starts (seconds)

_sem = asyncio.Semaphore(_CONCURRENCY)
_lock = asyncio.Lock()
_last_start = 0.0

logger.info(f"tg_throttle: initialized (concurrency={_CONCURRENCY}, min_interval={_MIN_INTERVAL*1000:.0f}ms)")


class _TgRpcGate:
    """Async context manager that caps concurrency and enforces a minimum spacing between RPC starts."""

    async def __aenter__(self):
        await _sem.acquire()
        global _last_start
        try:
            async with _lock:
                wait = _last_start + _MIN_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                _last_start = time.monotonic()
        except BaseException:
            # Do not leak a semaphore permit if cancelled while waiting for the spacing.
            _sem.release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        _sem.release()
        return False


def tg_rpc():
    """Return an async context manager that throttles a single live Telegram RPC call."""
    return _TgRpcGate()


@asynccontextmanager
async def tg_rpc_bounded(timeout: float):
    """Throttle a live Telegram RPC AND bound it with a timeout, correctly nested.

    The single tricky invariant this centralizes: the timeout must bound ONLY the
    RPC body, never the gate ENTRY — timing out the `_sem.acquire()` / spacing wait
    would turn legitimate queue backpressure (e.g. ~47 feeds queueing) into false
    timeouts. So the gate is the OUTER context and the timeout is the INNER one; a
    TimeoutError raised inside propagates out through the gate's `__aexit__`, which
    releases the permit (no leak). Call as:

        async with tg_rpc_bounded(Config["tg_rpc_timeout"]):
            result = await client.get_chat(channel_id)

    Every gated+bounded RPC uses this so no call site re-derives the nesting by hand
    (getting it wrong silently reopens the hang-under-backpressure class).
    """
    async with _TgRpcGate():
        async with asyncio.timeout(timeout):
            yield
