# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
"""Rich Messages phase 3/3 (#86) — part=True full-content re-fetch via messages.GetRichMessage.

Covers the mandated matrix (mock client, non-vacuous):
  (а) part=True -> re-fetch -> FULL tree in the snapshot (memo invalidated).
  (б) re-fetch failed -> partial tree kept + part=True -> plaque in the rendered HTML.
  (в) part=False -> ZERO re-fetches (call count 0).
  (г) breaker: FloodWait on the first post -> the rest are NOT re-fetched, snapshot still written.
  (д) breaker: a timeout on the first post stops the rest.
  (е) /media download cascade: fid missing -> ONE GetRichMessage -> resolve;
      re-fetch failure -> 503 + row alive; success+clean+part==False+fid-absent -> 404 + row deleted.
  (ж) memo 60s: two fids of one post -> ONE RPC.
  (з) sentinel with part=True -> re-fetch fixes the post.

Plus a low-level lock on safe_get_rich_message's typed result (the breaker keys on it).
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pyrogram import errors

import rich_tree
import tg_cache
import message_snapshot as ms
import telegram_client
from telegram_client import RichFetchResult, safe_get_rich_message
from post_parser import PostParser


# --------------------------------------------------------------------------------------
# Rich object + message factories (mirror the parity corpus used in test_rich_pipeline).
# --------------------------------------------------------------------------------------
def node(clsname, **fields):
    obj = type(clsname, (), {})()
    for k, v in fields.items():
        setattr(obj, k, v)
    return obj


def media_obj(fid, file_size=None, typename="Photo"):
    obj = type(typename, (), {})()
    obj.file_unique_id = fid
    obj.file_id = "FID_" + fid
    obj.file_size = file_size
    obj.mime_type = None
    return obj


def partial_rich(part=True):
    """A partial rich message: a lone heading, no media, part flag set."""
    return SimpleNamespace(blocks=[node("RichBlockSectionHeading", size=1, text="Teaser")],
                           is_rtl=False, part=part)


def full_rich(fid="ph1"):
    """The 'fully-fetched' object: heading + paragraph + a photo, part=False."""
    return SimpleNamespace(
        blocks=[
            node("RichBlockSectionHeading", size=1, text="Full Title"),
            node("RichBlockParagraph", text="The complete body text."),
            node("RichBlockPhoto", photo=media_obj(fid, file_size=1000, typename="Photo"), caption=None),
        ],
        is_rtl=False, part=False,
    )


def sentinel_rich(part=True):
    """A #84 parse_failed sentinel carrying the part flag (empty blocks)."""
    return SimpleNamespace(blocks=[], is_rtl=False, part=part, parse_failed=True)


_CHAT = SimpleNamespace(id=-1001234500000, username="richchan", title="Rich Channel", usernames=None)

_MSG_DEFAULTS = dict(
    id=555, date=datetime(2026, 7, 20, 12, 0, 0), text=None, caption=None, media=None,
    service=None, media_group_id=None, views=10, show_caption_above_media=False,
    reply_to_message_id=None, reply_to_message=None, empty=False, chat=_CHAT,
    sender_chat=None, from_user=None, forward_origin=None, reactions=None, poll=None,
    web_page=None, photo=None, video=None, document=None, audio=None, voice=None,
    video_note=None, animation=None, sticker=None, story=None, contact=None,
    location=None, venue=None, dice=None, game=None, giveaway=None,
    giveaway_winners=None, checklist=None, paid_media=None, live_photo=None,
)


def make_msg(**overrides):
    fields = dict(_MSG_DEFAULTS)
    fields.update(overrides)
    return SimpleNamespace(**fields)


class FakeFetcher:
    """Records each (chat_id, msg_id) and returns a scripted RichFetchResult per call index.

    Replaces telegram_client.safe_get_rich_message wherever it is looked up (tg_cache /
    api_server). The transport `client` arg is ignored — no real RPC is issued.
    """
    def __init__(self, scripts):
        # scripts: list[RichFetchResult]; the last entry repeats once exhausted.
        self._scripts = list(scripts)
        self.calls = []

    async def __call__(self, client, chat_id, msg_id, timeout=telegram_client.RICH_ENRICH_RPC_TIMEOUT):
        self.calls.append((chat_id, msg_id))
        idx = min(len(self.calls) - 1, len(self._scripts) - 1)
        return self._scripts[idx]

    @property
    def count(self):
        return len(self.calls)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    rich_tree.reset_counters()
    telegram_client.reset_rich_fetch_counters()
    yield


