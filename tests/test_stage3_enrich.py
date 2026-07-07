# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
"""
Stage 3 (render-pipeline refactor epic, issue #30/#34) — reply-enrichment lock.

Stage 3 merges generate_channel_rss/generate_channel_html around the shared
_prepare_feed_posts and moves _reply_enrichment (the live client.get_messages
reply-target fetch) INTO that shared path (enrich_replies=True for HTML,
False for RSS).

The stage-0 golden oracle CANNOT see this move: its harness replays the corpus
through monkeypatched tg_cache but leaves client.get_messages a no-op (the client
is a bare SimpleNamespace, so get_messages raises AttributeError and enrichment
silently no-ops). So enrichment is locked here instead, exactly as mandated on #30:

  (a) get_messages is BATCHED by chat_id (one call per chat, not one per reply);
  (b) the fetched reply target is set onto the right source message by
      (chat_id, message.id);
  (c) the enriched reply block actually renders in the HTML feed.

Plus: RSS deliberately does NOT enrich (keeps polling cheap) — locked too.
"""
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from pyrogram import errors

import rss_generator as rss_module
from rss_generator import _reply_enrichment, generate_channel_html, generate_channel_rss


class _Str(str):
    """Stand-in for Pyrogram's Str: .html returns the raw string unchanged, so a
    reply-target text reaches the pre-sanitize body like real entity text would."""
    @property
    def html(self):
        return str(self)


def _reply_target(mid, text):
    """A resolved reply-to-message as _format_reply_info consumes it."""
    return SimpleNamespace(id=mid, text=text, caption=None, sender_chat=None)


def make_message(mid, chat_id=-1001234567890, username="testchan", text="post",
                 reply_to_message_id=None, reply_to_message=None, date=None):
    m = SimpleNamespace()
    m.id = mid
    m.date = date or datetime(2024, 1, 1, 12, 0, mid % 60, tzinfo=timezone.utc)
    m.text = _Str(text) if text is not None else None
    m.caption = None
    m.media = None
    m.web_page = None
    m.poll = None
    m.service = None
    m.forward_origin = None
    m.reply_to_message = reply_to_message
    m.reply_to_message_id = reply_to_message_id
    m.sender_chat = None
    m.from_user = None
    m.reactions = None
    m.views = 100
    m.media_group_id = None
    m.show_caption_above_media = False
    m.chat = SimpleNamespace(id=chat_id, username=username)
    for attr in ("photo", "video", "document", "audio", "voice",
                 "video_note", "animation", "sticker"):
        setattr(m, attr, None)
    return m


class RecordingClient:
    """Fake Telegram client: records each get_messages(chat_id, ids) call and returns,
    for every requested id, a 'full' message carrying a resolved .reply_to_message."""
    def __init__(self, target_text_for=lambda chat_id, mid: f"TARGET_{chat_id}_{mid}"):
        self.calls = []                 # list[(chat_id, list[ids])]
        self._target_text_for = target_text_for

    async def get_messages(self, chat_id, ids):
        self.calls.append((chat_id, list(ids)))
        out = []
        for mid in ids:
            full = SimpleNamespace(
                id=mid,
                empty=False,
                reply_to_message=_reply_target(mid + 5000, self._target_text_for(chat_id, mid)),
            )
            out.append(full)
        return out


# --------------------------------------------------------------------------- #
# (a) batching by chat_id  +  (b) target set onto the right source message.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reply_enrichment_batches_by_chat_and_sets_target():
    CHAT_A, CHAT_B = -100111, -100222
    messages = [
        make_message(10, chat_id=CHAT_A, reply_to_message_id=1),
        make_message(11, chat_id=CHAT_A, reply_to_message_id=2),
        make_message(12, chat_id=CHAT_A, reply_to_message_id=3),
        make_message(20, chat_id=CHAT_B, reply_to_message_id=4),
        make_message(21, chat_id=CHAT_B, reply_to_message_id=5),
        make_message(30, chat_id=CHAT_A, reply_to_message_id=None),  # no reply -> not fetched
    ]
    client = RecordingClient()

    result = await _reply_enrichment(client, messages)

    # (a) BATCHED: exactly one call per chat_id (2), NOT one per reply (5).
    assert len(client.calls) == 2, f"expected 2 batched calls, got {client.calls}"
    calls_by_chat = dict(client.calls)
    assert calls_by_chat[CHAT_A] == [10, 11, 12], "chat A ids not batched into one call"
    assert calls_by_chat[CHAT_B] == [20, 21], "chat B ids not batched into one call"

    # (b) each fetched target is set onto the SOURCE message keyed by (chat_id, id).
    by_id = {m.id: m for m in result}
    for mid, chat in [(10, CHAT_A), (11, CHAT_A), (12, CHAT_A), (20, CHAT_B), (21, CHAT_B)]:
        assert by_id[mid].reply_to_message is not None, f"message {mid} not enriched"
        assert by_id[mid].reply_to_message.text == f"TARGET_{chat}_{mid}", \
            f"message {mid} got the wrong reply target"
    # The reply-less message is untouched.
    assert by_id[30].reply_to_message is None


@pytest.mark.asyncio
async def test_reply_enrichment_no_replies_makes_no_calls():
    messages = [make_message(1), make_message(2)]  # no reply_to_message_id
    client = RecordingClient()
    await _reply_enrichment(client, messages)
    assert client.calls == [], "get_messages must not be called when nothing needs enrichment"


# --------------------------------------------------------------------------- #
# (c) the enriched reply block renders in the HTML feed — AND enrichment now runs
#     inside the shared _prepare_feed_posts path (enrich_replies=True for HTML).
# --------------------------------------------------------------------------- #
def _patch_feed_source(monkeypatch, messages):
    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def fake_get_history(client, channel, limit=20):
        return messages

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)


