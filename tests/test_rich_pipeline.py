# flake8: noqa
# pylint: disable=missing-function-docstring, missing-class-docstring, redefined-outer-name, line-too-long
# pylint: disable=protected-access
"""End-to-end rich pipeline tests (#85): process_message body/title/flags/fids, the
cache-path restore parity (+ v6 miss), and the /media fid resolution / transient
classification on the download path.
"""
import json
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import rich_tree
import message_snapshot as ms
from post_parser import PostParser


# --------------------------------------------------------------------------------------
# Helpers: a full-surface message namespace (mirrors the parity corpus) + rich stubs.
# --------------------------------------------------------------------------------------
_CHAT = SimpleNamespace(id=-1001234500000, username="richchan", title="Rich Channel", usernames=None)

_DEFAULTS = dict(
    id=555, date=datetime(2026, 7, 20, 12, 0, 0), text=None, caption=None, media=None,
    service=None, media_group_id=None, views=10, show_caption_above_media=False,
    reply_to_message_id=None, reply_to_message=None, empty=False, chat=_CHAT,
    sender_chat=None, from_user=None, forward_origin=None, reactions=None, poll=None,
    web_page=None, photo=None, video=None, document=None, audio=None, voice=None,
    video_note=None, animation=None, sticker=None, story=None, contact=None,
    location=None, venue=None, dice=None, game=None, giveaway=None,
    giveaway_winners=None, checklist=None, paid_media=None, live_photo=None,
)


def node(clsname, **fields):
    obj = type(clsname, (), {})()
    for k, v in fields.items():
        setattr(obj, k, v)
    return obj


def media_obj(fid, file_size=None, typename="Photo"):
    # typename mirrors the real Kurigram media class (types.Photo/Video/...); the download
    # path's large-video probe keys on type(obj).__name__ == 'Video'.
    obj = type(typename, (), {})()
    obj.file_unique_id = fid
    obj.file_size = file_size
    obj.file_id = "FID_" + fid
    obj.mime_type = None
    return obj


def make_rich_message():
    heading = node("RichBlockSectionHeading", size=1, text="My Rich Title")
    paragraph = node("RichBlockParagraph", text="Some body text here")
    photo = node("RichBlockPhoto", photo=media_obj("smallph", file_size=1000, typename="Photo"), caption=None)
    big_video = node("RichBlockVideo", video=media_obj("bigvid", file_size=200 * 1024 * 1024, typename="Video"), caption=None)
    return SimpleNamespace(blocks=[heading, paragraph, photo, big_video], is_rtl=False, part=False)


def make_msg(**overrides):
    fields = dict(_DEFAULTS)
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def parser():
    return PostParser(MagicMock())


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    # process_message -> _generate_html_media -> _save_media_file_ids only APPENDS to the
    # in-memory list; the DB flush is a separate call, so nothing to patch. Reset counter.
    rich_tree.reset_counters()
    yield


# --------------------------------------------------------------------------------------
# process_message on a live rich post.
# --------------------------------------------------------------------------------------
class TestProcessMessageRich:
    def test_body_title_flag(self, parser):
        msg = make_msg(rich_message=make_rich_message())
        result = parser.process_message(msg, include_raw=False, sanitize=True)
        # Title from the first heading.
        assert result["html"]["title"] == "My Rich Title"
        # Rich content div with the heading rendered.
        assert 'class="rich-content"' in result["html"]["body"]
        assert "<h3>My Rich Title</h3>" in result["html"]["body"]
        assert "Some body text here" in result["html"]["body"]
        # rich flag present; no_image absent (the tree carries media).
        assert "rich" in result["flags"]
        assert "no_image" not in result["flags"]

    def test_media_fids_collected_with_100mb_skip(self, parser):
        msg = make_msg(rich_message=make_rich_message())
        parser.process_message(msg, include_raw=False, sanitize=True)
        fids = [t[2] for t in parser._pending_media_ids]
        assert "smallph" in fids          # small photo collected
        assert "bigvid" not in fids       # >100MB video skipped (registry §3.13)

    def test_empty_rich_tree_shows_single_placard(self, parser):
        # A sentinel / empty rich message: exactly ONE "could not be rendered" placard,
        # no empty rich-content div, and the post stays alive.
        msg = make_msg(rich_message=SimpleNamespace(blocks=[], is_rtl=False, part=False))
        body = parser._generate_html_body(msg)
        assert body.count("could not be rendered") == 1
        assert 'class="rich-content"' not in body

    def test_part_placard_rendered(self, parser):
        rm = make_rich_message()
        rm.part = True
        msg = make_msg(rich_message=rm)
        body = parser._generate_html_body(msg)
        assert "Only part of this post is shown" in body

    def test_non_rich_post_has_no_rich_div(self, parser):
        msg = make_msg(text=SimpleNamespace(html="hello", __str__=lambda s: "hello"))
        # A plain post: tree_of is None -> no rich-content div, no rich flag.
        body = parser._generate_html_body(msg)
        assert "rich-content" not in body
        assert "rich" not in parser._extract_flags(msg, html_body=body)


