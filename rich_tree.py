#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-caught, logging-fstring-interpolation, line-too-long
# pylint: disable=missing-function-docstring, missing-class-docstring, too-many-return-statements
# pylint: disable=too-many-branches, too-many-statements

"""Normalised rich-content tree (phase 2 of the Rich Messages epic, #83 / #85).

The rich tree is the SINGLE canonical representation of a rich message inside the
bridge: a plain, JSON-serialisable dict that every consumer (HTML render, title,
flags, media collection, snapshot) reads through :func:`tree_of`. Nothing outside
this module knows the Kurigram ``Rich*`` classes.

Shape (``SCHEMA_V`` versions the schema)::

    {"v": 1, "rtl": bool, "part": bool, "blocks": [node, ...]}

INVARIANT: bumping ``SCHEMA_V`` REQUIRES bumping ``message_snapshot.SNAPSHOT_VERSION``
(a stored tree with an older ``v`` must invalidate the snapshot). See the comment at
``SNAPSHOT_VERSION``.

Design guarantees:
  * :func:`tree_of` is the ONLY entry point; the result (including ``None``) is memoised
    on the message object under a ``_``-prefixed attribute, so a live post and its
    snapshot see the exact same tree — live/cache render parity is structural.
  * :func:`from_pyrogram` is fail-soft PER BLOCK: a block that raises becomes a
    ``{"t": "unsupported"}`` node (+ a WARNING and a /health counter), never a crash.
    The live ``parse_failed`` marker (block-contour, #84) is NOT written into the tree —
    it too degrades to ``{"t": "unsupported"}`` — so :func:`render_html` reads ONLY the
    tree and never a live object's attributes.
  * :func:`render_html` is PURE: it reads the tree, html.escape()s every user string,
    and only ever emits ``href`` values from the url_builder, validated ``str`` URLs, or
    ``#rich-…`` fragments.
"""

import logging
import re
import threading
from datetime import datetime
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

# Bumping this REQUIRES bumping message_snapshot.SNAPSHOT_VERSION (see module docstring).
SCHEMA_V = 1

# Recursion / size guards (policy constants, #83 R7).
MAX_RICH_DEPTH = 20      # real nesting is <=5; deeper -> placeholder node (bomb guard)
MAX_RICH_NODES = 2000    # table cells COUNT as nodes; over the limit -> truncation
MAX_SPAN = 20            # colspan/rowspan clamp — a layout bomb never reaches the reader

# --- Degradation counter (mirrors kurigram_compat / rss_generator -> /health) ----------
_counter_lock = threading.Lock()
_rich_block_adapt_failed = 0  # adapter per-block failures (WARNING) -> fail-soft unsupported node


def _incr_adapt_failed() -> None:
    global _rich_block_adapt_failed
    with _counter_lock:
        _rich_block_adapt_failed += 1


def get_rich_block_adapt_failed_count() -> int:
    """Adapter per-block failures since process start (unsupported node emitted)."""
    with _counter_lock:
        return _rich_block_adapt_failed


def reset_counters() -> None:
    """Reset the adapter counter. Test isolation only."""
    global _rich_block_adapt_failed
    with _counter_lock:
        _rich_block_adapt_failed = 0


# ======================================================================================
# tree_of — the single entry point (memoised on the message object)
# ======================================================================================
# Two `_`-prefixed attributes carry the memo. The prefix hides them from
# Object.default / str(message) serialisation (Object.default skips leading-underscore
# keys). A SEPARATE "computed" flag is needed because the tree itself can legitimately
# be None (a non-rich post), which must be memoised too (not recomputed every gate call).
_MEMO_VALUE = "_rich_tree_cached"
_MEMO_DONE = "_rich_tree_computed"