@pytest.mark.asyncio
async def test_html_feed_renders_enriched_reply_block(monkeypatch):
    MARKER = "ENRICHED_REPLY_MARKER_XYZ"
    # A shallow message: it has a reply_to_message_id but NO resolved reply_to_message
    # yet — enrichment must fetch and fill it before render.
    msg = make_message(77, chat_id=-1001234567890, reply_to_message_id=70,
                       reply_to_message=None, text="body text")
    _patch_feed_source(monkeypatch, [msg])

    client = RecordingClient(target_text_for=lambda chat_id, mid: MARKER)
    html = await generate_channel_html("testchan", client=client, limit=5)

    # Enrichment ran inside _prepare_feed_posts and was BATCHED (one call for the chat).
    assert client.calls == [(-1001234567890, [77])], f"enrichment not run/batched: {client.calls}"
    # The resolved reply target renders as a reply block carrying the marker text.
    assert '<div class="message-reply">' in html, "reply block not rendered"
    assert MARKER in html, "resolved reply-target text missing from the rendered feed"


@pytest.mark.asyncio
async def test_rss_feed_does_not_enrich_replies(monkeypatch):
    # RSS deliberately skips enrichment (enrich_replies=False) to keep polling cheap.
    msg = make_message(88, chat_id=-1001234567890, reply_to_message_id=80,
                       reply_to_message=None, text="body text")
    _patch_feed_source(monkeypatch, [msg])

    client = RecordingClient()
    await generate_channel_rss("testchan", client=client, limit=5)
    assert client.calls == [], "RSS must NOT call get_messages (enrich_replies=False)"


# --------------------------------------------------------------------------- #
# History over-fetch fork: RSS over-fetches limit*2 (headroom for grouping/trim),
# HTML fetches exactly limit. This is a REAL behavioral fork that the golden oracle
# CANNOT see (its fake_get_chat_history ignores the limit kwarg and returns the whole
# corpus, so both paths trim to GOLDEN_LIMIT identically). A refactor that accidentally
# unified the two would pass every golden test — so lock the forwarded limit here.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rss_overfetches_history_html_does_not(monkeypatch):
    N = 7
    seen = {}

    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def spy_get_history(client, channel, limit=20):
        seen.setdefault("limits", []).append(limit)
        return []

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", spy_get_history, raising=False)

    await generate_channel_rss("testchan", client=SimpleNamespace(), limit=N)
    assert seen["limits"] == [2 * N], f"RSS must over-fetch history limit*2, got {seen['limits']}"

    seen["limits"] = []
    await generate_channel_html("testchan", client=SimpleNamespace(), limit=N)
    assert seen["limits"] == [N], f"HTML must fetch history limit (no over-fetch), got {seen['limits']}"


# --------------------------------------------------------------------------- #
# Shared error handling in _prepare_feed_posts, verified through BOTH formatters.
# The golden oracle cannot see these paths (§3.9/§3.10 are error branches), so they
# are locked here. FEED_FUNCS lets each case assert RSS and HTML behave identically.
# --------------------------------------------------------------------------- #
FEED_FUNCS = [generate_channel_rss, generate_channel_html]


@pytest.mark.asyncio
@pytest.mark.parametrize("feed_func", FEED_FUNCS)
async def test_channel_not_found_returns_error_feed(monkeypatch, feed_func):
    async def raise_not_found(client, channel):
        raise errors.UsernameNotOccupied("no such user")

    async def fake_get_history(client, channel, limit=20):
        return []

    monkeypatch.setattr("tg_cache.cached_get_chat", raise_not_found, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    out = await feed_func("ghostchan", client=SimpleNamespace(), limit=5)
    # ChannelNotFound -> create_error_feed (RSS-XML error feed) in both paths.
    assert "does not exist" in out, f"{feed_func.__name__} did not return the error feed"
    assert "ghostchan" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("feed_func", FEED_FUNCS)
async def test_floodwait_from_get_chat_propagates(monkeypatch, feed_func):
    async def raise_flood(client, channel):
        raise errors.FloodWait(value=11)

    async def fake_get_history(client, channel, limit=20):
        return []

    monkeypatch.setattr("tg_cache.cached_get_chat", raise_flood, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_history, raising=False)

    # FloodWait must reach api_server unwrapped (mapped there to HTTP 429), NOT ValueError.
    with pytest.raises(errors.FloodWait):
        await feed_func("floodchan", client=SimpleNamespace(), limit=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("feed_func", FEED_FUNCS)
async def test_floodwait_from_history_propagates(monkeypatch, feed_func):
    # Registry §3.9 (the fix this stage lands): FloodWait raised while fetching HISTORY
    # must propagate -> HTTP 429. Before stage 3 it fell into `except Exception` and was
    # wrapped in ValueError -> HTTP 400. The golden cannot see this; locked here.
    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def raise_flood_history(client, channel, limit=20):
        raise errors.FloodWait(value=13)

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", raise_flood_history, raising=False)

    with pytest.raises(errors.FloodWait):
        await feed_func("testchan", client=SimpleNamespace(), limit=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("feed_func", FEED_FUNCS)
async def test_other_history_error_becomes_valueerror(monkeypatch, feed_func):
    # Any NON-FloodWait history failure is still wrapped in ValueError (api_server -> 400).
    async def fake_get_chat(client, channel):
        return SimpleNamespace(title="Test", username="testchan", id=-1001234567890)

    async def raise_runtime(client, channel, limit=20):
        raise RuntimeError("history backend exploded")

    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat_history", raise_runtime, raising=False)

    with pytest.raises(ValueError):
        await feed_func("testchan", client=SimpleNamespace(), limit=5)
