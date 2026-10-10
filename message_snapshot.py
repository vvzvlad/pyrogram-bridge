#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylance: disable=reportMissingImports, reportMissingModuleSource

"""JSON snapshot / restore for pyrogram Message objects (issue #23).

The history cache no longer pickles live pyrogram objects. On write a plain JSON
snapshot is extracted from a Message via an explicit allowlist, the FIELDS table; on read
a duck-typed CachedMessage is restored that is indistinguishable from a real Message
to the render pipeline (post_parser.py / rss_generator.py are NOT changed).
"""

import html
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Union

from pyrogram.enums import MessageMediaType
from pyrogram.types.messages_and_media.message import Str

import rich_tree

logger = logging.getLogger(__name__)

# A version mismatch makes _load_entry treat the cache file as a miss, so every channel is
# re-fetched from Telegram. Bump SNAPSHOT_VERSION only when a file written by the previous
# code would restore WRONG: a stored value changes its shape or meaning in a way the restore
# cannot read, or `rich_tree.SCHEMA_V` is bumped. Adding a field does NOT bump it: restore
# reads every key with `.get`, an old file restores the new field as None and renders
# without it until the history TTL (6.4–8 h) expires. (forward_origin restores by key
# presence instead: a new key is absent on an old file, which the render's hasattr/getattr
# reads already handle.) To change a stored value's shape,
# prefer a new key next to the old one: old files and a rolled-back build both keep reading
# the old key, and no bump is needed.
# The channel-info cache (tg_cache.cached_get_chat) shares this version but restores with
# SimpleNamespace(**data), not .get, and lives CHAT_CACHE_TTL_HOURS (9.6–12 h): a key added
# there is ABSENT on an old file, so read it with getattr(..., None) or bump.
# Version history: `git log -G 'SNAPSHOT_VERSION = ' -- message_snapshot.py`.
SNAPSHOT_VERSION = 9


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
    html falls back to the ESCAPED plain text. Escaping is what the renderers do for a
    value without ``.html`` (post_parser._format_reply_quote), so storing the raw plain
    text here would make a cache hit emit raw markup where a live fetch emits escaped
    text — the restored and the live render must be byte-identical.
    """
    if value is None:
        return None
    plain = str(value)
    html_value = getattr(value, "html", None)
    if html_value is None:
        html_value = html.escape(plain)
    return {"plain": plain, "html": str(html_value)}


def _restore_str(d: Optional[Union[dict, str]]) -> Optional[CachedStr]:
    if d is None:
        return None
    # Defensive: a pre-v5 payload (or a hand-built one) may carry a bare string here. It is
    # escaped for the same live-vs-cache parity reason as in _snapshot_str.
    if isinstance(d, str):
        return CachedStr.build(d, html.escape(d))
    plain = d.get("plain", "")
    # Uniform policy with _snapshot_str and the bare-string branch above: with no stored html
    # the plain text is escaped. A stored empty string is honoured, but None is not — a
    # CachedStr whose .html is None would crash _get_post_text_with_urls (text.html.replace).
    stored = d.get("html")
    html_value = stored if stored is not None else html.escape(plain)
    return CachedStr.build(plain, html_value)


def _enum_name(value: Any) -> Any:
    """Return an enum member's ``.name`` (JSON-safe) or the value unchanged."""
    if value is None:
        return None
    return value.name if hasattr(value, "name") else value


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