def tree_of(message: Any) -> Optional[dict]:
    """Return the rich tree for a message, or None if it is not a rich post.

    THE ONLY way any consumer obtains a tree. A CachedMessage carries the ready tree in
    ``message.rich_tree`` (stored by the snapshot); a live Message is adapted from
    ``message.rich_message`` via :func:`from_pyrogram`. The result — including None — is
    memoised on the object so the render, the title, the flags, the media collection and
    the snapshot all observe ONE identical tree.
    """
    if message is None:
        return None
    if getattr(message, _MEMO_DONE, False):
        return getattr(message, _MEMO_VALUE, None)
    tree = _compute_tree(message)
    # Some objects (e.g. spec'd mocks) may reject setattr — degrade to recompute.
    try:
        setattr(message, _MEMO_VALUE, tree)
        setattr(message, _MEMO_DONE, True)
    except Exception:
        pass
    return tree


def _compute_tree(message: Any) -> Optional[dict]:
    # CachedMessage path: the tree was serialised at snapshot time.
    stored = getattr(message, "rich_tree", None)
    if stored is not None:
        return stored
    # Live path: adapt the Kurigram RichMessage (None for every ordinary post).
    rich_message = getattr(message, "rich_message", None)
    if rich_message is None:
        return None
    try:
        return from_pyrogram(rich_message)
    except Exception as e:
        # Defensive last resort: the message-contour already guarantees a valid
        # RichMessage/sentinel and every block is guarded inside from_pyrogram, so this
        # should be unreachable. Degrade to None rather than crash a gate.
        logger.error(f"rich_tree_build_failed: {type(e).__name__}: {e}")
        return None


# ======================================================================================
# from_pyrogram — the adapter (the ONLY place that knows Kurigram Rich* classes)
# ======================================================================================
class _Budget:
    """Node budget shared across the whole tree walk (table cells and list items count)."""

    def __init__(self) -> None:
        self.count = 0
        self.truncated = False

    def take(self) -> bool:
        """Consume one node slot. Returns True if the budget is exhausted (skip the node)."""
        if self.count >= MAX_RICH_NODES:
            self.truncated = True
            return True
        self.count += 1
        return False


def from_pyrogram(rich_message: Any) -> dict:
    """Adapt a live Kurigram ``types.RichMessage`` into the canonical tree dict."""
    budget = _Budget()
    blocks = _adapt_blocks(getattr(rich_message, "blocks", None), 0, budget)
    if budget.truncated:
        # One "content truncated" node at the very end keeps a long-read partially useful.
        blocks.append({"t": "truncated"})
        logger.warning(f"rich_tree_truncated: node budget {MAX_RICH_NODES} exceeded")
    return {
        "v": SCHEMA_V,
        "rtl": bool(getattr(rich_message, "is_rtl", False)),
        "part": bool(getattr(rich_message, "part", False)),
        "blocks": blocks,
    }


def _adapt_blocks(blocks: Any, depth: int, budget: _Budget) -> list:
    out = []
    for block in (blocks or []):
        if block is None:
            continue
        node = _adapt_block(block, depth, budget)
        if node is not None:
            out.append(node)
    return out


def _adapt_block(obj: Any, depth: int, budget: _Budget) -> Optional[dict]:
    """Adapt ONE block. Returns a node dict, or None to skip it (budget exhausted)."""
    if budget.take():
        return None
    if depth > MAX_RICH_DEPTH:
        return {"t": "unsupported"}
    name = type(obj).__name__
    fn = _BLOCK_DISPATCH.get(name)
    try:
        if fn is None:
            # RichBlockUnsupported, RichBlockThinking, and any not-yet-mapped / future type.
            return {"t": "unsupported"}
        return fn(obj, depth, budget)
    except Exception as e:
        # Fail-soft: one bad block never crashes the post.
        _incr_adapt_failed()
        logger.warning(f"rich_block_adapt_failed: {type(e).__name__}: {e}")
        return {"t": "unsupported"}


def _adapt_heading(obj, depth, budget):
    size = getattr(obj, "size", 1)
    return {"t": "heading", "size": int(size) if isinstance(size, int) else 1,
            "text": _adapt_rt(getattr(obj, "text", None), budget)}


def _adapt_paragraph(obj, depth, budget):
    return {"t": "paragraph", "text": _adapt_rt(getattr(obj, "text", None), budget)}


def _adapt_pre(obj, depth, budget):
    lang = getattr(obj, "language", None)
    return {"t": "pre", "language": lang if isinstance(lang, str) else None,
            "text": _adapt_rt(getattr(obj, "text", None), budget)}