# ======================================================================================
# (а) part=True -> re-fetch -> FULL tree in the snapshot (memo invalidation is load-bearing).
# ======================================================================================
async def test_a_part_true_refetch_produces_full_tree_in_snapshot(monkeypatch):
    msg = make_msg(rich_message=partial_rich(part=True))

    # Prime the memo with the PARTIAL tree BEFORE enrichment. Without invalidate_tree_memo
    # this stale tree would survive the re-fetch — so asserting the post-tree differs proves
    # the invalidation, not just the replacement.
    pre = rich_tree.tree_of(msg)
    assert pre["part"] is True
    assert len(pre["blocks"]) == 1

    fetcher = FakeFetcher([RichFetchResult(full_rich("ph1"), "ok")])
    monkeypatch.setattr(tg_cache, "safe_get_rich_message", fetcher)

    out = await tg_cache.enrich_rich_parts(MagicMock(), [msg])

    assert fetcher.count == 1
    # The live object was replaced and the memo invalidated -> tree_of now recomputes FULL.
    post = rich_tree.tree_of(msg)
    assert post["part"] is False
    assert len(post["blocks"]) == 3
    media = list(rich_tree.iter_tree_media(post))
    assert [m["fid"] for m in media] == ["ph1"]

    # And the snapshot serialises the FULL tree (this is what the feed cache stores).
    snap = ms.snapshot_messages(out)[0]
    assert snap["rich_tree"]["part"] is False
    assert len(snap["rich_tree"]["blocks"]) == 3


# ======================================================================================
# (б) re-fetch failed -> partial tree kept + part=True -> plaque in the HTML.
# ======================================================================================
async def test_b_refetch_failed_keeps_partial_and_renders_plaque(monkeypatch):
    original = partial_rich(part=True)
    msg = make_msg(rich_message=original)

    fetcher = FakeFetcher([RichFetchResult(None, "error")])
    monkeypatch.setattr(tg_cache, "safe_get_rich_message", fetcher)

    await tg_cache.enrich_rich_parts(MagicMock(), [msg])

    assert fetcher.count == 1
    # Failure leaves the partial object untouched (still part=True).
    assert msg.rich_message is original
    tree = rich_tree.tree_of(msg)
    assert tree["part"] is True
    # The render surfaces the honest "partial post" plaque.
    body = PostParser(MagicMock())._generate_html_body(msg)
    assert "Only part of this post is shown" in body


# ======================================================================================
# (в) part=False -> ZERO re-fetches.
# ======================================================================================
async def test_c_part_false_makes_no_refetch(monkeypatch):
    msgs = [make_msg(id=1, rich_message=full_rich("a")),
            make_msg(id=2, rich_message=full_rich("b")),
            make_msg(id=3, rich_message=None)]  # non-rich too
    fetcher = FakeFetcher([RichFetchResult(full_rich(), "ok")])
    monkeypatch.setattr(tg_cache, "safe_get_rich_message", fetcher)

    await tg_cache.enrich_rich_parts(MagicMock(), msgs)

    assert fetcher.count == 0, "a complete (part=False) rich post must never be re-fetched"


# ======================================================================================
# (г) breaker: FloodWait on the first -> rest NOT re-fetched; snapshot STILL written.
# ======================================================================================
async def test_d_floodwait_breaker_stops_rest_and_snapshot_still_written(monkeypatch):
    msgs = [make_msg(id=10, rich_message=partial_rich()),
            make_msg(id=11, rich_message=partial_rich()),
            make_msg(id=12, rich_message=partial_rich())]

    # First call floods; any later call (which must NOT happen) would succeed.
    fetcher = FakeFetcher([RichFetchResult(None, "floodwait"),
                           RichFetchResult(full_rich(), "ok")])
    monkeypatch.setattr(tg_cache, "safe_get_rich_message", fetcher)

    saved = {}

    def fake_save(channel_id, messages, limit):
        saved["messages"] = messages
        saved["limit"] = limit

    async def fake_history(channel, limit=20):
        # async generator surrogate the way client.get_chat_history is consumed
        # (attribute on a SimpleNamespace -> called unbound, no self/client arg).
        for m in msgs:
            yield m

    async def fake_reply(client, messages):
        return messages

    monkeypatch.setattr(tg_cache, "_get_history_from_cache", lambda *a, **k: None)
    monkeypatch.setattr(tg_cache, "_reply_enrichment", fake_reply)
    monkeypatch.setattr(tg_cache, "_save_history_to_cache", fake_save)

    client = SimpleNamespace(get_chat_history=fake_history)
    result = await tg_cache.cached_get_chat_history(client, "richchan", limit=3)

    # Breaker: exactly ONE re-fetch (the flooded first post), the other two are not attempted.
    assert fetcher.count == 1
    # Snapshot is ALWAYS written even though the breaker tripped (partial posts keep the plaque).
    assert "messages" in saved and len(saved["messages"]) == 3
    assert result is not None


