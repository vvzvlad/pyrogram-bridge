"""Tests for message_snapshot.py (issue #23, Задания 4-5,7).

These verify that a pyrogram Message survives snapshot -> JSON -> restore as a
CachedMessage that the render pipeline (post_parser.py) consumes identically.
"""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pyrogram.enums import MessageMediaType, MessageServiceType

from message_snapshot import (
    snapshot_message,
    restore_message,
    snapshot_messages,
    restore_messages,
    CachedStr,
    CachedMessage,
)
from post_parser import PostParser


class FakeStr(str):
    """Stand-in for a live pyrogram Str: a str carrying a .html rendering."""
    def __new__(cls, plain, html):
        obj = str.__new__(cls, plain)
        obj._html = html
        return obj

    @property
    def html(self):
        return self._html


def _roundtrip(message):
    snap = snapshot_message(message)
    # The snapshot MUST be JSON-serializable (this is the whole point of the rewrite).
    json.dumps(snap)
    return restore_message(snap), snap


# --------------------------------------------------------------------------- #
# Test 1 — top-level round trip.
# --------------------------------------------------------------------------- #
def test_roundtrip_basic_fields():
    msg = SimpleNamespace(
        id=42,
        date=datetime(2020, 1, 2, 3, 4, 5),
        text=FakeStr("hello", "<b>hello</b>"),
        caption=FakeStr("cap", "<i>cap</i>"),
        media=MessageMediaType.PHOTO,
        views=123,
        media_group_id="mg-1",
    )
    restored, _ = _roundtrip(msg)
    assert restored.id == 42
    assert restored.date == datetime(2020, 1, 2, 3, 4, 5)
    assert restored.text.html == "<b>hello</b>"
    assert str(restored.text) == "hello"
    assert restored.caption.html == "<i>cap</i>"
    assert restored.media is MessageMediaType.PHOTO
    assert restored.views == 123
    assert restored.media_group_id == "mg-1"


def test_roundtrip_date_naive_and_aware():
    naive = SimpleNamespace(date=datetime(2021, 5, 5, 10, 0, 0))
    aware = SimpleNamespace(date=datetime(2021, 5, 5, 10, 0, 0, tzinfo=timezone.utc))

    r_naive, _ = _roundtrip(naive)
    r_aware, _ = _roundtrip(aware)

    assert r_naive.date.tzinfo is None
    assert r_aware.date.tzinfo is not None
    assert r_aware.date == datetime(2021, 5, 5, 10, 0, 0, tzinfo=timezone.utc)


def test_text_falls_back_to_plain_when_no_html():
    # A bare str (no .html) -> html defaults to the plain text.
    msg = SimpleNamespace(text="plain only")
    restored, _ = _roundtrip(msg)
    assert restored.text.html == "plain only"
    assert isinstance(restored.text, CachedStr)


# --------------------------------------------------------------------------- #
# Test 2 — polls with FormattedText fakes (must be JSON-serializable).
# --------------------------------------------------------------------------- #
def test_poll_formatted_text_unwrapped():
    option = SimpleNamespace(text=SimpleNamespace(text="Opt", entities=[]))
    poll = SimpleNamespace(
        question=SimpleNamespace(text="Q?", entities=[]),
        options=[option],
    )
    msg = SimpleNamespace(poll=poll)

    snap = snapshot_message(msg)
    # This MUST NOT raise. If the snapshot stored the FormattedText objects as-is, the
    # SimpleNamespace values would make json.dumps raise TypeError (regression guard).
    json.dumps(snap)

    restored = restore_message(snap)
    assert restored.poll.question == "Q?"
    assert isinstance(restored.poll.question, str)
    opt = restored.poll.options[0]
    assert getattr(opt, "text", "") == "Opt"


def test_poll_snapshot_would_fail_if_formattedtext_kept_raw():
    # Guard proving the assertion above actually catches raw FormattedText: a snapshot dict
    # that carried the FormattedText object would not be JSON-serializable.
    bad_snap = {"poll": {"question": SimpleNamespace(text="Q?"), "options": []}}
    with pytest.raises(TypeError):
        json.dumps(bad_snap)


