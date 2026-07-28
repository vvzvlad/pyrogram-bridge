#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-caught, logging-fstring-interpolation, line-too-long
# pylint: disable=missing-function-docstring

"""Defensive monkey-patches over Kurigram's Rich* message parsers (phase 1 of the
Rich Messages epic, issue #83 / #84).

WORKAROUND(kurigram-richblock-none)
-----------------------------------
Kurigram 2.2.24 (MTProto layer 227) parses ``message.rich_message`` but ships two
deterministic upstream bugs that raise *inside* ``RichBlock._parse`` and crash the
ENTIRE message parse, because ``Message._parse`` does not guard the call
(pyrogram/types/messages_and_media/message.py:1679):

  1. ``documents.get(...)`` returns ``None`` for a missing video/audio document and the
     result is dereferenced (``doc.attributes``) without a None-guard
     (pyrogram/types/messages_and_media/rich_block.py:243-244 video, 280-281 audio).
  2. ``RichBlockListItem._parse`` recurses via ``types.RichBlock._parse(client, block)``
     WITHOUT forwarding the ``photos``/``documents`` dicts (rich_block.py:475, 499), so
     any video/audio inside a list item raises ``AttributeError`` unconditionally and any
     inline photo silently degrades to ``RichBlockPhoto(photo=None)``.

Verified byte-for-byte against the installed wheel ``Kurigram==2.2.24``; rich_block.py is
identical to upstream dev HEAD sha 793ef246. Upstream bug report: <link once filed>.
These workarounds are removable once the upstream fix lands.

Two contours, both mandatory (they play DIFFERENT roles — not interchangeable):

  * message-contour — wraps ``RichMessage._parse``. Catches failures OUTSIDE any single
    block (the vector comprehensions, ``.rtl``) and is the last line of defence. Also
    recovers the ``part`` flag (dropped by the high-level type — rich_message.py only keeps
    ``blocks``/``is_rtl``) by re-attaching it to the parsed object, and is where None
    passes straight through (every ordinary post calls RichMessage._parse with None).

  * block-contour — wraps ``RichBlock._parse``. Degrades ONE bad block to a marked
    ``RichBlockUnsupported`` node so the rest of the post survives. Recursion is covered
    for free: containers and list items call ``types.RichBlock._parse`` by class name
    (rich_block.py:175, 199, 211, 226, 475, 499), so the patched name is hit nested,
    including the video-in-list crash above.

Rollback safety: on a Kurigram build that lacks the Rich* classes (e.g. a downgrade to
2.2.23) the import degrades to a no-op — the container must not crash-loop. Both wrappers
use ``except Exception`` (NOT BaseException — never swallow CancelledError).
"""

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# --- Degradation counters (mirrors rss_generator._render_failed_count → /health) --------
# Process-wide, guarded by a lock: parsing runs inside the client's asyncio loop and feeds
# may parse concurrently. Surfaced read-only via the get_*_count() accessors, exported to
# /health so a silent rich-parse regression becomes observable to the operator.
_counter_lock = threading.Lock()
_rich_msg_parse_failed = 0    # whole-message parse failures (ERROR) — message-contour
_rich_block_parse_failed = 0  # single-block parse failures (WARNING) — block-contour
_rich_part_seen = 0           # partial rich messages observed (WARNING) — phase-3 signal


def _incr_msg_parse_failed() -> None:
    global _rich_msg_parse_failed
    with _counter_lock:
        _rich_msg_parse_failed += 1


def _incr_block_parse_failed() -> None:
    global _rich_block_parse_failed
    with _counter_lock:
        _rich_block_parse_failed += 1


def _incr_part_seen() -> None:
    global _rich_part_seen
    with _counter_lock:
        _rich_part_seen += 1


def get_rich_msg_parse_failed_count() -> int:
    """Whole-message rich parse failures since process start (sentinel emitted)."""
    with _counter_lock:
        return _rich_msg_parse_failed