# ======================================================================================
# (д) breaker: a timeout on the first post stops the rest.
# ======================================================================================
async def test_e_timeout_breaker_stops_rest(monkeypatch):
    msgs = [make_msg(id=20, rich_message=partial_rich()),
            make_msg(id=21, rich_message=partial_rich())]
    fetcher = FakeFetcher([RichFetchResult(None, "timeout"),
                           RichFetchResult(full_rich(), "ok")])
    monkeypatch.setattr(tg_cache, "safe_get_rich_message", fetcher)

    await tg_cache.enrich_rich_parts(MagicMock(), msgs)

    assert fetcher.count == 1, "a timeout on the first post must stop the whole render's enrichment"
    assert msgs[1].rich_message.part is True  # second post left partial


async def test_e2_generic_error_skips_only_its_post(monkeypatch):
    # F5: a generic (non-flood, non-timeout) error must NOT trip the breaker — it skips ONLY
    # its own post and enrichment continues. [error, ok] -> both attempted (count==2), and the
    # second post IS enriched while the first keeps its partial tree + plaque.
    m1 = make_msg(id=30, rich_message=partial_rich())
    m2 = make_msg(id=31, rich_message=partial_rich())
    fetcher = FakeFetcher([RichFetchResult(None, "error"),
                           RichFetchResult(full_rich("e2"), "ok")])
    monkeypatch.setattr(tg_cache, "safe_get_rich_message", fetcher)

    await tg_cache.enrich_rich_parts(MagicMock(), [m1, m2])

    assert fetcher.count == 2, "a generic error must skip only its post, not stop the render"
    assert rich_tree.tree_of(m1)["part"] is True   # first: skipped, still partial
    assert rich_tree.tree_of(m2)["part"] is False  # second: enriched despite the earlier error


# ======================================================================================
# (з) sentinel with part=True -> re-fetch fixes the post.
# ======================================================================================
async def test_z_sentinel_part_true_is_refetched_and_fixed(monkeypatch):
    from kurigram_compat import has_parse_failures
    sent = sentinel_rich(part=True)
    msg = make_msg(rich_message=sent)
    assert has_parse_failures(sent) is True  # pre: a crashed parse

    fetcher = FakeFetcher([RichFetchResult(full_rich("z1"), "ok")])
    monkeypatch.setattr(tg_cache, "safe_get_rich_message", fetcher)

    await tg_cache.enrich_rich_parts(MagicMock(), [msg])

    assert fetcher.count == 1
    assert msg.rich_message is not sent
    tree = rich_tree.tree_of(msg)
    assert tree["part"] is False and len(tree["blocks"]) == 3  # post: fixed, full content


# ======================================================================================
# (е) /media download cascade + (ж) memo.
# ======================================================================================
@pytest.fixture
def _dl_env(monkeypatch, tmp_path):
    import api_server
    api_server._rich_refetch_memo.clear()
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "cache"))

    removed = []

    def fake_remove(db_path, keys):
        removed.extend(keys)

    monkeypatch.setattr(api_server, "remove_media_file_ids_sync", fake_remove)
    return api_server, removed


async def test_f1_cascade_resolves_after_refetch(_dl_env, monkeypatch):
    api_server, removed = _dl_env
    # Live post: partial rich, the requested fid ('ph1') is NOT present yet.
    msg = make_msg(rich_message=partial_rich(part=True), media=None, video=None)

    async def fake_get_messages(channel_id, post_id):
        return msg

    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get_messages)
    # The re-fetch returns the full object which DOES carry 'ph1'.
    fetcher = FakeFetcher([RichFetchResult(full_rich("ph1"), "ok")])
    monkeypatch.setattr(api_server, "safe_get_rich_message", fetcher)

    async def fake_atomic(file_id, path, timeout):
        assert file_id == "FID_ph1"  # resolved from the enriched object
        return "DOWNLOADED"

    monkeypatch.setattr(api_server, "_download_atomic", fake_atomic)

    got = await api_server.download_media_file("richchan", 555, "ph1")
    assert got == "DOWNLOADED"
    assert fetcher.count == 1
    assert removed == [], "a resolved fid must not delete the row"