def _adapt_footer(obj, depth, budget):
    return {"t": "footer", "text": _adapt_rt(getattr(obj, "text", None), budget)}


def _adapt_divider(obj, depth, budget):
    return {"t": "divider"}


def _adapt_math(obj, depth, budget):
    return {"t": "math", "expr": str(getattr(obj, "expression", "") or "")}


def _adapt_anchor(obj, depth, budget):
    return {"t": "anchor", "name": str(getattr(obj, "name", "") or "")}


def _adapt_list(obj, depth, budget):
    items = []
    for raw in (getattr(obj, "items", None) or []):
        # Upstream returns None for unknown raw item types (rich_block.py:528-529) — skip.
        if raw is None:
            continue
        if budget.take():
            break
        items.append({
            "label": str(getattr(raw, "label", "") or ""),
            "blocks": _adapt_blocks(getattr(raw, "blocks", None), depth + 1, budget),
            "checkbox": bool(getattr(raw, "has_checkbox", False)),
            "checked": bool(getattr(raw, "is_checked", False)),
            "value": getattr(raw, "value", None),
            "type": getattr(raw, "type", None),
        })
    if not items:
        # Empty list -> no block emitted at all.
        return None
    return {"t": "list", "items": items}


def _adapt_blockquote(obj, depth, budget):
    return {"t": "blockquote",
            "blocks": _adapt_blocks(getattr(obj, "blocks", None), depth + 1, budget),
            "credit": _adapt_rt(getattr(obj, "credit", None), budget)}


def _adapt_pullquote(obj, depth, budget):
    return {"t": "pullquote",
            "text": _adapt_rt(getattr(obj, "text", None), budget),
            "credit": _adapt_rt(getattr(obj, "credit", None), budget)}


def _adapt_collage(obj, depth, budget):
    node = {"t": "collage", "blocks": _adapt_blocks(getattr(obj, "blocks", None), depth + 1, budget)}
    _attach_caption(node, getattr(obj, "caption", None), budget)
    return node


def _adapt_slideshow(obj, depth, budget):
    node = {"t": "slideshow", "blocks": _adapt_blocks(getattr(obj, "blocks", None), depth + 1, budget)}
    _attach_caption(node, getattr(obj, "caption", None), budget)
    return node


def _adapt_table(obj, depth, budget):
    rows = []
    for raw_row in (getattr(obj, "cells", None) or []):
        # Charge one slot PER ROW (not only per cell): sparse rows (cells=[[],[],…]) would
        # otherwise run the inner loop zero times, never call budget.take(), and let millions
        # of empty rows through with truncated=False (DoS cap, review F1).
        if budget.take():
            break
        row = []
        for raw_cell in (raw_row or []):
            if budget.take():
                break
            row.append({
                "text": _adapt_rt(getattr(raw_cell, "text", None), budget),
                "header": bool(getattr(raw_cell, "is_header", False)),
                "colspan": getattr(raw_cell, "colspan", None),
                "rowspan": getattr(raw_cell, "rowspan", None),
            })
        rows.append(row)
        if budget.truncated:
            break
    # TRAP: RichBlockTable.caption is annotated RichBlockCaption but _parse stores the
    # RichText from the raw `title` there (rich_block.py:788-792) — map it as the table title.
    return {"t": "table",
            "title": _adapt_rt(getattr(obj, "caption", None), budget),
            "bordered": bool(getattr(obj, "is_bordered", False)),
            "striped": bool(getattr(obj, "is_striped", False)),
            "rows": rows}


def _adapt_details(obj, depth, budget):
    return {"t": "details",
            "summary": _adapt_rt(getattr(obj, "summary", None), budget),
            "blocks": _adapt_blocks(getattr(obj, "blocks", None), depth + 1, budget),
            "open": bool(getattr(obj, "is_open", False))}


def _adapt_map(obj, depth, budget):
    loc = getattr(obj, "location", None)
    node = {"t": "map",
            "lat": getattr(loc, "latitude", None),
            "lon": getattr(loc, "longitude", None)}
    _attach_caption(node, getattr(obj, "caption", None), budget)
    return node


