# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
"""
Issue #59 — a post with no date used to be handled THREE inconsistent ways:

  * _create_messages_groups group sort -> float('inf')      (sorts NEWEST, survives [:limit])
  * _render_messages_groups final sort  -> 0.0              (sorts to the TAIL of the feed)
  * generate_channel_rss pubDate        -> datetime.now(utc) (NON-DETERMINISTIC — changes per poll)

Feed order (tail) and pubDate ("now") contradicted each other, and the now() pubDate made
the entry float in date-sorted readers (Miniflux) and — because the feed ETag is a content
signature over the serialized body incl. <pubDate> (#62) — kept the ETag from ever
stabilizing for any feed containing a dateless post.

The fix unifies all three on ONE deterministic fallback: the Unix epoch
(rss_generator.MISSING_DATE_TS = 0.0), used identically in every sort key and in the RSS
pubDate, never now(). These tests assert that unification, its determinism across
serializations, and the resulting #62 ETag stability for dateless feeds.
"""
from types import SimpleNamespace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import pytest

import rss_generator
from rss_generator import generate_channel_rss, MISSING_DATE_TS
import api_server


class _Str(str):
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


def _items_by_id(rss):
    """Return [(message_id, pubDate_datetime), ...] in feed (document) order.

    Keyed by the trailing message id of <link> (the RSS <title> is title-cased by the
    renderer, so it is not a reliable identity handle).
    """
    root = ET.fromstring(rss)
    out = []
    for item in root.findall(".//item"):
        link = item.findtext("link") or ""
        mid = int(link.rstrip("/").rsplit("/", 1)[-1])
        pub = item.findtext("pubDate")
        dt = parsedate_to_datetime(pub) if pub else None
        out.append((mid, dt))
    return out


# --------------------------------------------------------------------------- #
# 1) The dateless post's pubDate is the deterministic epoch fallback, NOT now().
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dateless_post_pubdate_is_epoch_not_now(monkeypatch):
    dateless = make_message(101, "DATELESS_MARKER", None)
    _patch_feed_source(monkeypatch, [dateless])

    rss = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)
    items = _items_by_id(rss)
    assert len(items) == 1
    _, pub = items[0]

    expected = datetime.fromtimestamp(MISSING_DATE_TS, tz=timezone.utc)
    assert pub is not None
    assert pub.astimezone(timezone.utc) == expected  # epoch 1970-01-01T00:00:00Z
    # And emphatically NOT "now": the fallback must be far in the past.
    assert pub.astimezone(timezone.utc).year == 1970


# --------------------------------------------------------------------------- #
# 2) Determinism: same input -> identical pubDate across independent serializations.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dateless_pubdate_is_deterministic_across_serializations(monkeypatch):
    # Two full, independent renders of the same dateless post must yield the SAME pubDate.
    # With the old now() fallback these differed by the wall-clock gap between the two runs.
    dateless = make_message(101, "DATELESS_MARKER", None)
    _patch_feed_source(monkeypatch, [dateless])

    rss1 = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)
    rss2 = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)

    pub1 = _items_by_id(rss1)[0][1]
    pub2 = _items_by_id(rss2)[0][1]
    assert pub1 == pub2


# --------------------------------------------------------------------------- #
# 3) Consistency across paths: the dateless post sorts to the tail by pubDate — the
#    guarantee readers (Miniflux) act on — instead of contradicting itself (newest in the
#    group sort, tail in the final sort, "now" in pubDate).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dateless_post_pubdate_is_the_oldest(monkeypatch):
    older = make_message(1, "OLDER_REAL", datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    newer = make_message(2, "NEWER_REAL", datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc))
    dateless = make_message(3, "DATELESS_MARKER", None)
    _patch_feed_source(monkeypatch, [older, newer, dateless])

    rss = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)
    items = dict(_items_by_id(rss))

    # The dateless post's pubDate is the smallest (epoch) of the whole feed, so a date-sorted
    # reader places it at the tail — consistent with the internal tail sort, no floating.
    pub_dateless = items[3]
    others = [dt for mid, dt in items.items() if mid != 3]
    assert all(pub_dateless < o for o in others)
    assert pub_dateless.astimezone(timezone.utc) == datetime.fromtimestamp(MISSING_DATE_TS, tz=timezone.utc)


# --------------------------------------------------------------------------- #
# 4) #62 ETag no longer floats for a dateless feed.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dateless_feed_etag_is_stable_across_polls(monkeypatch):
    # #62's known limitation was exactly this: a dateless post's now() pubDate changed on
    # every poll, so the content-signature ETag never matched between two polls and 304
    # could never fire. With a deterministic epoch pubDate the two bodies are ETag-equal
    # (lastBuildDate, the only remaining volatile field, is stripped by _feed_etag).
    dateless = make_message(101, "DATELESS_MARKER", None)
    _patch_feed_source(monkeypatch, [dateless])

    rss1 = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)
    rss2 = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)

    # Sanity: the raw bodies differ only in the volatile <lastBuildDate>, not in <pubDate>.
    assert "1970" in rss1  # epoch pubDate present
    etag1 = api_server._feed_etag(rss1)
    etag2 = api_server._feed_etag(rss2)
    assert etag1 == etag2, "dateless-feed ETag must be stable across polls (#59 fixes #62 float)"


# --------------------------------------------------------------------------- #
# 5) A feed mixing dated and dateless posts still generates and keeps a stable ETag.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mixed_feed_etag_stable_and_dated_pubdate_untouched(monkeypatch):
    dated = make_message(1, "DATED_REAL", datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc))
    dateless = make_message(2, "DATELESS_MARKER", None)
    _patch_feed_source(monkeypatch, [dated, dateless])

    rss1 = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)
    rss2 = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=20)

    items = dict(_items_by_id(rss1))
    # The dated post (id 1) keeps its real pubDate; only the dateless one (id 2) uses epoch.
    assert items[1].astimezone(timezone.utc) == datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
    assert items[2].astimezone(timezone.utc) == datetime.fromtimestamp(MISSING_DATE_TS, tz=timezone.utc)

    assert api_server._feed_etag(rss1) == api_server._feed_etag(rss2)
