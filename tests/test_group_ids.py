# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""Stage 4 (render-pipeline refactor, issue #31 / epic #34) — pure time-clustering.

`_create_time_based_media_groups` deep-copied every cached message per feed request and
MUTATED media_group_id. It is replaced by `_compute_time_based_group_ids`, a PURE function
returning {message.id: effective_media_group_id}; `_create_messages_groups` reads that
mapping instead of a mutated attribute. All tests use NAIVE dates (as kurigram emits on
prod), not aware-UTC mocks.

Behavior registry items exercised here: §3.11 (None-date excluded from clustering, no
mapping entry; own media_group_id still applies) and §3.12 (naive-safe sort keys).

Issue #59 unified the treatment of a None-date post: it now uses ONE deterministic epoch
fallback (rss_generator.MISSING_DATE_TS) in EVERY sort key and in the RSS pubDate, so it
sorts to the tail everywhere and is trimmed by [:limit] like any other oldest post — it no
longer masquerades as the newest to survive the slice (the old, self-contradictory hack).
"""
import os
import time as _time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import rss_generator as rss_module
from rss_generator import (
    _compute_time_based_group_ids,
    _create_messages_groups,
    generate_channel_html,
)


D = datetime  # naive datetimes throughout


class _Str(str):
    """Str stand-in: .html returns the raw string (mirrors kurigram's Str)."""
    @property
    def html(self):
        return str(self)


class Msg:
    """Minimal message stand-in for the pure clustering function (needs id/date/mgid)."""
    def __init__(self, mid, date, media_group_id=None):
        self.id = mid
        self.date = date
        self.media_group_id = media_group_id
        self.service = None


def at(sec, minute=0):
    # Naive local datetime, same shape kurigram's datetime.fromtimestamp() produces.
    return D(2024, 1, 1, 12, minute, sec)


# --------------------------------------------------------------------------- #
# Core clustering equivalence with the old mutation.
# --------------------------------------------------------------------------- #
def test_time_cluster_without_id_gets_synthetic():
    a, b = Msg(1, at(0)), Msg(2, at(2))
    mapping = _compute_time_based_group_ids([a, b], merge_seconds=5)
    synthetic = f"time_{at(0)}"
    assert mapping == {1: synthetic, 2: synthetic}


def test_adoption_backfill_and_overwrite():
    # First member has no id, second brings "B" (first truthy in cluster order -> wins and
    # is BACKFILLED onto member 1), third brings "C" which is OVERWRITTEN to "B".
    a, b, c = Msg(1, at(0)), Msg(2, at(2), "B"), Msg(3, at(4), "C")
    mapping = _compute_time_based_group_ids([a, b, c], merge_seconds=5)
    assert mapping == {1: "B", 2: "B", 3: "B"}


def test_falsy_id_ignored_like_old_code():
    # 0 and "" are falsy: they are NOT adopted as the cluster id, exactly as the old
    # truthiness check. A 2-member cluster with only falsy ids gets a synthetic id.
    a, b = Msg(1, at(0), 0), Msg(2, at(2), "")
    mapping = _compute_time_based_group_ids([a, b], merge_seconds=5)
    synthetic = f"time_{at(0)}"
    assert mapping == {1: synthetic, 2: synthetic}


def test_singleton_gets_no_entry():
    # A lone message (even one carrying a truthy media_group_id) forms a singleton cluster
    # and gets NO entry; downstream it falls back to its own media_group_id.
    lonely = Msg(1, at(0), "MG")
    far = Msg(2, at(30, minute=1), "OTHER")  # >5s gap -> separate singleton
    mapping = _compute_time_based_group_ids([lonely, far], merge_seconds=5)
    assert mapping == {}


def test_input_is_not_mutated():
    a, b, c = Msg(1, at(0)), Msg(2, at(2), "B"), Msg(3, at(4), "C")
    before = [(m.id, m.date, m.media_group_id) for m in (a, b, c)]
    _compute_time_based_group_ids([a, b, c], merge_seconds=5)
    after = [(m.id, m.date, m.media_group_id) for m in (a, b, c)]
    assert before == after
    assert a.media_group_id is None and b.media_group_id == "B" and c.media_group_id == "C"


def test_ties_equal_dates_cluster_in_fetch_order():
    # Equal dates -> stable sort keeps fetch order; they cluster (gap 0 <= merge) and the
    # first truthy id in fetch order wins.
    a, b = Msg(1, at(5), "FIRST"), Msg(2, at(5), "SECOND")
    mapping = _compute_time_based_group_ids([a, b], merge_seconds=5)
    assert mapping == {1: "FIRST", 2: "FIRST"}


def test_gap_uses_naive_datetime_subtraction_not_timestamps():
    # The gap is a NAIVE wall-clock subtraction, exactly as the old code — NOT a timestamp
    # diff (they DIVERGE across a DST fold, and the old behavior is the contract). Pin a
    # DST zone and straddle the ambiguous "fall back" hour with the two folds:
    #   a = 01:30:00 fold=0 -> the FIRST (EDT, UTC-4) occurrence of that wall-clock time;
    #   b = 01:30:02 fold=1 -> the SECOND (EST, UTC-5) occurrence, ~1h later in REAL time.
    # Naive subtraction (b.date - a.date) = 2s -> they cluster. A timestamp diff would be
    # ~3602s -> they would NOT cluster. So mutating the gap to a `.timestamp()` diff turns
    # this red, locking the naive-subtraction contract.
    os.environ["TZ"] = "America/New_York"
    _time.tzset()
    try:
        a = Msg(1, D(2024, 11, 3, 1, 30, 0, fold=0))
        b = Msg(2, D(2024, 11, 3, 1, 30, 2, fold=1))
        # Sanity-check the fixture itself: naive gap 2s, real (timestamp) gap ~1h.
        assert (b.date - a.date).total_seconds() == 2
        assert b.date.timestamp() - a.date.timestamp() > 3600
        mapping = _compute_time_based_group_ids([a, b], merge_seconds=5)
        synthetic = f"time_{D(2024, 11, 3, 1, 30, 0)}"
        assert mapping == {1: synthetic, 2: synthetic}
    finally:
        os.environ["TZ"] = "UTC"
        _time.tzset()


# --------------------------------------------------------------------------- #
# §3.11 — None-date posts: excluded from clustering, own media_group_id still applies.
# --------------------------------------------------------------------------- #
def test_none_date_gets_no_mapping_entry():
    dated, nodate = Msg(1, at(0), "MG"), Msg(2, None, "MG")
    mapping = _compute_time_based_group_ids([dated, nodate], merge_seconds=5)
    assert 2 not in mapping  # None-date never participates


def test_mixed_none_date_does_not_crash():
    # The historical prod bug: a single None-date post amid naive-dated posts raised
    # TypeError. The pure function must simply skip it.
    msgs = [Msg(1, at(0)), Msg(2, None), Msg(3, at(2))]
    mapping = _compute_time_based_group_ids(msgs, merge_seconds=5)  # must not raise
    assert 2 not in mapping
    # The two dated posts still cluster (2s apart) under a synthetic id.
    synthetic = f"time_{at(0)}"
    assert mapping == {1: synthetic, 3: synthetic}


def test_fully_none_input_yields_empty_mapping():
    # Registry §3.11: the old code clustered a fully-None tail by insertion order (only
    # reachable via aware-date test mocks). New behavior: no clustering at all.
    msgs = [Msg(1, None, "A"), Msg(2, None), Msg(3, None)]
    assert _compute_time_based_group_ids(msgs, merge_seconds=5) == {}


def test_none_date_media_group_still_assembled_downstream():
    # None-date posts get no mapping entry but keep their own media_group_id, so a media
    # group made of None-date members is still assembled by _create_messages_groups.
    m1, m2 = Msg(10, None, "SHARED"), Msg(11, None, "SHARED")
    solo = Msg(12, at(0))
    mapping = _compute_time_based_group_ids([m1, m2, solo], merge_seconds=5)
    groups = _create_messages_groups([m1, m2, solo], mapping)
    shared = [g for g in groups if len(g) == 2]
    assert len(shared) == 1
    assert {m.id for m in shared[0]} == {10, 11}


# --------------------------------------------------------------------------- #
# §3.12 — naive-safe group sort: None-date groups survive [:limit] and land at the tail.
# --------------------------------------------------------------------------- #
def test_create_messages_groups_none_date_does_not_crash_default_path():
    # This is the LIVE default-path 500: a None-date group used to fall back to an AWARE
    # now() in the group sort key and blow up against the naive real dates. No mapping /
    # time_based_merge needed — plain grouping must survive.
    dated = Msg(1, at(0))
    nodate = Msg(2, None)
    groups = _create_messages_groups([dated, nodate])  # must not raise
    # Canonical #59 rule: None-date group uses the epoch fallback -> sorts to the TAIL
    # (oldest), so the real-dated group is first and the None-date group is last.
    assert groups[0][0].id == 1
    assert groups[-1][0].id == 2


def _make_message(mid, text, date, media_group_id=None):
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


@pytest.mark.asyncio
async def test_none_date_post_renders_and_lands_at_feed_end(monkeypatch):
    # END-TO-END regression for the live prod 500: a None-date post in a feed of real-dated
    # posts, WITH time_based_merge. Old code raised TypeError (naive vs aware) in BOTH the
    # group sort key (default path) and the time-cluster sort -> HTTP 500 on the default
    # feed path. New code renders it and, per §3.12, places it at the tail of the feed.
    posts = [
        _make_message(1, "OLDEST_REAL", at(0)),
        _make_message(2, "NEWEST_REAL", at(30)),
        _make_message(3, "NONE_DATE_POST", None),
    ]

    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return posts

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)
    monkeypatch.setitem(rss_module.Config, "time_based_merge", True)

    html = await generate_channel_html("testchan", client=SimpleNamespace(), limit=10)

    assert "NONE_DATE_POST" in html
    assert "NEWEST_REAL" in html and "OLDEST_REAL" in html
    # None-date post renders LAST (final post sort fallback 0.0 -> tail of feed, §3.12).
    assert html.index("NONE_DATE_POST") > html.index("OLDEST_REAL")
    assert html.index("NONE_DATE_POST") > html.index("NEWEST_REAL")