async def test_f2_refetch_failure_is_503_and_row_survives(_dl_env, monkeypatch):
    from fastapi import HTTPException
    api_server, removed = _dl_env
    msg = make_msg(rich_message=partial_rich(part=True), media=None, video=None)

    async def fake_get_messages(channel_id, post_id):
        return msg

    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get_messages)
    fetcher = FakeFetcher([RichFetchResult(None, "error")])  # re-fetch fails
    monkeypatch.setattr(api_server, "safe_get_rich_message", fetcher)

    with pytest.raises(HTTPException) as ei:
        await api_server.download_media_file("richchan", 555, "ph1")
    assert ei.value.status_code == 503
    assert removed == [], "a transient re-fetch failure must NOT delete the SQLite row"


async def test_f3_clean_complete_absent_is_404_and_row_deleted(_dl_env, monkeypatch):
    from fastapi import HTTPException
    api_server, removed = _dl_env
    msg = make_msg(rich_message=partial_rich(part=True), media=None, video=None)

    async def fake_get_messages(channel_id, post_id):
        return msg

    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get_messages)
    # Re-fetch yields a COMPLETE, cleanly-parsed object (part=False, no parse_failed) that
    # still lacks the requested fid 'ghost' -> genuinely gone -> permanent 404 + row deletion.
    fetcher = FakeFetcher([RichFetchResult(full_rich("ph1"), "ok")])
    monkeypatch.setattr(api_server, "safe_get_rich_message", fetcher)

    with pytest.raises(HTTPException) as ei:
        await api_server.download_media_file("richchan", 555, "ghost")
    assert ei.value.status_code == 404
    assert removed == [("richchan", 555, "ghost")], "a clean-but-absent fid must delete the row"


async def test_f4_clean_but_part_true_is_503_and_row_survives(_dl_env, monkeypatch):
    # Guards the `part==False` clause of the delete+404 decision: a re-fetch that parses
    # CLEANLY (no parse_failed) but is STILL partial (part=True) and lacks the fid must be
    # transient (503, row alive) — not a permanent 404. Remove the clause and this reddens.
    from fastapi import HTTPException
    api_server, removed = _dl_env
    msg = make_msg(rich_message=partial_rich(part=True), media=None, video=None)

    async def fake_get_messages(channel_id, post_id):
        return msg

    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get_messages)
    clean_but_partial = full_rich("ph1")
    clean_but_partial.part = True  # clean parse, still only PART of the post
    fetcher = FakeFetcher([RichFetchResult(clean_but_partial, "ok")])
    monkeypatch.setattr(api_server, "safe_get_rich_message", fetcher)

    with pytest.raises(HTTPException) as ei:
        await api_server.download_media_file("richchan", 555, "ghost")
    assert ei.value.status_code == 503
    assert removed == [], "a still-partial (part=True) re-fetch must NOT delete the row"


async def test_f5_sentinel_refetch_is_503_and_row_survives(_dl_env, monkeypatch):
    # Guards the `not has_parse_failures(...)` clause: a re-fetch that came back as a #84
    # sentinel (parse_failed=True) — even with part=False — must be transient (503, row alive),
    # because the parse crashed and the media may still be there. Remove the clause -> reddens.
    from fastapi import HTTPException
    api_server, removed = _dl_env
    msg = make_msg(rich_message=partial_rich(part=True), media=None, video=None)

    async def fake_get_messages(channel_id, post_id):
        return msg

    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get_messages)
    fetcher = FakeFetcher([RichFetchResult(sentinel_rich(part=False), "ok")])
    monkeypatch.setattr(api_server, "safe_get_rich_message", fetcher)

    with pytest.raises(HTTPException) as ei:
        await api_server.download_media_file("richchan", 555, "ghost")
    assert ei.value.status_code == 503
    assert removed == [], "a parse_failed sentinel re-fetch must NOT delete the row"