def _adapt_media(block, attr, kind, budget):
    media = getattr(block, attr, None)
    fid = getattr(media, "file_unique_id", None) if media is not None else None
    # A media node without a non-empty str file_unique_id cannot be served through /media
    # (guards upstream RichBlockPhoto(photo=None)) — degrade to an unsupported node.
    if not (isinstance(fid, str) and fid):
        return {"t": "unsupported"}
    node = {"t": kind, "fid": fid, "size": getattr(media, "file_size", None)}
    if kind in ("audio", "voice"):
        node["mime"] = getattr(media, "mime_type", None)
    _attach_caption(node, getattr(block, "caption", None), budget)
    return node


def _attach_caption(node: dict, caption: Any, budget: _Budget) -> None:
    if caption is None:
        return
    node["caption"] = {"text": _adapt_rt(getattr(caption, "text", None), budget),
                       "credit": _adapt_rt(getattr(caption, "credit", None), budget)}


_BLOCK_DISPATCH: dict = {
    "RichBlockSectionHeading": _adapt_heading,
    "RichBlockParagraph": _adapt_paragraph,
    "RichBlockPreformatted": _adapt_pre,
    "RichBlockFooter": _adapt_footer,
    "RichBlockDivider": _adapt_divider,
    "RichBlockMathematicalExpression": _adapt_math,
    "RichBlockAnchor": _adapt_anchor,
    "RichBlockList": _adapt_list,
    "RichBlockBlockQuotation": _adapt_blockquote,
    "RichBlockPullQuotation": _adapt_pullquote,
    "RichBlockCollage": _adapt_collage,
    "RichBlockSlideshow": _adapt_slideshow,
    "RichBlockTable": _adapt_table,
    "RichBlockDetails": _adapt_details,
    "RichBlockMap": _adapt_map,
    "RichBlockPhoto": lambda o, d, b: _adapt_media(o, "photo", "photo", b),
    "RichBlockVideo": lambda o, d, b: _adapt_media(o, "video", "video", b),
    "RichBlockAnimation": lambda o, d, b: _adapt_media(o, "animation", "animation", b),
    "RichBlockAudio": lambda o, d, b: _adapt_media(o, "audio", "audio", b),
    "RichBlockVoiceNote": lambda o, d, b: _adapt_media(o, "voice_note", "voice", b),
}


# ======================================================================================
# RichText adaptation (str | list | RichText subclass -> JSON-safe RT node)
# ======================================================================================
# Simple formatting wrappers: {"t": <short>, "text": RT}.
_RT_SIMPLE = {
    "RichTextBold": "bold",
    "RichTextItalic": "italic",
    "RichTextUnderline": "underline",
    "RichTextStrikethrough": "strike",
    "RichTextSpoiler": "spoiler",
    "RichTextMarked": "marked",
    "RichTextCode": "code",
    "RichTextSubscript": "sub",
    "RichTextSuperscript": "sup",
}
# Types rendered as plain text: unwrap to their inner .text (mailto/tel are deliberately
# NOT in ALLOWED_PROTOCOLS, so email/phone stay text; hashtags/mentions carry their marker
# already inside .text).
_RT_PLAIN = {
    "RichTextEmailAddress", "RichTextPhoneNumber", "RichTextBankCardNumber",
    "RichTextMention", "RichTextHashtag", "RichTextCashtag", "RichTextBotCommand",
}