# --------------------------------------------------------------------------- #
# Test 3 — reactions (normal / paid / custom).
# --------------------------------------------------------------------------- #
def test_reactions_normal_paid_custom():
    normal = SimpleNamespace(emoji="👍", count=5, is_paid=False)
    paid = SimpleNamespace(count=3, is_paid=True)
    custom = SimpleNamespace(custom_emoji_id=1234567890, count=2)
    msg = SimpleNamespace(reactions=SimpleNamespace(reactions=[normal, paid, custom]))

    restored, _ = _roundtrip(msg)
    r_normal, r_paid, r_custom = restored.reactions.reactions

    assert r_normal.emoji == "👍"
    assert r_normal.count == 5
    assert r_normal.is_paid is False
    assert r_normal.custom_emoji_id is None

    assert r_paid.is_paid is True
    assert r_paid.count == 3

    # Custom emoji: emoji is null, custom_emoji_id is a STRING; key still present.
    assert hasattr(r_custom, "emoji") is True
    assert r_custom.emoji is None
    assert isinstance(r_custom.custom_emoji_id, str)
    assert r_custom.custom_emoji_id == "1234567890"


# --------------------------------------------------------------------------- #
# Test 4 — forward_origin Case 1-5 (presence-semantics drives _format_forward_info).
# --------------------------------------------------------------------------- #
def _forward_html(forward_origin):
    msg = SimpleNamespace(forward_origin=forward_origin)
    restored, _ = _roundtrip(msg)
    return PostParser(None)._format_forward_info(restored)


def test_forward_origin_cases():
    # Case 1: channel/supergroup (has .chat)
    html = _forward_html(SimpleNamespace(
        type="channel",
        chat=SimpleNamespace(id=-100, title="Chan", username="chanu"),
    ))
    assert "Chan" in html and "@chanu" in html

    # Case 2: hidden user (sender_user_name)
    html = _forward_html(SimpleNamespace(type="hidden_user", sender_user_name="Hidden Guy"))
    assert "Hidden Guy" in html

    # Case 3: regular user (sender_user)
    html = _forward_html(SimpleNamespace(
        type="user",
        sender_user=SimpleNamespace(first_name="Ann", last_name="Bee", username="annbee"),
    ))
    assert "Ann Bee" in html and "@annbee" in html

    # Case 4: channel without username (chat_id + title, no chat attr)
    html = _forward_html(SimpleNamespace(type="channel", chat_id=-1002, title="NoUserChan"))
    assert "NoUserChan" in html

    # Case 5: anything else
    html = _forward_html(SimpleNamespace(type="something_else"))
    assert html == '<div class="message-forward">--- Forwarded message ---</div>'


def test_forward_origin_presence_semantics():
    # Only keys present on the live object are recorded (hasattr semantics).
    restored, snap = _roundtrip(SimpleNamespace(
        forward_origin=SimpleNamespace(type="hidden_user", sender_user_name="X")))
    assert "chat" not in snap["forward_origin"]
    assert "sender_user" not in snap["forward_origin"]
    assert not hasattr(restored.forward_origin, "chat")
    assert hasattr(restored.forward_origin, "sender_user_name")


# --------------------------------------------------------------------------- #
# Test 5 — service restored as string usable in `'X' in str(service)`.
# --------------------------------------------------------------------------- #
def test_service_pinned_message():
    msg = SimpleNamespace(service=MessageServiceType.PINNED_MESSAGE)
    restored, _ = _roundtrip(msg)
    assert "PINNED_MESSAGE" in str(restored.service)


# --------------------------------------------------------------------------- #
# Test 6 — mutability + deepcopy (.text.html survives).
# --------------------------------------------------------------------------- #
def test_mutable_and_deepcopy():
    msg = SimpleNamespace(id=7, text=FakeStr("body", "<u>body</u>"))
    restored, _ = _roundtrip(msg)

    # Mutable: reply enrichment assigns reply_to_message.
    sentinel = object()
    restored.reply_to_message = sentinel
    assert restored.reply_to_message is sentinel

    clone = copy.deepcopy(restored)
    assert clone.text.html == "<u>body</u>"
    assert str(clone.text) == "body"
    assert clone.id == 7


