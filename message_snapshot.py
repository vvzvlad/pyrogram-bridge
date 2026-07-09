#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylance: disable=reportMissingImports, reportMissingModuleSource

"""JSON snapshot / restore for pyrogram Message objects (issue #23).

The history cache no longer pickles live pyrogram objects. On write a plain JSON
snapshot is extracted from a Message via an explicit allowlist (schema v1); on read
a duck-typed CachedMessage is restored that is indistinguishable from a real Message
to the render pipeline (post_parser.py / rss_generator.py are NOT changed).
"""

import json
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any, List, Optional

from pyrogram.enums import MessageMediaType

logger = logging.getLogger(__name__)

# Bump when the snapshot schema changes in a backwards-incompatible way; a mismatch
# makes _load_entry treat the cache file as a miss (old files are simply re-fetched).
SNAPSHOT_VERSION = 1


class CachedStr(str):
    """A ``str`` subclass carrying a precomputed ``.html`` rendering.

    Mirrors pyrogram's ``Str`` (which exposes ``.html`` computed from entities). Here the
    HTML is computed once at snapshot time and stored, so the render pipeline can read
    ``message.text.html`` on a restored message exactly as on a live one. The instance
    ``__dict__`` (holding ``.html``) is preserved across deepcopy/pickle via ``__reduce__``.
    """

    html: str

    @classmethod
    def build(cls, plain: str, html: str) -> "CachedStr":
        obj = cls(plain)
        obj.html = html
        return obj

    def __reduce__(self):
        # Preserve the precomputed .html across copy.deepcopy() and pickle. Without this a
        # str subclass would be reconstructed as a bare str and lose the instance __dict__.
        return (CachedStr.build, (str(self), self.html))


def _unwrap_text(value: Any) -> Optional[str]:
    """Unwrap a styled-text container to a plain string.

    In kurigram 2.2.23 Poll.question and every PollOption.text are always FormattedText
    objects (``.text`` holds the string). The rule ``v.text if hasattr(v, 'text') else
    str(v)`` is correct for FormattedText, for a bare Str and for a plain string. Storing
    a FormattedText as-is would make json.dump raise TypeError, silently killing the cache
    for any channel that has polls.
    """
    if value is None:
        return None
    inner = value.text if hasattr(value, "text") else value
    return str(inner)


def _snapshot_str(value: Any) -> Optional[dict]:
    """Snapshot a text/caption value: {'plain', 'html'} or None if absent.

    ``.html`` is computed at write time (live pyrogram Str exposes it); if unavailable the
    html falls back to the plain text.
    """
    if value is None:
        return None
    plain = str(value)
    html = getattr(value, "html", None)
    if html is None:
        html = plain
    return {"plain": plain, "html": str(html)}


def _enum_name(value: Any) -> Any:
    """Return an enum member's ``.name`` (JSON-safe) or the value unchanged."""
    if value is None:
        return None
    return value.name if hasattr(value, "name") else value


def _snapshot_obj(obj: Any, keys: List[str]) -> Optional[dict]:
    """Snapshot the allowlisted ``keys`` off ``obj`` (getattr-only), or None if obj is None."""
    if obj is None:
        return None
    return {k: getattr(obj, k, None) for k in keys}


def _snapshot_chat(chat: Any) -> Optional[dict]:
    if chat is None:
        return None
    usernames = getattr(chat, "usernames", None)
    snap_usernames = None
    if usernames:
        snap_usernames = [
            {"username": getattr(u, "username", None), "active": getattr(u, "active", None)}
            for u in usernames
        ]
    return {
        "id": getattr(chat, "id", None),
        "username": getattr(chat, "username", None),
        "title": getattr(chat, "title", None),
        "usernames": snap_usernames,
    }


# forward_origin is the ONE object whose key SET mirrors the live object: the
# MessageOrigin* classes each carry a DIFFERENT set of attributes and
# _format_forward_info branches by hasattr (Case 1-5). Presence of a key in the
# snapshot must mirror presence of the attribute on the source object.
_FORWARD_ORIGIN_KEYS = ["type", "chat", "sender_user_name", "sender_user", "chat_id", "title"]


def _snapshot_forward_origin(fo: Any) -> Optional[dict]:
    if fo is None:
        return None
    result: dict = {}
    for key in _FORWARD_ORIGIN_KEYS:
        if not hasattr(fo, key):
            continue
        value = getattr(fo, key)
        if key == "type":
            result[key] = _enum_name(value)
        elif key == "chat":
            result[key] = _snapshot_obj(value, ["id", "title", "username"])
        elif key == "sender_user":
            result[key] = _snapshot_obj(value, ["first_name", "last_name", "username"])
        else:
            result[key] = value
    return result