def _adapt_rt(rt: Any, budget: _Budget, depth: int = 0) -> Any:
    if rt is None:
        return None
    if depth > MAX_RICH_DEPTH:
        return ""
    # Normalise Str (a str subclass) and pyrogram's List to plain JSON types.
    if isinstance(rt, str):
        # A scalar leaf is NOT charged a node slot (only list ELEMENTS are — see below).
        return str(rt)
    if isinstance(rt, (list, tuple)):
        # RichText lists (TextConcat) are unbounded upstream, so each element is charged a
        # node slot. Without this a single paragraph/heading/cell carrying a huge RichText
        # list would balloon the tree past MAX_RICH_NODES with truncated=False, and that
        # bloated tree would persist in the snapshot and be re-rendered/re-sanitised on
        # every feed request (DoS cap, review F1). Once the budget is exhausted the list is
        # cut and from_pyrogram appends the single top-level truncated marker.
        out = []
        for x in rt:
            if budget.take():
                break
            out.append(_adapt_rt(x, budget, depth + 1))
        return out

    name = type(rt).__name__
    if name in _RT_SIMPLE:
        return {"t": _RT_SIMPLE[name], "text": _adapt_rt(getattr(rt, "text", None), budget, depth + 1)}
    if name in _RT_PLAIN:
        return _adapt_rt(getattr(rt, "text", None), budget, depth + 1)
    if name == "RichTextUrl":
        # TRAP: TextAutoUrl stores a parsed RichText in .url (rich_text.py:159-164); only a
        # genuine str becomes a link. Str is a str subclass, so the common case passes.
        url = getattr(rt, "url", None)
        return {"t": "url", "text": _adapt_rt(getattr(rt, "text", None), budget, depth + 1),
                "url": url if isinstance(url, str) else None}
    if name == "RichTextTextMention":
        user = getattr(rt, "user", None)
        return {"t": "text_mention", "text": _adapt_rt(getattr(rt, "text", None), budget, depth + 1),
                "username": getattr(user, "username", None)}
    if name == "RichTextCustomEmoji":
        # Rendered as its alternative (alt) text — no image.
        return str(getattr(rt, "alternative_text", "") or "")
    if name == "RichTextDateTime":
        date = getattr(rt, "date", None)
        iso = date.isoformat() if hasattr(date, "isoformat") else None
        return {"t": "datetime", "text": _adapt_rt(getattr(rt, "text", None), budget, depth + 1), "date": iso}
    if name == "RichTextMathematicalExpression":
        return {"t": "math", "expr": str(getattr(rt, "expression", "") or "")}
    if name == "RichTextAnchor":
        return {"t": "anchor", "name": str(getattr(rt, "name", "") or "")}
    if name == "RichTextReference":
        return {"t": "reference", "name": str(getattr(rt, "name", "") or ""),
                "text": _adapt_rt(getattr(rt, "text", None), budget, depth + 1)}
    if name == "RichTextAnchorLink":
        return {"t": "anchor_link", "name": str(getattr(rt, "anchor_name", "") or ""),
                "text": _adapt_rt(getattr(rt, "text", None), budget, depth + 1)}
    if name == "RichTextReferenceLink":
        return {"t": "reference_link", "name": str(getattr(rt, "reference_name", "") or ""),
                "text": _adapt_rt(getattr(rt, "text", None), budget, depth + 1)}
    # Unknown RichText type: recurse into .text if present, else empty.
    logger.debug(f"rich_block_unknown_type: {name}")
    inner = getattr(rt, "text", None)
    return _adapt_rt(inner, budget, depth + 1) if inner is not None else ""


# ======================================================================================
# render_html — pure tree -> HTML fragment
# ======================================================================================
import html as _html

_HEADING_TAG = {1: "h3", 2: "h4", 3: "h5"}  # >=4 -> h6 (h1/h2 clash with page/post titles)
_RT_TAG = {"bold": "b", "italic": "i", "underline": "u", "strike": "s", "spoiler": "s",
           "marked": "mark", "code": "code", "sub": "sub", "sup": "sup"}
_MEDIA_TYPES = frozenset({"photo", "video", "animation", "audio", "voice"})


def _anchor_id(name: Any) -> str:
    """`rich-`-prefixed, sanitised ([A-Za-z0-9_-]) anchor identifier (DOM-clobbering guard)."""
    return "rich-" + re.sub(r"[^A-Za-z0-9_-]", "", str(name or ""))


def _esc(value: Any) -> str:
    return _html.escape(str(value if value is not None else ""))