def test_cachedstr_survives_pickle():
    import pickle as _pkl
    s = CachedStr.build("plain", "<b>plain</b>")
    back = _pkl.loads(_pkl.dumps(s))
    assert str(back) == "plain"
    assert back.html == "<b>plain</b>"


# --------------------------------------------------------------------------- #
# Test 7 — unknown media name -> None, no exception.
# --------------------------------------------------------------------------- #
def test_unknown_media_type():
    snap = snapshot_message(SimpleNamespace())
    snap["media"] = "FUTURE_TYPE"
    restored = restore_message(snap)  # must not raise
    assert restored.media is None


# --------------------------------------------------------------------------- #
# Test 9 — >100MB video is not collected by _save_media_file_ids.
# --------------------------------------------------------------------------- #
def test_large_video_not_collected():
    snap = snapshot_message(SimpleNamespace())
    snap["media"] = "VIDEO"
    snap["video"] = {"file_unique_id": "vid1", "file_size": 200 * 1024 * 1024}
    snap["chat"] = {"id": -1001, "username": "chan", "title": "t", "usernames": None}
    restored = restore_message(snap)

    pp = PostParser(None)
    pp._save_media_file_ids(restored)
    assert pp._pending_media_ids == []


def test_normal_video_is_collected():
    snap = snapshot_message(SimpleNamespace())
    snap["id"] = 10
    snap["media"] = "VIDEO"
    snap["video"] = {"file_unique_id": "vid2", "file_size": 1024}
    snap["chat"] = {"id": -1001, "username": "chan", "title": "t", "usernames": None}
    restored = restore_message(snap)

    pp = PostParser(None)
    pp._save_media_file_ids(restored)
    assert len(pp._pending_media_ids) == 1
    assert pp._pending_media_ids[0][2] == "vid2"


# --------------------------------------------------------------------------- #
# Test 10 — restored chat without username -> None, not AttributeError.
# --------------------------------------------------------------------------- #
def test_chat_without_username():
    snap = snapshot_message(SimpleNamespace())
    snap["chat"] = {"id": -100, "username": None, "title": "T", "usernames": None}
    restored = restore_message(snap)
    assert restored.chat.username is None


def test_chat_usernames_restored_as_objects():
    chat = SimpleNamespace(
        id=-100, username=None, title="T",
        usernames=[SimpleNamespace(username="alt", active=True),
                   SimpleNamespace(username="old", active=False)],
    )
    restored, _ = _roundtrip(SimpleNamespace(chat=chat))
    active = [u.username for u in restored.chat.usernames if u.active]
    assert active == ["alt"]
    # get_channel_username uses exactly this path.
    assert PostParser(None).get_channel_username(restored) == "alt"


# --------------------------------------------------------------------------- #
# Defaults: every pipeline-consumed attribute exists (getattr never raises).
# --------------------------------------------------------------------------- #
def test_empty_message_defaults():
    restored = restore_message(snapshot_message(SimpleNamespace()))
    for attr in ["id", "date", "text", "caption", "media", "service", "media_group_id",
                 "views", "show_caption_above_media", "reply_to_message_id",
                 "reply_to_message", "chat", "sender_chat", "from_user", "forward_origin",
                 "reactions", "poll", "web_page", "photo", "video", "document", "audio",
                 "voice", "video_note", "animation", "sticker"]:
        assert hasattr(restored, attr)
    assert restored.empty is False


def test_snapshot_messages_list_roundtrip():
    msgs = [SimpleNamespace(id=i) for i in range(3)]
    restored = restore_messages(snapshot_messages(msgs))
    assert [m.id for m in restored] == [0, 1, 2]
    assert all(isinstance(m, CachedMessage) for m in restored)