@pytest.mark.asyncio
async def test_none_date_group_sorts_oldest_and_is_dropped_under_limit(monkeypatch):
    # Canonical #59 rule: a None-date group uses the epoch fallback in the group sort key,
    # so it sorts as the OLDEST and is trimmed by [:limit] like any other oldest post —
    # NOT kept alive as the newest (the old, self-contradictory float('inf') hack that
    # contradicted its tail pubDate). This test applies REAL limit pressure (limit < number
    # of groups) so the slice actually drops groups: with 5 dated posts + 1 None-date post
    # and limit=3, the surviving 3 groups are the 3 NEWEST dated; the None-date group and
    # the 2 oldest dated are dropped. If the fallback regressed back to 'inf' the None-date
    # group would sort newest and survive -> this assertion fails.
    posts = [_make_message(mid, f"DATED_{mid}", at(0, minute=mid)) for mid in range(1, 6)]
    posts.append(_make_message(99, "NONE_DATE_POST", None))

    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return posts

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    html = await generate_channel_html("testchan", client=SimpleNamespace(), limit=3)

    # Exactly 3 posts survive the slice: the 3 NEWEST dated posts.
    assert html.count('class="message-body"') == 3
    # The None-date group sorts oldest (epoch fallback) and is dropped by the slice.
    assert "NONE_DATE_POST" not in html, "None-date group must sort oldest and be trimmed by [:limit] (#59)"
    assert "DATED_5" in html and "DATED_4" in html and "DATED_3" in html
    assert "DATED_1" not in html and "DATED_2" not in html