def render_html(tree: Optional[dict], url_builder: Callable[[str], Optional[str]]) -> str:
    """Render a rich tree to a sanitiser-ready HTML fragment. PURE: reads only the tree.

    ``url_builder(fid) -> Optional[str]`` builds a signed /media URL (None -> media node
    renders a placeholder). An empty ``blocks`` returns '' (the empty-tree placard is owned
    by _format_special_media). A tree with a foreign ``v`` renders a forward-compat placard.
    """
    if not isinstance(tree, dict):
        return ""
    if tree.get("v") != SCHEMA_V:
        return '<div class="rich-unsupported">Unsupported rich content version — open it in Telegram.</div>'
    blocks = tree.get("blocks") or []
    if not blocks:
        return ""
    return "\n".join(_render_block(b, url_builder) for b in blocks)


def _render_block(node: Any, url_builder) -> str:
    if not isinstance(node, dict):
        return ""
    t = node.get("t")
    if t == "heading":
        tag = _HEADING_TAG.get(node.get("size"), "h6")
        return f"<{tag}>{_render_rt(node.get('text'))}</{tag}>"
    if t == "paragraph":
        return f"<p>{_render_rt(node.get('text'))}</p>"
    if t == "pre":
        return f"<pre><code>{_render_rt(node.get('text'))}</code></pre>"
    if t == "footer":
        return f"<p><i>{_render_rt(node.get('text'))}</i></p>"
    if t == "divider":
        return "<hr>"
    if t == "math":
        return f"<pre>{_esc(node.get('expr'))}</pre>"
    if t == "anchor":
        return f'<span id="{_esc(_anchor_id(node.get("name")))}"></span>'
    if t == "list":
        return _render_list(node, url_builder)
    if t == "blockquote":
        inner = "".join(_render_block(b, url_builder) for b in (node.get("blocks") or []))
        return f"<blockquote>{inner}{_render_credit(node.get('credit'))}</blockquote>"
    if t == "pullquote":
        return f"<blockquote>{_render_rt(node.get('text'))}{_render_credit(node.get('credit'))}</blockquote>"
    if t in ("collage", "slideshow"):
        inner = "".join(_render_block(b, url_builder) for b in (node.get("blocks") or []))
        return inner + _render_caption(node.get("caption"))
    if t == "table":
        return _render_table(node)
    if t == "details":
        open_attr = " open" if node.get("open") else ""
        inner = "".join(_render_block(b, url_builder) for b in (node.get("blocks") or []))
        return f"<details{open_attr}><summary>{_render_rt(node.get('summary'))}</summary>{inner}</details>"
    if t == "map":
        return _render_map(node)
    if t in _MEDIA_TYPES:
        return _render_media(node, url_builder)
    if t == "truncated":
        return '<div class="rich-unsupported">Content truncated — open it in Telegram.</div>'
    if t == "unsupported":
        return '<div class="rich-unsupported">Unsupported block — open it in Telegram.</div>'
    return ""


def _render_credit(credit: Any) -> str:
    if credit is None:
        return ""
    rendered = _render_rt(credit)
    return f"<i>{rendered}</i>" if rendered else ""


def _render_caption(caption: Any) -> str:
    """Block caption -> <p><i>…</i></p>, always AFTER the block content."""
    if not isinstance(caption, dict):
        return ""
    text = _render_rt(caption.get("text"))
    credit = _render_rt(caption.get("credit"))
    if not text and not credit:
        return ""
    body = text
    if credit:
        body = f"{body} — {credit}" if body else credit
    return f"<p><i>{body}</i></p>"


def _render_list(node: dict, url_builder) -> str:
    items = node.get("items") or []
    if not items:
        return ""
    # Ordered iff ANY item carries a type (unordered items keep type=None upstream).
    ordered = any(it.get("type") is not None for it in items)
    if not ordered:
        return "<ul>" + "".join(_render_li(it, False, url_builder) for it in items) + "</ul>"
    # <ol> is honest only for a contiguous 1..N decimal run; anything else keeps the label.
    values = [it.get("value") for it in items]
    contiguous = (all(it.get("type") == "1" for it in items)
                  and all(isinstance(v, int) and not isinstance(v, bool) for v in values)
                  and values == list(range(1, len(values) + 1)))
    if contiguous:
        return "<ol>" + "".join(_render_li(it, False, url_builder) for it in items) + "</ol>"
    return "<ul>" + "".join(_render_li(it, True, url_builder) for it in items) + "</ul>"