def _restore_datetime(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value is not None else None


# --------------------------------------------------------------------------- #
# Field codecs. A table maps an attribute name to the codec of that field; snapshot_message
# and CachedMessage both walk FIELDS, so every stored field is declared once.
# --------------------------------------------------------------------------- #
class _Codec:
    """How one field is stored: ``dump(live value) -> JSON value`` and
    ``load(JSON value) -> restored value``.

    The live value is the owner's attribute named by the field (None if absent); a codec
    with ``read`` takes it as ``read(owner)`` instead.
    """

    def __init__(self, dump: Callable[[Any], Any], load: Callable[[Any], Any], *,
                 read: Optional[Callable[[Any], Any]] = None):
        self.dump = dump
        self.load = load
        self.read = read


def _same(value: Any) -> Any:
    return value


def _dump_fields(fields: Dict[str, _Codec], owner: Any) -> dict:
    return {
        name: codec.dump(codec.read(owner) if codec.read is not None else getattr(owner, name, None))
        for name, codec in fields.items()
    }


def _load_fields(fields: Dict[str, _Codec], data: dict, target: Any) -> None:
    for name, codec in fields.items():
        setattr(target, name, codec.load(data.get(name)))


# Stored and restored as is.
VALUE = _Codec(_same, _same)
# A text/caption: stored as {'plain', 'html'}, restored as a CachedStr.
TEXT = _Codec(_snapshot_str, _restore_str)
# A field no other kind describes: its own dump and load (and read).
CUSTOM = _Codec


def OBJ(fields: Dict[str, _Codec]) -> _Codec:
    """A nested object described by its own table, stored as a dict of the allowlisted fields
    (getattr(..., None), or the field codec's read), or None if the object is None.

    Restored with the FULL key set (None-defaults). Live pyrogram objects always set every
    attribute in __init__, and the pipeline reads them directly (e.g. rss_generator ~131
    message.chat.username in an except; post_parser ~1087 u.username / u.active without
    getattr). So restored objects must carry all keys. None restores as None (the attribute
    was absent on the live object).
    """
    def dump(obj: Any) -> Optional[dict]:
        return None if obj is None else _dump_fields(fields, obj)

    def load(data: Optional[dict]) -> Optional[SimpleNamespace]:
        if data is None:
            return None
        restored = SimpleNamespace()
        _load_fields(fields, data, restored)
        return restored

    return _Codec(dump, load)


def LIST(codec: _Codec) -> _Codec:
    """A list of ``codec`` values. An absent or empty list is stored as None; None restores as None."""
    return _Codec(lambda items: [codec.dump(item) for item in items] if items else None,
                  lambda items: None if items is None else [codec.load(item) for item in items])


# --------------------------------------------------------------------------- #
# Shapes several fields share.
# --------------------------------------------------------------------------- #
_ENUM_NAME = CUSTOM(_enum_name, _same)
_PHOTO = OBJ({"file_unique_id": VALUE})
_MEDIA_FILE = OBJ({"file_unique_id": VALUE, "file_size": VALUE})
_LOCATION = OBJ({"latitude": VALUE, "longitude": VALUE})
_CHAT_REF = OBJ({"id": VALUE, "title": VALUE, "username": VALUE})
_USER_REF = OBJ({"first_name": VALUE, "last_name": VALUE, "username": VALUE})


# --------------------------------------------------------------------------- #
# Irregular fields.
# --------------------------------------------------------------------------- #
# forward_origin is the ONE object whose key SET mirrors the live object: the
# MessageOrigin* classes each carry a DIFFERENT set of attributes and
# _format_forward_info branches by hasattr (Case 1-5). Presence of a key in the
# snapshot must mirror presence of the attribute on the source object.
# A codec's read= is not supported here: both presence and value come from the key itself
# (hasattr / getattr).
_FORWARD_ORIGIN_FIELDS = {
    "type": _ENUM_NAME,
    "chat": _CHAT_REF,
    "sender_user_name": VALUE,
    "sender_user": _USER_REF,
    "chat_id": VALUE,
    "title": VALUE,
}


def _snapshot_forward_origin(fo: Any) -> Optional[dict]:
    if fo is None:
        return None
    return {key: codec.dump(getattr(fo, key))
            for key, codec in _FORWARD_ORIGIN_FIELDS.items() if hasattr(fo, key)}


def _restore_forward_origin(d: Optional[dict]) -> Optional[SimpleNamespace]:
    """Restore forward_origin mirroring ONLY the recorded keys (presence-semantics)."""
    if d is None:
        return None
    ns = SimpleNamespace()
    for key, value in d.items():
        setattr(ns, key, _FORWARD_ORIGIN_FIELDS.get(key, VALUE).load(value))
    return ns


# Custom-emoji reactions carry no unicode emoji: emoji is null and the
# custom_emoji_id is stored as a STRING = str(document_id).
def _reaction_emoji(reaction: Any) -> Any:
    return None if getattr(reaction, "custom_emoji_id", None) is not None else getattr(reaction, "emoji", None)


_REACTION = OBJ({
    "emoji": CUSTOM(_same, _same, read=_reaction_emoji),
    "custom_emoji_id": CUSTOM(lambda cid: str(cid) if cid is not None else None, _same),
    "count": VALUE,
    "is_paid": VALUE,
})


def _snapshot_reactions(reactions: Any) -> Optional[list]:
    rlist = getattr(reactions, "reactions", None)
    return None if rlist is None else [_REACTION.dump(r) for r in rlist]


def _restore_reactions(items: Optional[list]) -> Optional[SimpleNamespace]:
    if items is None:
        return None
    # Mirror pyrogram: message.reactions is an object exposing a .reactions list.
    return SimpleNamespace(reactions=[_REACTION.load(r) for r in items])


_UNWRAPPED_TEXT = CUSTOM(_unwrap_text, _same)
# options must be namespace objects with a .text string; a bare string would make
# getattr(option, 'text', '') return '' and render empty options.
_POLL_OPTIONS = LIST(OBJ({"text": _UNWRAPPED_TEXT}))
_POLL = OBJ({
    "question": _UNWRAPPED_TEXT,
    "options": CUSTOM(_POLL_OPTIONS.dump, lambda items: _POLL_OPTIONS.load(items) or []),
    # The description_media objects post_parser._poll_media_object may pick, in its order. Each
    # keeps file_unique_id (the render URL) and file_size (_save_media_file_ids' >100MB skip).
    "description_media": OBJ({"photo": _MEDIA_FILE, "video": _MEDIA_FILE, "animation": _MEDIA_FILE,
                              "sticker": _MEDIA_FILE}),
})


_STICKER = OBJ({"file_unique_id": VALUE, "emoji": VALUE, "is_video": VALUE, "is_animated": VALUE})


def _snapshot_sticker(sticker: Any) -> Optional[dict]:
    """STICKER: the selector reads the flags and only the truthiness of .thumbs (an animated
    sticker with a thumbnail renders as that image). The thumbnail count is stored; the list
    is restored as N placeholders, like paid_media's items."""
    snap = _STICKER.dump(sticker)
    if snap is not None:
        snap["thumbs_count"] = len(getattr(sticker, "thumbs", None) or [])
    return snap


def _restore_sticker(d: Optional[dict]) -> Optional[SimpleNamespace]:
    sticker = _STICKER.load(d)
    if sticker is None:
        return None
    # thumbs is only tested for truthiness, so N placeholders reproduce it; None when there
    # are none, as on a live sticker. A file written before thumbs_count restores as None.
    count = d.get("thumbs_count") or 0
    sticker.thumbs = [None] * count if count else None
    return sticker


_STORY_MEDIA = OBJ({"video": _MEDIA_FILE, "photo": _MEDIA_FILE})


def _snapshot_story(story: Any) -> Optional[dict]:
    """STORY: _story_media_object / find_file_id_in_message read story.video and
    story.photo, each by .file_unique_id (video wins over photo). The chosen object is
    also the one _save_media_file_ids reads .file_size off for the >100MB skip, so
    file_size is snapshotted too (else a >100MB story media would be collected on a
    cache hit — F1). The render URL is still built from file_unique_id. The caption
    (a plain str in kurigram) is rendered by _format_special_media. Its markup comes
    separately in caption_entities, so the HTML they produce is stored next to it as
    caption_html (None without entities); "caption" stays a plain str."""
    snap = _STORY_MEDIA.dump(story)
    if snap is None:
        return None
    caption = getattr(story, "caption", None)
    entities = getattr(story, "caption_entities", None)
    snap["caption"] = str(caption) if isinstance(caption, str) else None
    # Same fallback as the renderer: an entity unparse cannot handle (a name mention without
    # user data) yields None, so the caption is stored without markup instead of failing the
    # snapshot of the whole channel.
    snap["caption_html"] = getattr(Str(caption).init(list(entities)), "html", None) if entities and isinstance(caption, str) else None
    return snap


def _restore_story(d: Optional[dict]) -> Optional[SimpleNamespace]:
    story = _STORY_MEDIA.load(d)
    if story is None:
        return None
    caption = d.get("caption")
    caption_html = d.get("caption_html")
    # The stored markup rides on the caption as a CachedStr; without it the caption stays
    # a bare str, which the renderer escapes like a live caption without entities.
    story.caption = CachedStr.build(caption, caption_html) if caption_html is not None else caption
    # The markup, if any, is already in caption.html. The renderer still reads
    # caption_entities for a bare-string caption, and a restored object carries every
    # attribute the renderer reads.
    story.caption_entities = None
    return story


# CHECKLIST: reads .title (used ONLY when it isinstance str) and .tasks; each task
# reads .text and the truthiness of .completed_by / .completion_date (→ ☑/☐).
#
# title is stored only when it is a str (matching the renderer's isinstance gate, which
# otherwise renders an empty title). completed_by / completion_date are stored as bools
# (the renderer only tests their truthiness) so a live User/datetime stays JSON-safe.
_AS_BOOL = CUSTOM(bool, _same)
_CHECKLIST_TASKS = LIST(OBJ({
    # Mirror the renderer: a non-str text is str()-ified. Storing the resolved string
    # keeps the restored value isinstance-str, reproducing the same rendered bytes.
    "text": CUSTOM(lambda text: text if isinstance(text, str) else str(text), _same,
                   read=lambda task: getattr(task, "text", "")),
    "completed_by": _AS_BOOL,
    "completion_date": _AS_BOOL,
}))
_CHECKLIST = OBJ({
    "title": CUSTOM(lambda title: title if isinstance(title, str) else None, _same),
    "tasks": CUSTOM(lambda tasks: _CHECKLIST_TASKS.dump(tasks) or [],
                    lambda items: _CHECKLIST_TASKS.load(items) or []),
})


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


def _restore_paid_media(d: Optional[dict]) -> Optional[SimpleNamespace]:
    if d is None:
        return None
    count = d.get("media_count") or 0
    # media is only ever len()'d by the renderer, so N placeholder items reproduce the count.
    return SimpleNamespace(stars_amount=d.get("stars_amount"), media=[None] * count)


# --------------------------------------------------------------------------- #
# The stored fields of a Message: snapshot_message stores one key per entry and
# CachedMessage restores one attribute per entry.
# --------------------------------------------------------------------------- #
FIELDS: Dict[str, _Codec] = {
    "id": VALUE,
    # isoformat()/fromisoformat() preserve naive/aware exactly as pyrogram stores it.
    "date": CUSTOM(lambda date: date.isoformat() if date is not None else None, _restore_datetime),
    "text": TEXT,
    "caption": TEXT,
    "media": CUSTOM(_enum_name, _restore_media),
    # service restored as a plain string; all consumers use truthiness and
    # `'X' in str(service)`.
    "service": _ENUM_NAME,
    "media_group_id": VALUE,
    "views": VALUE,
    "show_caption_above_media": VALUE,
    "reply_to_message_id": VALUE,
    # DEPTH-1 snapshot of a resolved reply target: ONLY the fields _format_reply_info
    # (post_parser.py) reads. The reply's own nested reply_to_message is deliberately NOT
    # snapshotted (no recursion).
    #
    # Reply-target texts are stored IN FULL (and with their .html rendering) precisely BECAUSE the
    # renderer (_format_reply_info) is the one that may shorten a quote: keeping the whole text here
    # makes a cache hit render byte-identically to a live fetch and lets REPLY_QUOTE_TRUNCATE_* be
    # retuned without invalidating the cache. Truncating at snapshot time would do neither.
    "reply_to_message": OBJ({
        "id": VALUE,
        "text": TEXT,
        "caption": TEXT,
        # `chat` carries the id alone: it exists solely so _reply_is_near_own_channel can tell
        # "same channel" from "another chat" on a cache hit. pyrogram parses `chat` on every
        # Message independently of sender_chat/from_user, which is why it works where
        # sender_chat does not (a channel signing posts with the authors' profiles has
        # from_user set and sender_chat None).
        "chat": OBJ({"id": VALUE}),
        "sender_chat": _CHAT_REF,
        "from_user": OBJ({"id": VALUE, "first_name": VALUE, "last_name": VALUE, "username": VALUE}),
    }),
    # message.quote (TextQuote): only its text, with the .html rendering of its entities.
    "quote": OBJ({"text": TEXT}),
    # message.external_reply: what post_parser._format_external_reply_info reads — the target
    # chat + message id (the link) and the origin's author fields (the label).
    "external_reply": OBJ({
        "message_id": VALUE,
        "chat": _CHAT_REF,
        "origin": OBJ({"chat": _CHAT_REF, "sender_chat": _CHAT_REF, "sender_user": _USER_REF,
                       "sender_user_name": VALUE}),
    }),
    "author_signature": VALUE,
    "empty": CUSTOM(_same, bool),
    "chat": OBJ({
        "id": VALUE,
        "username": VALUE,
        "title": VALUE,
        # ChatType enum -> its name, like `media`/`service`: an enum is not JSON-serialisable.
        # _reply_is_near_own_channel needs it to tell a channel feed (where a neighbouring
        # post really is the entry above) from a group/discussion feed. Restored as the NAME
        # string, not the enum: readers must compare by name (_reply_is_near_own_channel
        # does), never by enum identity.
        "type": _ENUM_NAME,
        "usernames": LIST(OBJ({"username": VALUE, "active": VALUE})),
    }),
    "sender_chat": _CHAT_REF,
    "from_user": _USER_REF,
    "forward_origin": CUSTOM(_snapshot_forward_origin, _restore_forward_origin),
    "reactions": CUSTOM(_snapshot_reactions, _restore_reactions),
    "poll": _POLL,
    "web_page": OBJ({
        "type": VALUE,
        "url": VALUE,
        "display_url": VALUE,
        "site_name": VALUE,
        "title": VALUE,
        "description": VALUE,
        "has_large_media": VALUE,
        "photo": _PHOTO,
    }),
    "photo": _PHOTO,
    "video": _MEDIA_FILE,
    # file_size is snapshotted for every type whose selected object flows into
    # _save_media_file_ids' >100MB skip (MEDIA_SOURCES: document/audio/animation/
    # video_note select this exact object). Without it a restored >100MB media
    # would have file_size=None and be wrongly collected on a cache hit (F1).
    "document": OBJ({"file_unique_id": VALUE, "mime_type": VALUE, "file_size": VALUE, "file_name": VALUE}),
    "audio": OBJ({"file_unique_id": VALUE, "mime_type": VALUE, "file_size": VALUE, "performer": VALUE,
                  "title": VALUE}),
    "voice": OBJ({"file_unique_id": VALUE, "mime_type": VALUE}),
    "video_note": _MEDIA_FILE,
    "animation": _MEDIA_FILE,
    "sticker": CUSTOM(_snapshot_sticker, _restore_sticker),
    # LIVE_PHOTO (Kurigram 2.2.23) renders as a video element via the video_loop_400
    # kind. MEDIA_SOURCES/_get_file_unique_id/_save_media_file_ids read live_photo's
    # file_unique_id + file_size (the >100MB skip); find_file_id also reads file_unique_id.
    # Mirror the `video` allowlist so a cached live-photo message keeps its media block.
    "live_photo": _MEDIA_FILE,
    # Special-media info-block types (issue #23 review fix). _format_special_media
    # (post_parser.py) and find_file_id_in_message (api_server.py) read a fixed set of
    # type-specific sub-fields; each entry stores EXACTLY those — nothing more, nothing
    # less — so the type's info block renders on a cache hit exactly as for a live Message.
    "story": CUSTOM(_snapshot_story, _restore_story),
    "contact": OBJ({"first_name": VALUE, "last_name": VALUE, "phone_number": VALUE}),
    "location": _LOCATION,
    # VENUE: the nested .location is read via _format_osm_link.
    "venue": OBJ({"title": VALUE, "address": VALUE, "location": _LOCATION}),
    "dice": OBJ({"emoji": VALUE, "value": VALUE}),
    "game": OBJ({"title": VALUE}),
    "invoice": OBJ({"title": VALUE, "description": VALUE, "currency": VALUE, "total_amount": VALUE}),
    "giveaway": OBJ({
        "quantity": VALUE,
        "months": VALUE,
        "stars": VALUE,
        # until_date (a datetime rendered via strftime) is stored as isoformat and restored to
        # a datetime, so hasattr(until_date, 'strftime') holds exactly as on a live object and
        # the strftime branch reproduces byte-for-byte. Only a real datetime renders (renderer
        # gates on hasattr(.,'strftime')); anything else is dropped to None so the same
        # "no until date" branch is taken on restore.
        "until_date": CUSTOM(lambda until: until.isoformat() if hasattr(until, "isoformat") else None,
                             _restore_datetime),
        "description": VALUE,
    }),
    "giveaway_winners": OBJ({"winner_count": VALUE, "quantity": VALUE, "prize_description": VALUE}),
    "checklist": _CHECKLIST,
    "paid_media": CUSTOM(_snapshot_paid_media, _restore_paid_media),
    # Rich Messages (Kurigram 2.2.24, #85). Store the canonical tree VERBATIM (JSON-safe
    # by construction). Obtained through rich_tree.tree_of — NOT from_pyrogram directly —
    # so the snapshot and the live render share ONE memoised tree per object (parity).
    # None for a non-rich post. Restored directly: all consumers reach it through
    # rich_tree.tree_of, which reads this `rich_tree` attribute on a CachedMessage. This
    # makes the cache-hit render byte-identical to the live one.
    "rich_tree": CUSTOM(_same, _same, read=lambda message: rich_tree.tree_of(message)),
}


def snapshot_message(message: Any) -> dict:
    """Extract a JSON-serializable snapshot from a pyrogram Message: one key per FIELDS entry.

    Every field is read via getattr(..., None) unless its codec has a read. See the module
    docstring / issue #23 for the field contract.
    """
    return _dump_fields(FIELDS, message)


class CachedMessage:
    """Duck-typed stand-in for a pyrogram Message, restored from a snapshot dict.

    ``.reply_to_message`` comes from the snapshot itself (reply targets are resolved at
    fetch time in cached_get_chat_history and persisted); nothing assigns it post-restore.
    Every top-level attribute the render pipeline reads exists with a None/False default
    so getattr never raises.
    """

    def __init__(self, data: dict):
        self._snapshot = data
        _load_fields(FIELDS, data, self)
        # rich_message stays None — the live objects are not snapshotted; the tree is in rich_tree.
        self.rich_message = None

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
