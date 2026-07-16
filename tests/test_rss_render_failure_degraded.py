# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
"""
Issue #60 — a render error on ONE post must not silently drop it from the feed.

Before the fix, an exception while rendering a message group was logged and `continue`d,
so the post vanished from the feed entirely. Because the guid is stable and RSS shows no
"holes", the reader could not tell a post had ever existed — a silent data loss
indistinguishable from "nothing was posted".

rss_generator now emits a DEGRADED placeholder entry (title + date + stable link/guid +
a "could not be rendered" note) for a group whose render raises, and counts the failure
(surfaced on /health). The other posts still render normally and the feed still generates.
"""
from types import SimpleNamespace
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import pytest

import rss_generator
from rss_generator import generate_channel_rss
from post_parser import PostParser


class _Str(str):
    """Stand-in for Pyrogram's Str: .html returns the raw string unchanged."""
    @property
    def html(self):
        return str(self)


def make_message(mid, text, date):
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
    m.media_group_id = None
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
        return messages

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)


def _make_render_raise_for(monkeypatch, bad_id):
    """Make PostParser.process_message raise for one message id, delegate otherwise.

    This reproduces a real render failure inside _render_messages_groups (the actual
    code path #60 is about), rather than a simplified stand-in.
    """
    original = PostParser.process_message

    def flaky(self, message, include_raw=False, sanitize=True):
        if message.id == bad_id:
            raise RuntimeError("boom: render blew up for this post")
        return original(self, message, include_raw=include_raw, sanitize=sanitize)

    monkeypatch.setattr(PostParser, "process_message", flaky)


@pytest.mark.asyncio
async def test_render_failure_surfaces_degraded_entry_not_dropped(monkeypatch):
    # Dates minutes apart so time-based merge cannot fold them into one entry.
    bad_msg = make_message(101, "THIS_POST_WILL_FAIL_RENDER",
                           datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    good_msg = make_message(202, "SIBLING_CLEAN_MARKER",
                            datetime(2024, 1, 1, 12, 30, 0, tzinfo=timezone.utc))
    _patch_feed_source(monkeypatch, [good_msg, bad_msg])
    _make_render_raise_for(monkeypatch, bad_id=101)

    before = rss_generator.get_render_failed_count()

    # 1) Feed still generates (no propagated 500) despite the failing post.
    rss = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)

    # 2) Valid, parseable XML.
    root = ET.fromstring(rss)

    # 3) BOTH posts are present: the good sibling AND a degraded entry for the failed one.
    items = root.findall(".//item")
    assert len(items) == 2, f"expected the failed post to be surfaced, got {len(items)} items"

    # 4) The good sibling rendered normally.
    assert "SIBLING_CLEAN_MARKER" in rss

    # 5) The failed post is surfaced as a degraded placeholder (visible marker text),
    #    NOT silently dropped.
    assert rss_generator._RENDER_FAILED_NOTE in rss

    # 6) The degraded entry keeps the STABLE guid/link for the failed message id, so a
    #    later successful render updates it in place instead of resurfacing it.
    guids = [g.text for g in root.findall(".//item/guid")]
    assert any(g and g.endswith("/101") for g in guids), f"stable guid for failed post missing: {guids}"

    # 7) The failure was counted (surfaced on /health).
    assert rss_generator.get_render_failed_count() == before + 1


@pytest.mark.asyncio
async def test_all_posts_failing_still_yields_a_feed(monkeypatch):
    # Even if every post fails to render, the feed must generate with degraded entries
    # rather than raising / returning an empty feed.
    m1 = make_message(301, "A", datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc))
    m2 = make_message(302, "B", datetime(2024, 1, 1, 13, 30, 0, tzinfo=timezone.utc))
    _patch_feed_source(monkeypatch, [m1, m2])

    original = PostParser.process_message

    def always_fail(self, message, include_raw=False, sanitize=True):
        raise RuntimeError("boom")

    monkeypatch.setattr(PostParser, "process_message", always_fail)

    rss = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)
    root = ET.fromstring(rss)
    assert len(root.findall(".//item")) == 2