def _render_li(item: dict, use_label: bool, url_builder) -> str:
    prefix = ""
    if item.get("checkbox"):
        prefix = "☑ " if item.get("checked") else "☐ "
    if use_label and item.get("label"):
        prefix += _esc(item.get("label")) + " "
    body = "".join(_render_block(b, url_builder) for b in (item.get("blocks") or []))
    return f"<li>{prefix}{body}</li>"


def _render_table(node: dict) -> str:
    parts = ["<table>"]
    title = _render_rt(node.get("title"))
    if title:
        parts.append(f"<caption>{title}</caption>")
    for row in (node.get("rows") or []):
        parts.append("<tr>")
        for cell in (row or []):
            tag = "th" if cell.get("header") else "td"
            attrs = ""
            for span_attr in ("colspan", "rowspan"):
                span = cell.get(span_attr)
                if isinstance(span, int) and not isinstance(span, bool) and span > 1:
                    clamped = min(span, MAX_SPAN)
                    if clamped != span:
                        logger.debug(f"rich_table_span_clamped: {span_attr} {span} -> {clamped}")
                    attrs += f' {span_attr}="{clamped}"'
            parts.append(f"<{tag}{attrs}>{_render_rt(cell.get('text'))}</{tag}>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _render_map(node: dict) -> str:
    lat = node.get("lat")
    lon = node.get("lon")
    caption = _render_caption(node.get("caption"))
    if isinstance(lat, (int, float)) and not isinstance(lat, bool) \
            and isinstance(lon, (int, float)) and not isinstance(lon, bool):
        osm = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}"
        link = f'<a href="{_html.escape(osm, quote=True)}">{lat:.5f}, {lon:.5f}</a>'
        return f'<div class="rich-map">📍 Location: {link}</div>{caption}'
    return f'<div class="rich-map">📍 Location</div>{caption}'


# Inline media styles — same sizing literals as post_parser's renderers (survive the
# sanitiser's 5-property style filter) so rich media matches the rest of the feed.
_STYLE_IMG = "max-width:100%; width:auto; height:auto; max-height:400px; object-fit:contain;"
_STYLE_VIDEO = "max-width:100%; width:auto; height:auto; max-height:400px;"
_STYLE_ANIM = "max-width:100%; width:auto; height:auto; max-height:400px; object-fit:contain;"
_STYLE_AUDIO = "width:100%; max-width:400px;"


def _render_media(node: dict, url_builder) -> str:
    fid = node.get("fid")
    url = url_builder(fid) if (isinstance(fid, str) and fid) else None
    if not url:
        body = '<div class="rich-unsupported">Media unavailable — open it in Telegram.</div>'
        return body + _render_caption(node.get("caption"))
    src = _html.escape(url, quote=True)
    t = node.get("t")
    if t == "photo":
        body = f'<img src="{src}" style="{_STYLE_IMG}">'
    elif t == "video":
        body = f'<video controls src="{src}" style="{_STYLE_VIDEO}"></video>'
    elif t == "animation":
        body = f'<video controls autoplay loop muted src="{src}" style="{_STYLE_ANIM}"></video>'
    else:  # audio / voice
        default_mime = "audio/ogg" if t == "voice" else "audio/mpeg"
        mime = node.get("mime")
        mime = mime if isinstance(mime, str) and mime else default_mime
        body = (f'<audio controls style="{_STYLE_AUDIO}">'
                f'<source src="{src}" type="{_html.escape(mime, quote=True)}"></audio>')
    return body + _render_caption(node.get("caption"))


