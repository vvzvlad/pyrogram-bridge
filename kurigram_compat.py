#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-caught, logging-fstring-interpolation, line-too-long
# pylint: disable=missing-function-docstring

"""A single wrapper over Kurigram's ``RichMessage._parse`` (Rich Messages epic, #83 / #84).

Kurigram 2.2.26 (MTProto layer 229) parses ``message.rich_message``, and ``Message._parse``
calls ``RichMessage._parse`` UNCONDITIONALLY for every message
(pyrogram/types/messages_and_media/message.py:1777), guarding nothing. This module wraps
that one call. It does two jobs:

  * recovers the ``part`` flag — LOAD-BEARING, not defensive. The high-level type drops it
    (rich_message.py keeps only ``blocks``/``is_rtl``), and phase 3 (#86) needs it to know
    which posts to re-fetch in full. Nothing else can recover it after the parse.
  * fails soft on the whole message. Anything raising under the parse (a block, the vector
    comprehensions, ``.rtl``) would otherwise take down the entire get_messages/history
    call. Instead we emit a ``part=True`` sentinel with empty blocks, which the phase-3
    re-fetch recognises and REPAIRS.

HISTORY — removed in the 2.2.24 -> 2.2.26 bump: a second wrapper over ``RichBlock._parse``
degraded ONE bad block to a marked ``RichBlockUnsupported`` node instead of losing the post.
It existed for two deterministic upstream bugs (a missing document dereferenced without a
None-guard; ``RichBlockListItem._parse`` recursing without forwarding photos/documents).
2.2.26 fixed BOTH — every media branch now returns ``RichBlockUnsupported()`` on a missing
document (rich_block.py:290, 331, 347) and the list-item recursion forwards the full
argument set (rich_block.py:571, 596) — so the wrapper had nothing left to catch. A block
that raises now degrades the whole post to the sentinel above rather than a single block:
less granular, still never a crash. Restore the block contour from git history if a future
layer reintroduces a per-block upstream crash.

Rollback safety: on a Kurigram build that lacks the Rich* classes the import degrades to a
no-op — the container must not crash-loop. The wrapper uses ``except Exception`` (NOT
BaseException — never swallow CancelledError).
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
_rich_msg_parse_failed = 0    # whole-message parse failures (ERROR)
_rich_part_seen = 0           # partial rich messages observed (WARNING) — phase-3 signal


def _incr_msg_parse_failed() -> None:
    global _rich_msg_parse_failed
    with _counter_lock:
        _rich_msg_parse_failed += 1


def _incr_part_seen() -> None:
    global _rich_part_seen
    with _counter_lock:
        _rich_part_seen += 1


def get_rich_msg_parse_failed_count() -> int:
    """Whole-message rich parse failures since process start (sentinel emitted)."""
    with _counter_lock:
        return _rich_msg_parse_failed


def get_rich_part_seen_count() -> int:
    """Partial (``part=True``) rich messages seen since process start.

    Lives in production PERMANENTLY (not just on the test stand): a growing value is the
    signal to (re)open phase 3 — enrichment of partial rich content via GetRichMessage.
    """
    with _counter_lock:
        return _rich_part_seen


def reset_counters() -> None:
    """Reset all counters. For test isolation only — not used in production."""
    global _rich_msg_parse_failed, _rich_part_seen
    with _counter_lock:
        _rich_msg_parse_failed = 0
        _rich_part_seen = 0


# --- Bind the Kurigram Rich* classes, degrading to a no-op if they are absent -----------
try:
    from pyrogram import raw as _raw
    from pyrogram import types as _types

    _RichMessage = _types.RichMessage
    _RawRichMessage = _raw.types.RichMessage
    # Captured BEFORE patching so the wrapper always delegates to the genuine original
    # (re-installing is therefore idempotent — a wrapper never wraps a wrapper).
    _orig_richmessage_parse = _RichMessage._parse
    _RICH_AVAILABLE = True
except (ImportError, AttributeError):  # pragma: no cover - exercised via monkeypatch in tests
    _RICH_AVAILABLE = False

_installed = False


async def _wrapped_richmessage_parse(client, rich_message=None, users=None, chats=None):
    """message-contour wrapper for ``RichMessage._parse`` (async staticmethod)."""
    # None-passthrough (LOAD-BEARING): Message._parse calls RichMessage._parse
    # UNCONDITIONALLY for every message (message.py:1777), and an ordinary post passes
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
    """True if this rich message is the wrapper's failed-parse sentinel.

    Used by the phase 2/3 download path to decide 503-transient vs 404-permanent for
    missing rich media. Only the message contour sets ``parse_failed``, and only on the
    top-level object, so this is a flat check — the recursive block walk went with the
    block contour (see the module docstring).
    """
    if rich_message is None:
        return False
    return bool(getattr(rich_message, "parse_failed", False))


def install() -> bool:
    """Install the RichMessage._parse wrapper. Idempotent; no-op on a non-rich Kurigram.

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
    # Re-patch through staticmethod(...) — RichMessage._parse is a staticmethod.
    _RichMessage._parse = staticmethod(_wrapped_richmessage_parse)
    _installed = True
    logger.info("kurigram_compat: rich parse wrapper installed (message contour)")
    return True