def _snapshot_reactions(reactions: Any) -> Optional[list]:
    if reactions is None:
        return None
    rlist = getattr(reactions, "reactions", None)
    if rlist is None:
        return None
    out = []
    for r in rlist:
        cid = getattr(r, "custom_emoji_id", None)
        out.append({
            # Custom-emoji reactions carry no unicode emoji: emoji is null and the
            # custom_emoji_id is stored as a STRING = str(document_id).
            "emoji": None if cid is not None else getattr(r, "emoji", None),
            "custom_emoji_id": str(cid) if cid is not None else None,
            "count": getattr(r, "count", None),
            "is_paid": getattr(r, "is_paid", None),
        })
    return out


def _snapshot_poll(poll: Any) -> Optional[dict]:
    if poll is None:
        return None
    options = getattr(poll, "options", None)
    snap_options = None
    if options:
        snap_options = [{"text": _unwrap_text(getattr(o, "text", None))} for o in options]
    return {
        "question": _unwrap_text(getattr(poll, "question", None)),
        "options": snap_options,
    }


def _snapshot_web_page(wp: Any) -> Optional[dict]:
    if wp is None:
        return None
    return {
        "type": getattr(wp, "type", None),
        "url": getattr(wp, "url", None),
        "display_url": getattr(wp, "display_url", None),
        "site_name": getattr(wp, "site_name", None),
        "title": getattr(wp, "title", None),
        "description": getattr(wp, "description", None),
        "has_large_media": getattr(wp, "has_large_media", None),
        "photo": _snapshot_obj(getattr(wp, "photo", None), ["file_unique_id"]),
    }


def snapshot_message(message: Any) -> dict:
    """Extract a JSON-serializable snapshot (schema v1) from a pyrogram Message.

    Every field is read via getattr(..., None). See the module docstring / issue #23 for
    the field contract.
    """
    date = getattr(message, "date", None)
    media = getattr(message, "media", None)
    service = getattr(message, "service", None)
    return {
        "id": getattr(message, "id", None),
        # isoformat()/fromisoformat() preserve naive/aware exactly as pyrogram stores it.
        "date": date.isoformat() if date is not None else None,
        "text": _snapshot_str(getattr(message, "text", None)),
        "caption": _snapshot_str(getattr(message, "caption", None)),
        "media": media.name if media is not None else None,
        "service": service.name if service is not None else None,
        "media_group_id": getattr(message, "media_group_id", None),
        "views": getattr(message, "views", None),
        "show_caption_above_media": getattr(message, "show_caption_above_media", None),
        "reply_to_message_id": getattr(message, "reply_to_message_id", None),
        "empty": getattr(message, "empty", None),
        "chat": _snapshot_chat(getattr(message, "chat", None)),
        "sender_chat": _snapshot_obj(getattr(message, "sender_chat", None), ["id", "title", "username"]),
        "from_user": _snapshot_obj(getattr(message, "from_user", None), ["first_name", "last_name", "username"]),
        "forward_origin": _snapshot_forward_origin(getattr(message, "forward_origin", None)),
        "reactions": _snapshot_reactions(getattr(message, "reactions", None)),
        "poll": _snapshot_poll(getattr(message, "poll", None)),
        "web_page": _snapshot_web_page(getattr(message, "web_page", None)),
        "photo": _snapshot_obj(getattr(message, "photo", None), ["file_unique_id"]),
        "video": _snapshot_obj(getattr(message, "video", None), ["file_unique_id", "file_size"]),
        "document": _snapshot_obj(getattr(message, "document", None), ["file_unique_id", "mime_type"]),
        "audio": _snapshot_obj(getattr(message, "audio", None), ["file_unique_id", "mime_type"]),
        "voice": _snapshot_obj(getattr(message, "voice", None), ["file_unique_id", "mime_type"]),
        "video_note": _snapshot_obj(getattr(message, "video_note", None), ["file_unique_id"]),
        "animation": _snapshot_obj(getattr(message, "animation", None), ["file_unique_id"]),
        "sticker": _snapshot_obj(getattr(message, "sticker", None), ["file_unique_id", "emoji", "is_video"]),
    }


def _ns(d: Optional[dict], keys: List[str]) -> Optional[SimpleNamespace]:
    """Restore a nested object with the FULL schema key set (None-defaults).

    Live pyrogram objects always set every attribute in __init__, and the pipeline reads
    them directly (e.g. rss_generator ~131 message.chat.username in an except; post_parser
    ~1087 u.username / u.active without getattr). So restored objects must carry all keys.
    Returns None if ``d`` is None (the attribute was absent on the live object).
    """
    if d is None:
        return None
    ns = SimpleNamespace()
    for k in keys:
        setattr(ns, k, d.get(k))
    return ns


def _restore_chat(d: Optional[dict]) -> Optional[SimpleNamespace]:
    if d is None:
        return None
    usernames = d.get("usernames")
    restored_usernames = None
    if usernames is not None:
        restored_usernames = [_ns(u, ["username", "active"]) for u in usernames]
    return SimpleNamespace(
        id=d.get("id"),
        username=d.get("username"),
        title=d.get("title"),
        usernames=restored_usernames,
    )