def get_rich_block_parse_failed_count() -> int:
    """Single-block rich parse failures since process start (unsupported node emitted)."""
    with _counter_lock:
        return _rich_block_parse_failed


def get_rich_part_seen_count() -> int:
    """Partial (``part=True``) rich messages seen since process start.

    Lives in production PERMANENTLY (not just on the test stand): a growing value is the
    signal to (re)open phase 3 — enrichment of partial rich content via GetRichMessage.
    """
    with _counter_lock:
        return _rich_part_seen


def reset_counters() -> None:
    """Reset all counters. For test isolation only — not used in production."""
    global _rich_msg_parse_failed, _rich_block_parse_failed, _rich_part_seen
    with _counter_lock:
        _rich_msg_parse_failed = 0
        _rich_block_parse_failed = 0
        _rich_part_seen = 0


# --- Bind the Kurigram Rich* classes, degrading to a no-op if they are absent -----------
try:
    from pyrogram import raw as _raw
    from pyrogram import types as _types

    _RichMessage = _types.RichMessage
    _RichBlock = _types.RichBlock
    _RichBlockUnsupported = _types.RichBlockUnsupported
    _RawRichMessage = _raw.types.RichMessage
    # Captured BEFORE patching so the wrappers always delegate to the genuine originals
    # (re-installing is therefore idempotent — a wrapper never wraps a wrapper).
    _orig_richmessage_parse = _RichMessage._parse
    _orig_richblock_parse = _RichBlock._parse
    _RICH_AVAILABLE = True
except (ImportError, AttributeError):  # pragma: no cover - exercised via monkeypatch in tests
    _RICH_AVAILABLE = False

_installed = False


async def _wrapped_richmessage_parse(client, rich_message=None, users=None, chats=None):
    """message-contour wrapper for ``RichMessage._parse`` (async staticmethod)."""
    # None-passthrough (LOAD-BEARING): Message._parse calls RichMessage._parse
    # UNCONDITIONALLY for every message (message.py:1679), and an ordinary post passes
    # rich_message=None. Delegate verbatim so a normal post never enters the stats/part
    # path below. Without this early return the sentinel/degradation path would flood
    # every feed item. (Do NOT fold this into the try: the setattr below assumes a
    # non-None parsed object, guaranteed only once None is filtered out here.)
    if rich_message is None:
        return await _orig_richmessage_parse(client, rich_message, _d(users), _d(chats))

    try:
        parsed = await _orig_richmessage_parse(client, rich_message, _d(users), _d(chats))
    except Exception as e:
        # Failure outside any single block (vector comprehension / .rtl). Emit a sentinel
        # so the whole get_messages/history call survives. part=True + empty blocks lets
        # phase 3 recognise and REPAIR such posts via a re-fetch.
        _incr_msg_parse_failed()
        part = bool(getattr(rich_message, "part", None))
        # Best-effort attribution: _parse's signature carries no channel/id; surface what
        # the chats argument offers.
        logger.error(
            f"rich_msg_parse_failed: {type(e).__name__}: {e} "
            f"(part={part}, chats={_describe_chats(chats)})"
        )
        sentinel = _RichMessage(blocks=[])
        setattr(sentinel, "parse_failed", True)
        setattr(sentinel, "part", part)
        return sentinel

    # Defensive: today a non-None raw always yields a real RichMessage (the single
    # raw.base.RichMessage constructor passes the upstream isinstance gate). But if a future
    # layer adds a second RichMessage constructor the gate rejects, _orig returns None and the
    # setattr below would raise the exact uncaught AttributeError this wrapper exists to prevent.
    if parsed is None:
        return parsed

    # Success. rich_message is a genuine (non-None) raw object here, so parsed is a real
    # RichMessage — recover the lost `part` flag and record raw vector stats.
    part = bool(getattr(rich_message, "part", None))
    photos = getattr(rich_message, "photos", None) or []
    documents = getattr(rich_message, "documents", None) or []
    # Object.default serialises non-underscore, non-None __dict__ attrs → `part` becomes
    # visible in /raw_json (there is no __slots__ on Object).
    setattr(parsed, "part", part)
    logger.info(
        f"rich_raw_stats: part={part} photos={len(photos)} documents={len(documents)}"
    )
    if part:
        _incr_part_seen()
        logger.warning(
            "rich_part_seen: a partial rich message was received (part=True) — "
            "phase-3 enrichment territory"
        )
    return parsed