def _render_rt(rt: Any) -> str:
    if rt is None:
        return ""
    if isinstance(rt, str):
        return _html.escape(rt)
    if isinstance(rt, (list, tuple)):
        return "".join(_render_rt(x) for x in rt)
    if not isinstance(rt, dict):
        return ""
    t = rt.get("t")
    inner = _render_rt(rt.get("text"))
    if t in _RT_TAG:
        tag = _RT_TAG[t]
        return f"<{tag}>{inner}</{tag}>"
    if t == "url":
        url = rt.get("url")
        if isinstance(url, str) and url:
            # Dangerous schemes (javascript:, data:) are filtered by nh3 at the boundary.
            return f'<a href="{_html.escape(url, quote=True)}">{inner or _html.escape(url)}</a>'
        return inner
    if t == "text_mention":
        username = rt.get("username")
        if username:
            return f'<a href="https://t.me/{_html.escape(str(username), quote=True)}">{inner}</a>'
        return inner
    if t == "datetime":
        iso = rt.get("date")
        if iso:
            try:
                return _html.escape(datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M"))
            except Exception:
                pass
        return inner
    if t == "math":
        return f"<code>{_esc(rt.get('expr'))}</code>"
    if t == "anchor":
        return f'<span id="{_esc(_anchor_id(rt.get("name")))}"></span>'
    if t == "reference":
        return f'<span id="{_esc(_anchor_id(rt.get("name")))}">{inner}</span>'
    if t in ("anchor_link", "reference_link"):
        return f'<a href="#{_esc(_anchor_id(rt.get("name")))}">{inner}</a>'
    return inner


# ======================================================================================
# Title extraction (pure — reads only the tree)
# ======================================================================================
def extract_title(tree: Optional[dict]) -> Optional[str]:
    """First heading, else first non-empty paragraph, flattened to plain text (or None)."""
    if not isinstance(tree, dict):
        return None
    blocks = tree.get("blocks") or []
    for wanted in ("heading", "paragraph"):
        for node in blocks:
            if isinstance(node, dict) and node.get("t") == wanted:
                text = _flatten_rt(node.get("text")).strip()
                if text:
                    return text
    return None


def _flatten_rt(rt: Any) -> str:
    if rt is None:
        return ""
    if isinstance(rt, str):
        return rt
    if isinstance(rt, (list, tuple)):
        return "".join(_flatten_rt(x) for x in rt)
    if isinstance(rt, dict):
        if rt.get("t") == "math":
            return str(rt.get("expr", "") or "")
        if "text" in rt:
            return _flatten_rt(rt.get("text"))
        return ""
    return ""


# ======================================================================================
# Media iterators
# ======================================================================================
def iter_tree_media(tree: Optional[dict]) -> Iterator[dict]:
    """Yield {"fid", "size", "kind"} for every media node in the tree (kind := node t)."""
    if not isinstance(tree, dict):
        return
    yield from _walk_tree_media(tree.get("blocks"))


def _walk_tree_media(blocks: Any) -> Iterator[dict]:
    for node in (blocks or []):
        if not isinstance(node, dict):
            continue
        t = node.get("t")
        if t in _MEDIA_TYPES:
            yield {"fid": node.get("fid"), "size": node.get("size"), "kind": t}
        if node.get("blocks"):
            yield from _walk_tree_media(node.get("blocks"))
        if t == "list":
            for item in (node.get("items") or []):
                yield from _walk_tree_media(item.get("blocks"))


# Media attributes carried by the live Kurigram media blocks.
_LIVE_MEDIA_ATTRS = ("photo", "video", "animation", "audio", "voice_note")
# Container attributes that hold nested live blocks.
_LIVE_CHILD_ATTRS = ("blocks", "items")


def iter_media_objects(rich_message: Any) -> Iterator[Any]:
    """Yield the LIVE Kurigram media objects (with file_id) — download path only.

    file_id is ephemeral and never written into the tree, so the download path walks the
    live blocks directly. Recurses through container blocks and list items.
    """
    if rich_message is None:
        return
    yield from _walk_live_objects(getattr(rich_message, "blocks", None), 0)


def _walk_live_objects(node: Any, depth: int) -> Iterator[Any]:
    if node is None or depth > MAX_RICH_DEPTH:
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk_live_objects(item, depth)
        return
    for attr in _LIVE_MEDIA_ATTRS:
        media = getattr(node, attr, None)
        if media is not None and isinstance(getattr(media, "file_unique_id", None), str):
            yield media
    for attr in _LIVE_CHILD_ATTRS:
        child = getattr(node, attr, None)
        if child is not None:
            yield from _walk_live_objects(child, depth + 1)