def _restore_forward_origin(d: Optional[dict]) -> Optional[SimpleNamespace]:
    """Restore forward_origin mirroring ONLY the recorded keys (presence-semantics)."""
    if d is None:
        return None
    ns = SimpleNamespace()
    for key, value in d.items():
        if key == "chat":
            setattr(ns, key, _ns(value, ["id", "title", "username"]))
        elif key == "sender_user":
            setattr(ns, key, _ns(value, ["first_name", "last_name", "username"]))
        else:
            setattr(ns, key, value)
    return ns


def _restore_reactions(items: Optional[list]) -> Optional[SimpleNamespace]:
    if items is None:
        return None
    reactions = [_ns(r, ["emoji", "custom_emoji_id", "count", "is_paid"]) for r in items]
    # Mirror pyrogram: message.reactions is an object exposing a .reactions list.
    return SimpleNamespace(reactions=reactions)


def _restore_poll(d: Optional[dict]) -> Optional[SimpleNamespace]:
    if d is None:
        return None
    options = d.get("options") or []
    # options must be namespace objects with a .text string; a bare string would make
    # getattr(option, 'text', '') return '' and render empty options.
    restored_options = [SimpleNamespace(text=o.get("text")) for o in options]
    return SimpleNamespace(question=d.get("question"), options=restored_options)


def _restore_web_page(d: Optional[dict]) -> Optional[SimpleNamespace]:
    if d is None:
        return None
    return SimpleNamespace(
        type=d.get("type"),
        url=d.get("url"),
        display_url=d.get("display_url"),
        site_name=d.get("site_name"),
        title=d.get("title"),
        description=d.get("description"),
        has_large_media=d.get("has_large_media"),
        photo=_ns(d.get("photo"), ["file_unique_id"]),
    )


def _restore_media(name: Optional[str]) -> Optional[MessageMediaType]:
    if name is None:
        return None
    try:
        return MessageMediaType[name]
    except KeyError:
        # A media type unknown to this kurigram build: warn and drop to None so the 31
        # post_parser sites that compare against / index by the enum keep working.
        logger.warning(f"snapshot_unknown_media_type: {name}")
        return None


def _restore_str(d: Optional[dict]) -> Optional[CachedStr]:
    if d is None:
        return None
    return CachedStr.build(d.get("plain", ""), d.get("html", d.get("plain", "")))


class CachedMessage:
    """Duck-typed stand-in for a pyrogram Message, restored from a snapshot dict.

    Mutable (reply enrichment assigns ``.reply_to_message``). Every top-level attribute the
    render pipeline reads exists with a None/False default so getattr never raises.
    """

    def __init__(self, data: dict):
        self._snapshot = data
        self.id = data.get("id")
        date = data.get("date")
        self.date = datetime.fromisoformat(date) if date is not None else None
        self.text = _restore_str(data.get("text"))
        self.caption = _restore_str(data.get("caption"))
        self.media = _restore_media(data.get("media"))
        # service restored as a plain string; all consumers use truthiness and
        # `'X' in str(service)`.
        self.service = data.get("service")
        self.media_group_id = data.get("media_group_id")
        self.views = data.get("views")
        self.show_caption_above_media = data.get("show_caption_above_media")
        self.reply_to_message_id = data.get("reply_to_message_id")
        self.reply_to_message = None
        self.empty = bool(data.get("empty"))
        self.chat = _restore_chat(data.get("chat"))
        self.sender_chat = _ns(data.get("sender_chat"), ["id", "title", "username"])
        self.from_user = _ns(data.get("from_user"), ["first_name", "last_name", "username"])
        self.forward_origin = _restore_forward_origin(data.get("forward_origin"))
        self.reactions = _restore_reactions(data.get("reactions"))
        self.poll = _restore_poll(data.get("poll"))
        self.web_page = _restore_web_page(data.get("web_page"))
        self.photo = _ns(data.get("photo"), ["file_unique_id"])
        self.video = _ns(data.get("video"), ["file_unique_id", "file_size"])
        self.document = _ns(data.get("document"), ["file_unique_id", "mime_type"])
        self.audio = _ns(data.get("audio"), ["file_unique_id", "mime_type"])
        self.voice = _ns(data.get("voice"), ["file_unique_id", "mime_type"])
        self.video_note = _ns(data.get("video_note"), ["file_unique_id"])
        self.animation = _ns(data.get("animation"), ["file_unique_id"])
        self.sticker = _ns(data.get("sticker"), ["file_unique_id", "emoji", "is_video"])

    def __str__(self) -> str:
        return json.dumps(self._snapshot, default=str)

    def __repr__(self) -> str:
        return self.__str__()


def restore_message(data: dict) -> CachedMessage:
    return CachedMessage(data)


def snapshot_messages(messages: List[Any]) -> List[dict]:
    return [snapshot_message(m) for m in messages]


def restore_messages(items: List[dict]) -> List[CachedMessage]:
    return [restore_message(d) for d in items]
