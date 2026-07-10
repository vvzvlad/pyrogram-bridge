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
# v2: added the special-media info-block types (story, contact, location, venue, dice,
# game, giveaway, giveaway_winners, checklist, paid_media) plus live_photo. A v1 file lacks
# these keys, so a cached special-media / live-photo message would restore with an empty
# block — invalidate v1 files.
SNAPSHOT_VERSION = 2


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


# --------------------------------------------------------------------------- #
# Special-media snapshots (issue #23 review fix).
#
# _format_special_media (post_parser.py) and find_file_id_in_message
# (api_server.py) read a handful of type-specific attributes off the Message that
# were NOT in schema v1. On a cache hit a restored Message carried media=<enum> but
# message.<attr>=None, so the special info block silently vanished (render
# divergence vs. a live Message). Each helper below snapshots EXACTLY the sub-fields
# the renderer + find_file_id read for that type — nothing more, nothing less.
# --------------------------------------------------------------------------- #
def _snapshot_story(story: Any) -> Optional[dict]:
    """STORY: _story_media_object / find_file_id_in_message read story.video and
    story.photo, each by .file_unique_id (video wins over photo). The chosen object is
    also the one _save_media_file_ids reads .file_size off for the >100MB skip, so
    file_size is snapshotted too (else a >100MB story media would be collected on a
    cache hit — F1). The render URL is still built from file_unique_id."""
    if story is None:
        return None
    return {
        "video": _snapshot_obj(getattr(story, "video", None), ["file_unique_id", "file_size"]),
        "photo": _snapshot_obj(getattr(story, "photo", None), ["file_unique_id", "file_size"]),
    }


def _snapshot_venue(venue: Any) -> Optional[dict]:
    """VENUE: reads .title, .address and its nested .location (.latitude/.longitude via
    _format_osm_link)."""
    if venue is None:
        return None
    return {
        "title": getattr(venue, "title", None),
        "address": getattr(venue, "address", None),
        "location": _snapshot_obj(getattr(venue, "location", None), ["latitude", "longitude"]),
    }


def _snapshot_giveaway(giveaway: Any) -> Optional[dict]:
    """GIVEAWAY: reads .quantity, .months, .stars, .until_date (a datetime rendered via
    strftime) and .description. until_date is stored as isoformat and restored to a
    datetime so the strftime branch reproduces byte-for-byte."""
    if giveaway is None:
        return None
    until = getattr(giveaway, "until_date", None)
    return {
        "quantity": getattr(giveaway, "quantity", None),
        "months": getattr(giveaway, "months", None),
        "stars": getattr(giveaway, "stars", None),
        # Only a real datetime renders (renderer gates on hasattr(.,'strftime')); anything
        # else is dropped to None so the same "no until date" branch is taken on restore.
        "until_date": until.isoformat() if hasattr(until, "isoformat") else None,
        "description": getattr(giveaway, "description", None),
    }


def _snapshot_checklist(checklist: Any) -> Optional[dict]:
    """CHECKLIST: reads .title (used ONLY when it isinstance str) and .tasks; each task
    reads .text and the truthiness of .completed_by / .completion_date (→ ☑/☐).

    title is stored only when it is a str (matching the renderer's isinstance gate, which
    otherwise renders an empty title). completed_by / completion_date are stored as bools
    (the renderer only tests their truthiness) so a live User/datetime stays JSON-safe."""
    if checklist is None:
        return None
    title = getattr(checklist, "title", None)
    snap_tasks = []
    for task in (getattr(checklist, "tasks", None) or []):
        text = getattr(task, "text", "")
        # Mirror the renderer: a non-str text is str()-ified. Storing the resolved string
        # keeps the restored value isinstance-str, reproducing the same rendered bytes.
        text_str = text if isinstance(text, str) else str(text)
        snap_tasks.append({
            "text": text_str,
            "completed_by": bool(getattr(task, "completed_by", None)),
            "completion_date": bool(getattr(task, "completion_date", None)),
        })
    return {
        "title": title if isinstance(title, str) else None,
        "tasks": snap_tasks,
    }