async def test_g_memo_collapses_two_fids_into_one_rpc(_dl_env, monkeypatch):
    from fastapi import HTTPException
    api_server, removed = _dl_env

    def new_msg():
        return make_msg(rich_message=partial_rich(part=True), media=None, video=None)

    async def fake_get_messages(channel_id, post_id):
        return new_msg()

    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get_messages)
    # A failing re-fetch is memoised too, so a burst over one post fires exactly ONE RPC.
    fetcher = FakeFetcher([RichFetchResult(None, "error")])
    monkeypatch.setattr(api_server, "safe_get_rich_message", fetcher)

    for fid in ("fidA", "fidB"):  # two DIFFERENT media of the SAME (channel, post)
        with pytest.raises(HTTPException):
            await api_server.download_media_file("richchan", 555, fid)

    assert fetcher.count == 1, "two fids of one post must share ONE GetRichMessage re-fetch (sequential/TTL memo)"


async def test_h_parallel_fids_coalesce_into_one_rpc(_dl_env, monkeypatch):
    # F1: CONCURRENT downloads of two media of the same part post (the real parallel page
    # load — the download dedup key includes fid, so they run at once) must share ONE
    # GetRichMessage via the in-flight coalescing, not fire N. The TTL memo alone cannot do
    # this (it is written only AFTER the awaited RPC completes).
    import asyncio
    api_server, removed = _dl_env
    from fastapi import HTTPException

    async def fake_get_messages(channel_id, post_id):
        return make_msg(rich_message=partial_rich(part=True), media=None, video=None)

    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get_messages)

    started = 0

    async def slow_fetch(client, chat_id, msg_id, timeout=telegram_client.RICH_ENRICH_RPC_TIMEOUT):
        nonlocal started
        started += 1
        await asyncio.sleep(0.05)  # hold the RPC open so the sibling request overlaps it
        return RichFetchResult(None, "error")

    monkeypatch.setattr(api_server, "safe_get_rich_message", slow_fetch)

    async def one(fid):
        with pytest.raises(HTTPException):
            await api_server.download_media_file("richchan", 555, fid)

    await asyncio.gather(one("fidA"), one("fidB"))
    assert started == 1, "concurrent fids of one post must coalesce into ONE GetRichMessage"


# ======================================================================================
# Low-level lock: safe_get_rich_message typed result + counters (the breaker keys on these).
# ======================================================================================
class _FakeRawClient:
    def __init__(self, invoke_impl):
        self._invoke_impl = invoke_impl

    async def resolve_peer(self, chat_id):
        return SimpleNamespace(peer=chat_id)

    async def invoke(self, request):
        return await self._invoke_impl(request)


async def test_safe_fetch_ok_returns_parsed(monkeypatch):
    raw_rich = object()
    messages = SimpleNamespace(messages=[SimpleNamespace(rich_message=raw_rich)])

    async def invoke(_req):
        return messages

    marker = SimpleNamespace(blocks=[], part=False)

    async def fake_parse(client, rm):
        assert rm is raw_rich
        return marker

    monkeypatch.setattr(telegram_client.types.RichMessage, "_parse", staticmethod(fake_parse))

    res = await safe_get_rich_message(_FakeRawClient(invoke), -100, 5)
    assert res.outcome == "ok" and res.rich_message is marker
    assert telegram_client.get_rich_part_fetch_attempt_count() == 1
    assert telegram_client.get_rich_part_fetch_failed_count() == 0


async def test_safe_fetch_floodwait_is_typed():
    async def invoke(_req):
        raise errors.FloodWait(value=9)

    res = await safe_get_rich_message(_FakeRawClient(invoke), -100, 5)
    assert res.outcome == "floodwait" and res.rich_message is None
    assert telegram_client.get_rich_part_fetch_failed_count() == 1


async def test_safe_fetch_timeout_is_typed():
    import asyncio

    async def invoke(_req):
        await asyncio.sleep(0.2)

    res = await safe_get_rich_message(_FakeRawClient(invoke), -100, 5, timeout=0.01)
    assert res.outcome == "timeout" and res.rich_message is None


async def test_safe_fetch_generic_error_is_typed():
    async def invoke(_req):
        raise RuntimeError("boom")

    res = await safe_get_rich_message(_FakeRawClient(invoke), -100, 5)
    assert res.outcome == "error" and res.rich_message is None
