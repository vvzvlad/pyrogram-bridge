# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
"""
Issue #58 — a media group's guid must be STABLE across polls, independent of which subset
of the album's messages happens to fall inside the current fetch window.

Before the fix the merged-group guid was derived from the message_id of the "first message
carrying text/caption". That representative shifts as the album enters/leaves the window
(the group composition itself grows newest-member-first), so the reader dedups on a guid
that changes every poll -> the same album re-surfaces as a NEW unread entry (duplicate).

The fix keys a native album's guid on its immutable Telegram media_group_id, which is
identical for every member and independent of how many are in the window. This test drives
the real generate_channel_rss path across several window subsets of one album and asserts
the album's guid never changes (while the naive message-id basis demonstrably would).
"""
from types import SimpleNamespace
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import pytest

from rss_generator import generate_channel_rss


class _Str(str):
    """Stand-in for Pyrogram's Str: .html returns the raw string unchanged."""
    @property
    def html(self):
        return str(self)


def make_message(mid, text, date, media_group_id=None):
    m = SimpleNamespace()
    m.id = mid
    m.date = date
    m.text = _Str(text) if text is not None else None
    m.caption = None
    m.media = None
    m.web_page = None
    m.poll = None
    m.service = None
    m.forward_origin = None
    m.reply_to_message = None
    m.reply_to_message_id = None
    m.sender_chat = None
    m.from_user = None
    m.reactions = None
    m.views = 100
    m.media_group_id = media_group_id
    m.show_caption_above_media = False
    m.chat = SimpleNamespace(id=-1001234567890, username="testchan")
    for attr in ("photo", "video", "document", "audio", "voice",
                 "video_note", "animation", "sticker"):
        setattr(m, attr, None)
    return m


def _patch_feed_source(monkeypatch, messages):
    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        # The returned list IS the fetch window; the caller controls the subset.
        return messages

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)


def _one_item(rss):
    root = ET.fromstring(rss)
    items = root.findall(".//item")
    assert len(items) == 1, f"expected exactly one (merged) item, got {len(items)}"
    item = items[0]
    guid_el = item.find("guid")
    link_el = item.find("link")
    return guid_el.text, guid_el.get("isPermaLink"), link_el.text


async def _poll_album_guid(monkeypatch, window):
    _patch_feed_source(monkeypatch, window)
    rss = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)
    return _one_item(rss)


# A native Telegram album: three consecutive messages sharing one media_group_id, posted in
# the same second (as real albums are). Ids are consecutive so a shifting window changes the
# min/first member.
ALBUM_MGID = "ALBUM_XYZ_13391216106820330"
_D = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_album_guid_stable_across_window_subsets(monkeypatch):
    m100 = make_message(100, "album-a", _D, media_group_id=ALBUM_MGID)
    m101 = make_message(101, "album-b", _D, media_group_id=ALBUM_MGID)
    m102 = make_message(102, "album-c", _D, media_group_id=ALBUM_MGID)

    # Poll 1: only the newest two members are inside the window (the earliest fell off / has
    # not been fetched yet). Poll 2: the whole album is inside the window.
    guid_a, permalink_a, link_a = await _poll_album_guid(monkeypatch, [m101, m102])
    guid_b, permalink_b, link_b = await _poll_album_guid(monkeypatch, [m100, m101, m102])

    # Core assertion (the dedup the issue wants): shifting the window did NOT change the
    # album's guid, so the reader does not re-show it as a new entry.
    assert guid_a == guid_b, f"album guid changed across window shift: {guid_a!r} != {guid_b!r}"

    # The guid is keyed on the immutable media_group_id (not a real page -> not a permalink).
    assert f"mediagroup={ALBUM_MGID}" in guid_a
    assert permalink_a == "false" and permalink_b == "false"

    # The naive basis (the human-facing <link>, still the representative message) DID shift
    # with the window — proving the window really changed the group composition and that the
    # guid's stability is not an accident of an unchanged representative.
    assert link_a != link_b, f"expected the representative link to shift with the window: {link_a} == {link_b}"
    assert link_a.endswith("/101") and link_b.endswith("/100")


@pytest.mark.asyncio
async def test_single_member_of_album_in_window_shares_album_guid(monkeypatch):
    # The hardest transient: when an album first enters the window only its newest member is
    # visible (a one-message group that still carries the media_group_id). Its guid must
    # already equal the full album's guid, or it duplicates the moment the rest arrives.
    m100 = make_message(100, "album-a", _D, media_group_id=ALBUM_MGID)
    m101 = make_message(101, "album-b", _D, media_group_id=ALBUM_MGID)
    m102 = make_message(102, "album-c", _D, media_group_id=ALBUM_MGID)

    guid_single, permalink_single, _ = await _poll_album_guid(monkeypatch, [m102])
    guid_full, _, _ = await _poll_album_guid(monkeypatch, [m100, m101, m102])

    assert guid_single == guid_full, f"single-member album guid diverged: {guid_single!r} != {guid_full!r}"
    assert f"mediagroup={ALBUM_MGID}" in guid_single
    assert permalink_single == "false"


@pytest.mark.asyncio
async def test_non_album_single_message_keeps_permalink_guid(monkeypatch):
    # A plain (non-album) message is unaffected: its guid stays the immutable-message-id
    # t.me permalink, so existing single-post feeds do not churn.
    msg = make_message(500, "solo", _D, media_group_id=None)
    guid, permalink, link = await _poll_album_guid(monkeypatch, [msg])
    assert permalink == "true"
    assert guid.endswith("/500") and link.endswith("/500")
