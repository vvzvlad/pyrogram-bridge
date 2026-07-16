# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
"""
Issue #54 (critical) — XML-incompatible control chars / lone surrogates in a single
post text must NOT crash the whole RSS feed.

feedgen serializes via lxml, which raises
    ValueError: All strings must be XML compatible ... no control characters
for any XML-forbidden control char (\\x00-\\x08, \\x0B, \\x0C, \\x0E-\\x1F) or lone
surrogate (\\uD800-\\uDFFF) — even inside CDATA. That ValueError used to propagate out
of generate_channel_rss as an HTTP 500 on the ENTIRE feed, on every reader poll, until
the bad post scrolled out of the fetch window.

rss_generator now strips those chars from every string handed to feedgen, immediately
before serialization, while keeping XML-valid whitespace (TAB \\x09, LF \\x0A, CR \\x0D).
"""
from types import SimpleNamespace
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import pytest

from rss_generator import generate_channel_rss, _strip_xml_incompatible


class _Str(str):
    """Stand-in for Pyrogram's Str: .html returns the raw string unchanged, so the
    control chars reach the pre-serialize html/text exactly like real entity text."""
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


# --------------------------------------------------------------------------- #
# Unit: the helper strips exactly the XML-incompatible set and nothing else.
# --------------------------------------------------------------------------- #
def test_strip_removes_control_and_surrogates_keeps_valid_whitespace():
    dirty = "a\x00b\x08c\x0bd\x0ce\x1ff\ud800g"
    assert _strip_xml_incompatible(dirty) == "abcdefg"
    # TAB / LF / CR are XML-compatible and must survive untouched.
    assert _strip_xml_incompatible("x\ty\nz\r") == "x\ty\nz\r"
    # Ordinary + non-BMP (single code point, NOT a surrogate pair) text is preserved.
    assert _strip_xml_incompatible("привет 😀 tail") == "привет 😀 tail"


def test_strip_passes_through_non_str():
    assert _strip_xml_incompatible(None) is None
    assert _strip_xml_incompatible(123) == 123


# --------------------------------------------------------------------------- #
# Regression: a bad post no longer 500s the feed, and its siblings survive.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rss_feed_survives_control_chars_in_post_text(monkeypatch):
    # \x0c (FF) + \x08 (BS) + a lone high surrogate. Tab/newline are valid and kept.
    bad_text = "before\x0cmid\x08tail\ud800\tKEEPTAB\nKEEPNL after"
    clean_text = "SIBLING_CLEAN_MARKER"
    # Dates minutes apart so time-based merge cannot fold them into one entry.
    bad_msg = make_message(101, bad_text, datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    clean_msg = make_message(202, clean_text, datetime(2024, 1, 1, 12, 30, 0, tzinfo=timezone.utc))
    _patch_feed_source(monkeypatch, [clean_msg, bad_msg])

    # 1) Must NOT raise (today's bug: ValueError -> HTTP 500 on the whole feed).
    rss = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)

    # 2) Output is valid, parseable XML.
    root = ET.fromstring(rss)

    # 3) BOTH entries are present (the bad post AND its clean sibling).
    items = root.findall(".//item")
    assert len(items) == 2, f"expected both posts in feed, got {len(items)}"

    # 4) The XML-incompatible chars are gone from the serialized output, while the
    #    normal text (and the kept tab/newline neighbours) survived.
    assert "\x0c" not in rss and "\x08" not in rss and "\ud800" not in rss
    assert "SIBLING_CLEAN_MARKER" in rss
    assert "beforemidtail" in rss          # bad chars removed, surrounding text intact
    assert "KEEPTAB" in rss and "KEEPNL" in rss