# --------------------------------------------------------------------------------------
# Cache-path parity + an outdated-version miss.
# --------------------------------------------------------------------------------------
class TestCachePathParity:
    def test_live_vs_restored_render_identical(self, parser):
        msg = make_msg(rich_message=make_rich_message())
        live = parser.process_message(msg, include_raw=False, sanitize=True)

        restored = ms.restore_message(ms.snapshot_message(msg))
        # A restored CachedMessage carries rich_message=None and the stored tree.
        assert restored.rich_message is None
        assert restored.rich_tree is not None
        cached = PostParser(MagicMock()).process_message(restored, include_raw=False, sanitize=True)

        assert cached["html"]["title"] == live["html"]["title"]
        assert cached["html"]["body"] == live["html"]["body"]
        assert cached["flags"] == live["flags"]

    def test_restored_collects_same_fids(self):
        msg = make_msg(rich_message=make_rich_message())
        restored = ms.restore_message(ms.snapshot_message(msg))
        p = PostParser(MagicMock())
        p.process_message(restored, include_raw=False, sanitize=True)
        fids = [t[2] for t in p._pending_media_ids]
        assert "smallph" in fids and "bigvid" not in fids

    def test_v6_file_is_a_miss_and_the_current_version_is_a_hit(self, tmp_path):
        import tg_cache
        path = os.path.join(tmp_path, "history.json")
        payload = {"limit": 5, "messages": []}

        # An entry written with the current SNAPSHOT_VERSION is a hit.
        tg_cache._store_entry(path, payload)
        assert tg_cache._load_entry(path, max_age_hours=999) is not None

        # A v6 entry (the phase-1 rich_present marker era) is rejected as a version mismatch.
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
        entry["version"] = 6
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entry, f)
        assert tg_cache._load_entry(path, max_age_hours=999) is None


# --------------------------------------------------------------------------------------
# /media fid resolution + transient classification (api_server).
# --------------------------------------------------------------------------------------
class TestFindFileIdRich:
    async def test_rich_fid_resolves_to_file_id(self):
        import api_server
        msg = MagicMock()
        msg.media = None
        msg.rich_message = make_rich_message()
        got = await api_server.find_file_id_in_message(msg, "smallph")
        assert got == "FID_smallph"

    async def test_rich_big_video_fid_resolves(self):
        import api_server
        msg = MagicMock()
        msg.media = None
        msg.rich_message = make_rich_message()
        assert await api_server.find_file_id_in_message(msg, "bigvid") == "FID_bigvid"

    async def test_foreign_fid_returns_none(self):
        import api_server
        msg = MagicMock()
        msg.media = None
        msg.rich_message = make_rich_message()
        # A fid not present anywhere: rich walk misses, then the general loop misses too.
        # The MagicMock media attributes compare unequal to the fid, so None is returned.
        msg.photo = msg.video = msg.animation = msg.video_note = None
        msg.audio = msg.voice = msg.sticker = msg.document = None
        msg.web_page = None
        msg.chat = SimpleNamespace(id=-100)
        msg.id = 1
        assert await api_server.find_file_id_in_message(msg, "nonexistent") is None


class TestTransientClassification:
    async def test_rich_large_video_size_only_for_big_video(self):
        import api_server
        msg = SimpleNamespace(rich_message=make_rich_message())
        # bigvid is a >100MB Video -> its size is returned; small photo is not a video.
        assert api_server._rich_large_video_size(msg, "bigvid") == 200 * 1024 * 1024
        assert api_server._rich_large_video_size(msg, "smallph") is None

    def test_has_parse_failures_drives_transient(self):
        # The download path raises 503 (transient) instead of deleting the SQLite row when
        # the rich parse degraded. Assert the predicate the branch keys on.
        from kurigram_compat import has_parse_failures
        healthy = make_rich_message()
        assert has_parse_failures(healthy) is False
        sentinel = SimpleNamespace(parse_failed=True, blocks=[])
        assert has_parse_failures(sentinel) is True