def _snapshot_paid_media(paid_media: Any) -> Optional[dict]:
    """PAID_MEDIA: _generate_html_media reads .stars_amount (default 0) and the LENGTH of
    .media. Snapshot the renderer's effective stars value and the item count; the media
    list is restored as N placeholders so len() reproduces."""
    if paid_media is None:
        return None
    media = getattr(paid_media, "media", None)
    count = len(media) if isinstance(media, (list, tuple)) else 0
    return {
        # Snapshot getattr(...,0) so a missing attribute restores to 0 exactly as the
        # renderer would have defaulted it (a present None stays None on both sides).
        "stars_amount": getattr(paid_media, "stars_amount", 0),
        "media_count": count,
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
        # file_size is snapshotted for every type whose selected object flows into
        # _save_media_file_ids' >100MB skip (MEDIA_SOURCES: document/audio/animation/
        # video_note select this exact object). Without it a restored >100MB media
        # would have file_size=None and be wrongly collected on a cache hit (F1).
        "document": _snapshot_obj(getattr(message, "document", None), ["file_unique_id", "mime_type", "file_size"]),
        "audio": _snapshot_obj(getattr(message, "audio", None), ["file_unique_id", "mime_type", "file_size"]),
        "voice": _snapshot_obj(getattr(message, "voice", None), ["file_unique_id", "mime_type"]),
        "video_note": _snapshot_obj(getattr(message, "video_note", None), ["file_unique_id", "file_size"]),
        "animation": _snapshot_obj(getattr(message, "animation", None), ["file_unique_id", "file_size"]),
        "sticker": _snapshot_obj(getattr(message, "sticker", None), ["file_unique_id", "emoji", "is_video"]),
        # LIVE_PHOTO (Kurigram 2.2.23) renders as a video element via the video_loop_400
        # kind. MEDIA_SOURCES/_get_file_unique_id/_save_media_file_ids read live_photo's
        # file_unique_id + file_size (the >100MB skip); find_file_id also reads file_unique_id.
        # Mirror the `video` allowlist so a cached live-photo message keeps its media block.
        "live_photo": _snapshot_obj(getattr(message, "live_photo", None), ["file_unique_id", "file_size"]),
        # Special-media info-block types (issue #23 review fix). Each reads a fixed set of
        # sub-fields in _format_special_media / find_file_id_in_message; snapshot exactly those.
        "story": _snapshot_story(getattr(message, "story", None)),
        "contact": _snapshot_obj(getattr(message, "contact", None), ["first_name", "last_name", "phone_number"]),
        "location": _snapshot_obj(getattr(message, "location", None), ["latitude", "longitude"]),
        "venue": _snapshot_venue(getattr(message, "venue", None)),
        "dice": _snapshot_obj(getattr(message, "dice", None), ["emoji", "value"]),
        "game": _snapshot_obj(getattr(message, "game", None), ["title"]),
        "giveaway": _snapshot_giveaway(getattr(message, "giveaway", None)),
        "giveaway_winners": _snapshot_obj(
            getattr(message, "giveaway_winners", None), ["winner_count", "quantity", "prize_description"]),
        "checklist": _snapshot_checklist(getattr(message, "checklist", None)),
        "paid_media": _snapshot_paid_media(getattr(message, "paid_media", None)),
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


# --------------------------------------------------------------------------- #
# Special-media restores (issue #23 review fix). Mirror the snapshot helpers above:
# rebuild each object so the exact attributes _format_special_media /
# find_file_id_in_message read exist, restoring the special block on a cache hit.
# --------------------------------------------------------------------------- #
def _restore_story(d: Optional[dict]) -> Optional[SimpleNamespace]:
    if d is None:
        return None
    return SimpleNamespace(
        video=_ns(d.get("video"), ["file_unique_id", "file_size"]),
        photo=_ns(d.get("photo"), ["file_unique_id", "file_size"]),
    )


def _restore_venue(d: Optional[dict]) -> Optional[SimpleNamespace]:
    if d is None:
        return None
    return SimpleNamespace(
        title=d.get("title"),
        address=d.get("address"),
        location=_ns(d.get("location"), ["latitude", "longitude"]),
    )


def _restore_giveaway(d: Optional[dict]) -> Optional[SimpleNamespace]:
    if d is None:
        return None
    until = d.get("until_date")
    return SimpleNamespace(
        quantity=d.get("quantity"),
        months=d.get("months"),
        stars=d.get("stars"),
        # Restore a datetime so hasattr(until_date, 'strftime') holds exactly as on a live
        # object; None (no/invalid date) restores as None and the strftime branch is skipped.
        until_date=datetime.fromisoformat(until) if until is not None else None,
        description=d.get("description"),
    )


def _restore_checklist(d: Optional[dict]) -> Optional[SimpleNamespace]:
    if d is None:
        return None
    restored_tasks = [
        SimpleNamespace(
            text=t.get("text"),
            completed_by=t.get("completed_by"),
            completion_date=t.get("completion_date"),
        )
        for t in (d.get("tasks") or [])
    ]
    return SimpleNamespace(title=d.get("title"), tasks=restored_tasks)


def _restore_paid_media(d: Optional[dict]) -> Optional[SimpleNamespace]:
    if d is None:
        return None
    count = d.get("media_count") or 0
    # media is only ever len()'d by the renderer, so N placeholder items reproduce the count.
    return SimpleNamespace(stars_amount=d.get("stars_amount"), media=[None] * count)


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
        self.document = _ns(data.get("document"), ["file_unique_id", "mime_type", "file_size"])
        self.audio = _ns(data.get("audio"), ["file_unique_id", "mime_type", "file_size"])
        self.voice = _ns(data.get("voice"), ["file_unique_id", "mime_type"])
        self.video_note = _ns(data.get("video_note"), ["file_unique_id", "file_size"])
        self.animation = _ns(data.get("animation"), ["file_unique_id", "file_size"])
        self.sticker = _ns(data.get("sticker"), ["file_unique_id", "emoji", "is_video"])
        # LIVE_PHOTO: restored like `video` so the video_loop_400 media block renders on a
        # cache hit (file_unique_id → URL; file_size → the >100MB collection skip).
        self.live_photo = _ns(data.get("live_photo"), ["file_unique_id", "file_size"])
        # Special-media info-block types (issue #23 review fix): restored so the type's
        # info block renders on a cache hit exactly as for a live Message.
        self.story = _restore_story(data.get("story"))
        self.contact = _ns(data.get("contact"), ["first_name", "last_name", "phone_number"])
        self.location = _ns(data.get("location"), ["latitude", "longitude"])
        self.venue = _restore_venue(data.get("venue"))
        self.dice = _ns(data.get("dice"), ["emoji", "value"])
        self.game = _ns(data.get("game"), ["title"])
        self.giveaway = _restore_giveaway(data.get("giveaway"))
        self.giveaway_winners = _ns(
            data.get("giveaway_winners"), ["winner_count", "quantity", "prize_description"])
        self.checklist = _restore_checklist(data.get("checklist"))
        self.paid_media = _restore_paid_media(data.get("paid_media"))

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
