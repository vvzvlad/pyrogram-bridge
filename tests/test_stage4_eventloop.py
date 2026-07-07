# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Stage 4 (event-loop hygiene for feed generation) tests.

Covers:
- 4.1 raw_message laziness: feeds do NOT compute str(message); JSON/debug HTML do.
- 4.2 side-effect IO removed from process_message: _save_media_file_ids only appends to
      self._pending_media_ids; the caller flushes once via upsert_media_file_ids_bulk_sync.
      Also: the render path contains NO create_task / get_running_loop / to_thread.
- 4.3 render pipeline moved into a single thread: the four render functions are now plain
      sync functions and actually execute off the main thread; deepcopy of a pickled Message
      does not crash; a 100-message feed generates correctly.
- 4.4 sanitize coverage (XSS): a <script> / onerror= / javascript: payload is stripped in
      ALL outputs — rss, html-feed, single-post html, and json — each with exactly one pass.
"""
import re
import pickle
import asyncio
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pyrogram.enums import MessageMediaType

import post_parser as pp_module
import rss_generator as rss_module
from post_parser import PostParser
from rss_generator import (
    generate_channel_rss,
    generate_channel_html,
    _render_pipeline,
    _compute_time_based_group_ids,
    _create_messages_groups,
    _render_messages_groups,
)

XSS_PAYLOAD = "<script>alert('xss')</script><img src=x onerror=\"alert(1)\"><a href=\"javascript:alert(2)\">click</a>"


class _Str(str):
    """Minimal stand-in for Pyrogram's Str: .html returns the raw string unchanged,
    so a malicious payload reaches the pre-sanitization html body just like real text
    carrying entities would."""
    @property
    def html(self):
        return str(self)


def make_message(mid, text=None, media=None, photo_uid=None, username="testchan",
                 date=None):
    m = SimpleNamespace()
    m.id = mid
    m.date = date or datetime(2024, 1, 1, 12, 0, mid % 60, tzinfo=timezone.utc)
    m.text = _Str(text) if text is not None else None
    m.caption = None
    m.media = media
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
    m.chat = SimpleNamespace(id=-1001234567890, username=username)
    # media sub-objects default to None
    for attr in ("photo", "video", "document", "audio", "voice",
                 "video_note", "animation", "sticker"):
        setattr(m, attr, None)
    if media == MessageMediaType.PHOTO and photo_uid:
        m.photo = SimpleNamespace(file_unique_id=photo_uid)
    return m


def _co_names(func):
    return set(func.__code__.co_names)


# ---------------------------------------------------------------------------
# 4.3 — render functions are plain sync and run off the main thread
# ---------------------------------------------------------------------------

def test_render_functions_are_sync():
    # _trim_messages_groups was inlined into _render_pipeline as a `[:limit]` slice
    # (render-pipeline cosmetics stage); the trimming path is now covered via _render_pipeline.
    for fn in (_compute_time_based_group_ids, _create_messages_groups,
               _render_messages_groups, _render_pipeline):
        assert not asyncio.iscoroutinefunction(fn), f"{fn.__name__} must be a plain sync function"


@pytest.mark.asyncio
async def test_pipeline_runs_in_worker_thread(monkeypatch):
    main_ident = threading.get_ident()
    seen = {}

    real_render = rss_module._render_messages_groups

    def spy(*args, **kwargs):
        seen["ident"] = threading.get_ident()
        return real_render(*args, **kwargs)

    monkeypatch.setattr(rss_module, "_render_messages_groups", spy)

    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return [make_message(i, text=f"post {i}") for i in range(5)]

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    await generate_channel_rss("testchan", client=SimpleNamespace(), limit=5)
    assert "ident" in seen
    assert seen["ident"] != main_ident, "render pipeline must run in a worker thread, not the loop thread"


def test_time_clustering_does_not_mutate_pickled_message():
    # Stage 4: _create_time_based_media_groups (which deep-copied the cached list and
    # MUTATED media_group_id) is gone. Time-clustering is now a PURE mapping function; a
    # pickled Message straight from the cache must come out untouched.
    from pyrogram.types import Message, Chat
    from pyrogram.enums import ChatType
    base = datetime(2024, 1, 1, 12, 0, 0)  # naive, as kurigram emits
    a = pickle.loads(pickle.dumps(Message(
        id=7, date=base, text="hello", media_group_id="orig_A",
        chat=Chat(id=-1001, type=ChatType.CHANNEL, username="testchan"))))
    b = pickle.loads(pickle.dumps(Message(
        id=8, date=base.replace(second=2), text="world", media_group_id=None,
        chat=Chat(id=-1001, type=ChatType.CHANNEL, username="testchan"))))

    mapping = _compute_time_based_group_ids([a, b], merge_seconds=5)

    # The two adjacent posts are clustered under the first truthy id, but only via the
    # RETURNED mapping — the input objects keep their original media_group_id.
    assert mapping == {7: "orig_A", 8: "orig_A"}
    assert a.media_group_id == "orig_A"
    assert b.media_group_id is None


# ---------------------------------------------------------------------------
# 4.2 — no asyncio in the render path; bulk upsert after render
# ---------------------------------------------------------------------------

def test_render_path_has_no_asyncio_side_effects():
    banned = {"create_task", "get_running_loop", "to_thread", "ensure_future"}
    funcs = [
        _render_pipeline, _compute_time_based_group_ids, _create_messages_groups,
        _render_messages_groups,
        PostParser.process_message, PostParser._generate_html_body,
        PostParser._generate_html_media, PostParser.generate_html_footer,
        PostParser._reactions_views_links, PostParser._save_media_file_ids,
        PostParser._sanitize_html,
    ]
    for fn in funcs:
        offenders = _co_names(fn) & banned
        assert not offenders, f"{fn.__qualname__} references forbidden asyncio names: {offenders}"


@pytest.mark.asyncio
async def test_media_ids_persisted_via_bulk_upsert(monkeypatch):
    calls = []

    def fake_bulk(db_path, entries):
        calls.append(list(entries))

    monkeypatch.setattr(pp_module, "upsert_media_file_ids_bulk_sync", fake_bulk)

    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return [
            make_message(1, text="just text"),
            make_message(2, media=MessageMediaType.PHOTO, photo_uid="uid_abc"),
        ]

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    await generate_channel_rss("testchan", client=SimpleNamespace(), limit=10)

    assert len(calls) == 1, "bulk upsert must be called exactly once after render"
    entries = calls[0]
    assert len(entries) == 1, "only the photo message contributes a media id"
    channel, post_id, fid, _ts = entries[0]
    assert (channel, post_id, fid) == ("testchan", 2, "uid_abc")


@pytest.mark.asyncio
async def test_save_media_file_ids_only_appends(monkeypatch):
    # Even with a running loop, _save_media_file_ids must not create tasks — just append.
    parser = PostParser(SimpleNamespace())
    monkeypatch.setattr(pp_module, "upsert_media_file_ids_bulk_sync",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called directly")))
    msg = make_message(3, media=MessageMediaType.PHOTO, photo_uid="uid_x")
    parser._save_media_file_ids(msg)
    assert parser._pending_media_ids == [("testchan", 3, "uid_x", parser._pending_media_ids[0][3])]


# ---------------------------------------------------------------------------
# 4.1 — raw_message laziness
# ---------------------------------------------------------------------------

def test_raw_message_lazy_for_feed():
    parser = PostParser(SimpleNamespace())
    result = parser.process_message(make_message(10, text="hi"), include_raw=False, sanitize=False)
    assert "raw_message" not in result


def test_raw_message_present_for_json_and_debug():
    parser = PostParser(SimpleNamespace())
    result = parser.process_message(make_message(11, text="hi"), include_raw=True)
    assert "raw_message" in result
    assert isinstance(result["raw_message"], str)


@pytest.mark.asyncio
async def test_100_message_feed_generates(monkeypatch):
    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return [make_message(i, text=f"post number {i}") for i in range(1, 101)]

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    rss = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=100)
    assert rss.count("<item>") == 100
    assert "post number 50" in rss


# ---------------------------------------------------------------------------
# 4.4 — XSS: payload stripped in all four outputs
# ---------------------------------------------------------------------------

def _assert_clean(html_str, where):
    assert "<script>" not in html_str, f"{where}: <script> survived"
    assert "onerror" not in html_str, f"{where}: onerror= survived"
    assert "javascript:" not in html_str, f"{where}: javascript: survived"


def _make_client_returning(msg):
    client = SimpleNamespace()

    async def get_messages(channel, post_id):
        return msg

    client.get_messages = get_messages
    return client


@pytest.mark.asyncio
async def test_xss_stripped_in_json():
    msg = make_message(20, text=XSS_PAYLOAD)
    parser = PostParser(_make_client_returning(msg))
    data = await parser.get_post("testchan", 20, "json")
    _assert_clean(data["html"]["body"], "json body")
    _assert_clean(data["html"]["footer"], "json footer")


@pytest.mark.asyncio
async def test_xss_stripped_in_single_post_html():
    msg = make_message(21, text=XSS_PAYLOAD)
    parser = PostParser(_make_client_returning(msg))
    html_out = await parser.get_post("testchan", 21, "html")
    _assert_clean(html_out, "single-post html")


@pytest.mark.asyncio
async def test_xss_stripped_in_single_post_html_debug():
    # debug HTML embeds raw_message (str(message)) into a <pre>; it must be html-escaped so
    # no live tag from the payload survives. The escaped dump legitimately still contains the
    # inert words "onerror"/"javascript:" as text — what matters is that they are NOT live.
    msg = make_message(22, text=XSS_PAYLOAD)
    parser = PostParser(_make_client_returning(msg))
    html_out = await parser.get_post("testchan", 22, "html", debug=True)

    # 1) The rendered display area (everything before the raw <pre>) is fully sanitized.
    display = html_out.split('<pre', 1)[0]
    _assert_clean(display, "single-post debug display")

    # 2) The raw <pre> dump is html-escaped: no live <script> tag anywhere, and the payload
    #    appears only in escaped form (proving html.escape ran).
    assert "<script>" not in html_out, "debug raw dump left a live <script> tag"
    assert "&lt;script&gt;" in html_out, "debug raw dump was not html-escaped"


@pytest.mark.asyncio
async def test_xss_in_title_escaped_in_debug_html():
    # Issue #13: the debug branch of _format_html embeds data["html"]["title"], which is
    # generated from user-controlled content and never passes through bleach. A poll whose
    # question carries a <script> tag yields the title "📊 Poll: <script>alert(1)</script>"
    # verbatim — it must be html-escaped before being embedded in the debug output.
    msg = make_message(25, media=MessageMediaType.POLL)
    msg.poll = SimpleNamespace(question="<script>alert(1)</script>")
    parser = PostParser(_make_client_returning(msg))
    html_out = await parser.get_post("testchan", 25, "html", debug=True)

    # No live <script> tag anywhere in the output (title div, body, footer, raw <pre>).
    assert "<script>" not in html_out, "debug html left a live <script> tag"
    # The title div itself carries the payload only in escaped form (proving html.escape
    # ran on the title, not just on the raw_message <pre> dump).
    title_lines = [line for line in html_out.split("\n") if 'class="title"' in line]
    assert title_lines, "debug output must contain the title div"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in title_lines[0], \
        "title was not html-escaped in debug output"


@pytest.mark.asyncio
async def test_xss_stripped_in_rss_feed(monkeypatch):
    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return [make_message(23, text=XSS_PAYLOAD)]

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    rss = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=5)
    # The sanitized HTML lives in <content:encoded><![CDATA[...]]></content:encoded>.
    cdata = re.findall(r"<!\[CDATA\[(.*?)\]\]>", rss, re.DOTALL)
    assert cdata, "expected CDATA content in RSS"
    for chunk in cdata:
        _assert_clean(chunk, "rss content")


@pytest.mark.asyncio
async def test_xss_stripped_in_html_feed(monkeypatch):
    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return [make_message(24, text=XSS_PAYLOAD)]

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    html_feed = await generate_channel_html("testchan", client=SimpleNamespace(), limit=5)
    _assert_clean(html_feed, "html feed")


def _media_msg_with_payload_caption(mid):
    # A photo message whose CAPTION carries the payload — this exercises the media
    # fragment path (_generate_html_media / caption rendering) whose internal per-fragment
    # sanitize pass was removed in 4.4. The covering pass must still strip it.
    m = make_message(mid, media=MessageMediaType.PHOTO, photo_uid="pic123")
    m.caption = _Str(XSS_PAYLOAD)
    return m


@pytest.mark.asyncio
async def test_xss_in_media_caption_stripped_direct_paths():
    # json + single-post html go through process_message(sanitize=True) directly.
    parser = PostParser(_make_client_returning(_media_msg_with_payload_caption(30)))
    data = await parser.get_post("testchan", 30, "json")
    _assert_clean(data["html"]["body"], "json body (media caption)")
    _assert_clean(data["html"]["footer"], "json footer (media caption)")

    parser2 = PostParser(_make_client_returning(_media_msg_with_payload_caption(31)))
    html_out = await parser2.get_post("testchan", 31, "html")
    _assert_clean(html_out, "single-post html (media caption)")


@pytest.mark.asyncio
async def test_xss_in_media_caption_stripped_in_feeds(monkeypatch):
    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return [_media_msg_with_payload_caption(32)]

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    rss = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=5)
    for chunk in re.findall(r"<!\[CDATA\[(.*?)\]\]>", rss, re.DOTALL):
        _assert_clean(chunk, "rss content (media caption)")

    async def fake_get_history2(client, channel, limit=20):
        return [_media_msg_with_payload_caption(33)]
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history2, raising=False)
    html_feed = await generate_channel_html("testchan", client=SimpleNamespace(), limit=5)
    _assert_clean(html_feed, "html feed (media caption)")


# --------------------------------------------------------------------------- #
# §3.4 — per-post sanitize ISOLATION (stage-1 load-bearing invariant, PR #36).
# A dangling/unbalanced tag in post A must be normalized WITHIN A's own fragment and
# cannot swallow post B (cross-post DOM/XSS bleed). This holds because _render_pipeline
# runs a SEPARATE bleach.clean per post and the formatter joins the <hr> divider AFTER
# sanitize. A FUTURE stage that rejoins BEFORE sanitizing would reintroduce the bleed
# and pass every single-message XSS test — so this 2-post case MUST turn it red.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unbalanced_post_does_not_swallow_next_post_html_feed(monkeypatch):
    # Post A carries a dangling <div><b> with NO closers; post B is well-formed and
    # carries a unique marker. _Str.html feeds the raw tags into the pre-sanitize body
    # exactly as a real message with entities would.
    msg_a = make_message(50, text="<div><b>DANGLING_A no closers here",
                         date=datetime(2024, 1, 1, 12, 5, 0, tzinfo=timezone.utc))
    msg_b = make_message(51, text="INTACT_B_MARKER",
                         date=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return [msg_a, msg_b]  # A is newer than B -> A rendered first

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    html = await generate_channel_html("testchan", client=SimpleNamespace(), limit=5)

    # The divider survives (joined AFTER per-post sanitize) and splits the feed into
    # exactly two TOP-LEVEL fragments. A join-before-sanitize regression strips the
    # non-whitelisted <hr> entirely (strip=True) -> this alone already goes red.
    parts = html.split('<hr class="post-divider">')
    assert len(parts) == 2, "expected exactly one top-level post-divider between the two posts"
    frag_a, frag_b = parts

    # A's dangling tags were balanced WITHIN A's own fragment, NOT deferred past the
    # divider. A rejoin-before-sanitize regression closes them only at the very end of
    # the whole feed (after B), leaving frag_a with more opens than closes.
    assert "DANGLING_A" in frag_a
    assert frag_a.count("<div") == frag_a.count("</div>"), "post A's <div> not closed within its own fragment"
    assert frag_a.count("<b>") == frag_a.count("</b>"), "post A's <b> not closed within its own fragment"

    # B is intact, lives at top level, and is itself balanced — never nested inside A
    # (its marker must not have been trapped before A's divider).
    assert "INTACT_B_MARKER" in frag_b, "post B content missing/swallowed by post A"
    assert "INTACT_B_MARKER" not in frag_a, "post B content bled into post A's fragment"
    assert frag_b.count("<div") == frag_b.count("</div>"), "post B fragment not self-contained"


# --------------------------------------------------------------------------- #
# Review round-1: the new bulk-upsert SQL executed for real (not mocked).
# --------------------------------------------------------------------------- #
def test_bulk_upsert_media_file_ids_real_sql(tmp_path):
    import sqlite3
    from file_io import upsert_media_file_ids_bulk_sync, init_db_sync

    db = str(tmp_path / "t.db")
    init_db_sync(db)

    # Empty list is a no-op (no crash).
    upsert_media_file_ids_bulk_sync(db, [])

    # Multi-row insert.
    upsert_media_file_ids_bulk_sync(db, [
        ("chA", 1, "fidA", 100.0),
        ("chB", 2, "fidB", 200.0),
    ])
    conn = sqlite3.connect(db)
    rows = dict(((c, p, f), a) for c, p, f, a in
                conn.execute("SELECT channel, post_id, file_unique_id, added FROM media_file_ids"))
    assert rows[("chA", 1, "fidA")] == 100.0
    assert rows[("chB", 2, "fidB")] == 200.0

    # Re-upsert the SAME key updates `added` (ON CONFLICT ... DO UPDATE SET added=excluded.added).
    upsert_media_file_ids_bulk_sync(db, [("chA", 1, "fidA", 999.0)])
    a = conn.execute(
        "SELECT added FROM media_file_ids WHERE channel='chA' AND post_id=1 AND file_unique_id='fidA'"
    ).fetchone()[0]
    assert a == 999.0
    conn.close()


# --------------------------------------------------------------------------- #
# Review round-1: media ids collected before a render exception are still
# flushed (the flush is in a finally). Removing the finally must break this.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pending_media_flushed_on_render_exception(monkeypatch):
    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return [make_message(40, text="hi")]

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    # A render pipeline that collects a pending id then raises mid-render.
    def boom_pipeline(messages, post_parser, *a, **k):
        post_parser._pending_media_ids.append(("chZ", 9, "fidZ", 1.0))
        raise RuntimeError("render blew up")

    monkeypatch.setattr(rss_module, "_render_pipeline", boom_pipeline)

    flushed = {}
    async def fake_bulk(db, entries):
        flushed["entries"] = list(entries)
    monkeypatch.setattr("post_parser.upsert_media_file_ids_bulk_sync",
                        lambda db, entries: flushed.__setitem__("entries", list(entries)),
                        raising=False)

    with pytest.raises(Exception):
        await generate_channel_rss("testchan", client=SimpleNamespace(), limit=5)

    # The collected id was persisted despite the render exception (flush in finally).
    assert flushed.get("entries") == [("chZ", 9, "fidZ", 1.0)]


# --------------------------------------------------------------------------- #
# Review round-1 [security]: if the ONLY sanitize pass throws, the feed must
# FAIL CLOSED (html.escape the raw content), never emit the raw XSS payload.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rss_fails_closed_when_sanitizer_raises(monkeypatch):
    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return [make_message(41, text=XSS_PAYLOAD)]

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    # Force bleach to blow up (e.g. the RecursionError class already seen in prod).
    # API relocation: the single bleach config now lives in sanitizer.py, which imports
    # it as `from bleach import clean as HTMLSanitizer`; sanitize_html resolves the name
    # at call time, so patching sanitizer.HTMLSanitizer triggers the fail-closed path.
    import sanitizer as sanitizer_module
    def boom(*a, **k):
        raise RecursionError("bleach exploded")
    monkeypatch.setattr(sanitizer_module, "HTMLSanitizer", boom, raising=True)

    rss = await generate_channel_rss("testchan", client=SimpleNamespace(), limit=5)
    chunks = re.findall(r"<!\[CDATA\[(.*?)\]\]>", rss, re.DOTALL)
    assert chunks, "expected CDATA content"
    for chunk in chunks:
        # Fail-closed: the raw payload was html.escaped, so NO live tag survived — every
        # `<` became `&lt;`. (The letters "javascript:" still appear, but as inert text
        # inside an escaped &quot;…&quot;, not a live href.)
        assert "<script" not in chunk, "RSS fail-open: raw <script> reached the feed"
        assert "<img" not in chunk, "RSS fail-open: raw <img onerror> reached the feed"
        assert "<a " not in chunk, "RSS fail-open: raw <a href> reached the feed"
        # ...and the escaping actually ran (payload present as escaped text, not dropped).
        assert "&lt;script&gt;" in chunk, "expected the payload html-escaped, not dropped"