async def _wrapped_richblock_parse(client, rich_block=None, *args, **kwargs):
    """block-contour wrapper for ``RichBlock._parse`` (async staticmethod).

    ``*args``/``**kwargs`` forward photos/documents/part/users/chats verbatim, preserving
    the recursion contract so nested container/list-item parses hit this wrapper too.
    """
    try:
        return await _orig_richblock_parse(client, rich_block, *args, **kwargs)
    except Exception as e:
        # One bad block degrades to a single marked node; the rest of the post is intact.
        # An exception raised in a list ITEM's own parse degrades the whole list to one
        # node — acceptable (the alternative is losing the entire post).
        _incr_block_parse_failed()
        logger.warning(f"rich_block_parse_failed: {type(e).__name__}: {e}")
        node = _RichBlockUnsupported()
        # parse_failed distinguishes OUR failure node from the honest upstream fallthrough
        # for not-yet-implemented block types (rich_block.py:315 returns a bare
        # RichBlockUnsupported()). Object.default serialises it into /raw_json.
        setattr(node, "parse_failed", True)
        return node


def _d(value):
    """Default empty-dict for the optional users/chats args (mirrors upstream defaults)."""
    return {} if value is None else value


def _describe_chats(chats: Any) -> str:
    try:
        if not chats:
            return "none"
        return ",".join(str(k) for k in list(chats.keys())[:5])
    except Exception:
        return "unknown"


def has_parse_failures(rich_message) -> bool:
    """True if any node in the rich message carries the ``parse_failed`` marker.

    Recursive over the block tree (containers/list-items/table-cells). Used by the phase
    2/3 download path to decide 503-transient vs 404-permanent for missing rich media.
    """
    if rich_message is None:
        return False
    if getattr(rich_message, "parse_failed", False):
        return True
    return _walk_has_failure(getattr(rich_message, "blocks", None))


# Container attributes that hold child blocks (rich_block.py __init__ signatures):
# blocks (ListItem/BlockQuotation/Collage/Slideshow/Details), items (RichBlockList),
# cells (RichBlockTable — a list OF lists of cells).
_CHILD_BLOCK_ATTRS = ("blocks", "items", "cells")


def _walk_has_failure(node) -> bool:
    if node is None:
        return False
    if isinstance(node, (list, tuple)):
        return any(_walk_has_failure(x) for x in node)
    if getattr(node, "parse_failed", False):
        return True
    for attr in _CHILD_BLOCK_ATTRS:
        child = getattr(node, attr, None)
        if child and _walk_has_failure(child):
            return True
    return False


def install() -> bool:
    """Install the two defensive wrappers. Idempotent; no-op on a non-rich Kurigram.

    Called at import time from telegram_client.py BEFORE the Client is created. Returns
    True if the wrappers were installed, False on the no-op (rollback) path.
    """
    global _installed
    if not _RICH_AVAILABLE:
        # Rollback to a Kurigram without Rich* classes must NOT crash-loop the container.
        logger.info(
            "rich support inactive: installed Kurigram lacks Rich* classes; "
            "kurigram_compat is a no-op"
        )
        return False
    if _installed:
        return True
    # Re-patch through staticmethod(...) — RichMessage/RichBlock._parse are staticmethods.
    _RichMessage._parse = staticmethod(_wrapped_richmessage_parse)
    _RichBlock._parse = staticmethod(_wrapped_richblock_parse)
    _installed = True
    logger.info("kurigram_compat: rich parse wrappers installed (message + block contours)")
    return True
