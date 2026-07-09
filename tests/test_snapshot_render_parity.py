# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""Render-parity oracle for the snapshot cache (issue #23 review fix).

The core ask of the review: prove that snapshot -> restore preserves EVERYTHING the
render pipeline reads. The old golden test replays a pickle corpus and never renders a
live Message against its snapshot, so a field dropped from the allowlist (the exact
bug the reviewer mutation-proved for web_page/sender_chat/... and for the special-media
types) passes silently.

This test builds a CORPUS of representative live fake Message objects (plain text, every
media type, every special-media type, web_page, poll, forwards Case 1-5, reactions,
caption, a multi-message media group) and for EACH case:

  1. renders the LIVE objects through the REAL public feed entry points
     (rss_generator.generate_channel_html / generate_channel_rss — the same path the
     golden test and production use, driven by monkeypatching tg_cache),
  2. snapshot -> restore every message, renders the RESTORED objects the same way,
  3. asserts the two renders are BYTE-IDENTICAL (HTML and the RSS feed).

Any field the snapshot fails to preserve makes the two renders diverge and this test go
red. It is a self-contained live-vs-restored parity oracle; it does NOT depend on the
frozen pickle goldens.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from pyrogram.enums import MessageMediaType

from message_snapshot import snapshot_messages, restore_messages
from tests import golden_replay as gr


# --------------------------------------------------------------------------- #
# Live-object fakes.
# --------------------------------------------------------------------------- #
class FakeStr(str):
    """Stand-in for a live pyrogram Str: a str carrying a .html rendering."""
    def __new__(cls, plain, html=None):
        obj = str.__new__(cls, plain)
        obj._html = html if html is not None else plain
        return obj

    @property
    def html(self):
        return self._html


_CHANNEL = "parity_ch"
_CHAT = SimpleNamespace(id=-1001234567890, username=_CHANNEL, title="Parity Channel", usernames=None)

# The full top-level attribute surface the render pipeline reads (mirrors CachedMessage).
# A live fake sets every one so it renders WITHOUT relying on getattr fallbacks that a
# restored CachedMessage would provide — otherwise the live side, not the snapshot, would
# be the thing under-specified.
_DEFAULTS = dict(
    id=0, date=None, text=None, caption=None, media=None, service=None,
    media_group_id=None, views=100, show_caption_above_media=False,
    reply_to_message_id=None, reply_to_message=None, empty=False,
    chat=_CHAT, sender_chat=None, from_user=None, forward_origin=None,
    reactions=None, poll=None, web_page=None, photo=None, video=None,
    document=None, audio=None, voice=None, video_note=None, animation=None,
    sticker=None, story=None, contact=None, location=None, venue=None,
    dice=None, game=None, giveaway=None, giveaway_winners=None,
    checklist=None, paid_media=None, live_photo=None,
)

_id_counter = [1000]


def make_msg(**overrides):
    _id_counter[0] += 1
    fields = dict(_DEFAULTS)
    fields["id"] = _id_counter[0]
    fields["date"] = datetime(2024, 3, 1, 12, 0, _id_counter[0] % 60)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _reactions(*reacts):
    return SimpleNamespace(reactions=list(reacts))


# --------------------------------------------------------------------------- #
# Corpus: each case is (name, [live messages]) rendered as its own mini feed.
# --------------------------------------------------------------------------- #
def build_corpus():
    corpus = []

    # Plain text.
    corpus.append(("plain_text", [make_msg(text=FakeStr("Hello world", "Hello <b>world</b>"))]))

    # Caption (photo + caption + show_caption_above_media).
    corpus.append(("caption_photo", [make_msg(
        media=MessageMediaType.PHOTO,
        photo=SimpleNamespace(file_unique_id="pcap"),
        caption=FakeStr("A caption", "A <i>caption</i>"),
        show_caption_above_media=True,
    )]))

    # Regular media types.
    corpus.append(("photo", [make_msg(media=MessageMediaType.PHOTO,
                                      photo=SimpleNamespace(file_unique_id="ph1"))]))
    corpus.append(("video", [make_msg(media=MessageMediaType.VIDEO,
                                      video=SimpleNamespace(file_unique_id="vd1", file_size=2048))]))
    corpus.append(("document_pdf", [make_msg(media=MessageMediaType.DOCUMENT,
                                             document=SimpleNamespace(file_unique_id="doc1", mime_type="application/pdf"))]))
    corpus.append(("document_other", [make_msg(media=MessageMediaType.DOCUMENT,
                                               document=SimpleNamespace(file_unique_id="doc2", mime_type="image/png"))]))
    corpus.append(("audio", [make_msg(media=MessageMediaType.AUDIO,
                                      audio=SimpleNamespace(file_unique_id="au1", mime_type="audio/mpeg"))]))
    corpus.append(("voice", [make_msg(media=MessageMediaType.VOICE,
                                      voice=SimpleNamespace(file_unique_id="vo1", mime_type="audio/ogg"))]))
    corpus.append(("video_note", [make_msg(media=MessageMediaType.VIDEO_NOTE,
                                           video_note=SimpleNamespace(file_unique_id="vn1"))]))
    corpus.append(("animation", [make_msg(media=MessageMediaType.ANIMATION,
                                          animation=SimpleNamespace(file_unique_id="an1"))]))
    corpus.append(("sticker_image", [make_msg(media=MessageMediaType.STICKER,
                                              sticker=SimpleNamespace(file_unique_id="st1", emoji="😀", is_video=False))]))
    corpus.append(("sticker_video", [make_msg(media=MessageMediaType.STICKER,
                                              sticker=SimpleNamespace(file_unique_id="st2", emoji="🎥", is_video=True))]))
    corpus.append(("live_photo", [make_msg(media=MessageMediaType.LIVE_PHOTO,
                                           live_photo=SimpleNamespace(file_unique_id="lp1", file_size=4096))]))

    # --- Special-media types (Fix 1) ------------------------------------- #
    corpus.append(("story_photo", [make_msg(
        media=MessageMediaType.STORY,
        story=SimpleNamespace(video=None, photo=SimpleNamespace(file_unique_id="sto_p")),
    )]))
    corpus.append(("story_video", [make_msg(
        media=MessageMediaType.STORY,
        story=SimpleNamespace(video=SimpleNamespace(file_unique_id="sto_v"), photo=None),
    )]))
    corpus.append(("contact", [make_msg(
        media=MessageMediaType.CONTACT,
        contact=SimpleNamespace(first_name="Ann", last_name="Bee", phone_number="+15551234"),
    )]))
    corpus.append(("location", [make_msg(
        media=MessageMediaType.LOCATION,
        location=SimpleNamespace(latitude=51.50111, longitude=-0.14222),
    )]))
    corpus.append(("venue", [make_msg(
        media=MessageMediaType.VENUE,
        venue=SimpleNamespace(title="Big Ben", address="Westminster",
                              location=SimpleNamespace(latitude=51.50055, longitude=-0.12461)),
    )]))
    corpus.append(("dice", [make_msg(
        media=MessageMediaType.DICE, dice=SimpleNamespace(emoji="🎯", value=6))]))
    corpus.append(("game", [make_msg(
        media=MessageMediaType.GAME, game=SimpleNamespace(title="Chess Master"))]))
    corpus.append(("giveaway", [make_msg(
        media=MessageMediaType.GIVEAWAY,
        giveaway=SimpleNamespace(quantity=3, months=6, stars=None,
                                 until_date=datetime(2030, 12, 31, 12, 0, 0),
                                 description="Great prizes"),
    )]))
    corpus.append(("giveaway_stars", [make_msg(
        media=MessageMediaType.GIVEAWAY,
        giveaway=SimpleNamespace(quantity=2, months=None, stars=500,
                                 until_date=None, description=None),
    )]))
    corpus.append(("giveaway_winners", [make_msg(
        media=MessageMediaType.GIVEAWAY_WINNERS,
        giveaway_winners=SimpleNamespace(winner_count=5, quantity=10, prize_description="Premium"),
    )]))
    corpus.append(("checklist", [make_msg(
        media=MessageMediaType.CHECKLIST,
        checklist=SimpleNamespace(title="Todo list", tasks=[
            SimpleNamespace(text="done task", completed_by=SimpleNamespace(id=1), completion_date=None),
            SimpleNamespace(text="open task", completed_by=None, completion_date=None),
        ]),
    )]))
    corpus.append(("paid_media", [make_msg(
        media=MessageMediaType.PAID_MEDIA,
        paid_media=SimpleNamespace(stars_amount=50, media=[object(), object()]),
    )]))

    # --- web_page + poll -------------------------------------------------- #
    corpus.append(("web_page", [make_msg(
        text=FakeStr("check this", "check this"),
        media=MessageMediaType.WEB_PAGE,
        web_page=SimpleNamespace(type="article", url="https://example.com/a",
                                 display_url="example.com/a", site_name="Example",
                                 title="An Article", description="Desc here",
                                 has_large_media=False,
                                 photo=SimpleNamespace(file_unique_id="wp1")),
    )]))
    corpus.append(("poll", [make_msg(
        media=MessageMediaType.POLL,
        poll=SimpleNamespace(
            question=SimpleNamespace(text="Favourite?", entities=[]),
            options=[SimpleNamespace(text=SimpleNamespace(text="Red", entities=[])),
                     SimpleNamespace(text=SimpleNamespace(text="Blue", entities=[]))],
            description_media=None, explanation_media=None,
        ),
    )]))

    # --- Forwards Case 1-5 (drive _format_forward_info) ------------------- #
    corpus.append(("forward_channel", [make_msg(
        text=FakeStr("fwd", "fwd"),
        forward_origin=SimpleNamespace(type="channel",
                                       chat=SimpleNamespace(id=-100, title="Src Chan", username="srcchan")),
    )]))
    corpus.append(("forward_hidden", [make_msg(
        text=FakeStr("fwd", "fwd"),
        forward_origin=SimpleNamespace(type="hidden_user", sender_user_name="Hidden Guy"),
    )]))
    corpus.append(("forward_user", [make_msg(
        text=FakeStr("fwd", "fwd"),
        forward_origin=SimpleNamespace(type="user",
                                       sender_user=SimpleNamespace(first_name="Ann", last_name="Bee", username="annbee")),
    )]))
    corpus.append(("forward_channel_nouser", [make_msg(
        text=FakeStr("fwd", "fwd"),
        forward_origin=SimpleNamespace(type="channel", chat_id=-1002, title="NoUserChan"),
    )]))
    corpus.append(("forward_other", [make_msg(
        text=FakeStr("fwd", "fwd"),
        forward_origin=SimpleNamespace(type="something_else"),
    )]))

    # --- Reactions: normal / paid / custom -------------------------------- #
    corpus.append(("reactions", [make_msg(
        text=FakeStr("react", "react"),
        reactions=_reactions(
            SimpleNamespace(emoji="👍", count=5, is_paid=False),
            SimpleNamespace(count=3, is_paid=True),
            SimpleNamespace(custom_emoji_id=1234567890, count=2),
        ),
    )]))

    # --- sender_chat / from_user author paths ----------------------------- #
    corpus.append(("sender_chat_author", [make_msg(
        text=FakeStr("a", "a"),
        sender_chat=SimpleNamespace(id=-100, title="Sender Chan", username="senderchan"),
    )]))
    corpus.append(("from_user_author", [make_msg(
        text=FakeStr("a", "a"),
        from_user=SimpleNamespace(first_name="Joe", last_name="Doe", username="joedoe"),
    )]))

    # --- Multi-message media group (grouping merge) ----------------------- #
    corpus.append(("media_group", [
        make_msg(media=MessageMediaType.PHOTO, media_group_id="grp1",
                 photo=SimpleNamespace(file_unique_id="grp_a"),
                 caption=FakeStr("group cap", "group cap")),
        make_msg(media=MessageMediaType.PHOTO, media_group_id="grp1",
                 photo=SimpleNamespace(file_unique_id="grp_b")),
    ]))

    return corpus


# --------------------------------------------------------------------------- #
# Rendering harness — patches tg_cache to feed a message list into the real
# generate_channel_* entry points (the golden-replay path).
# --------------------------------------------------------------------------- #
def _render(messages, monkeypatch):
    async def fake_get_chat_history(client, channel_id, limit=20):
        return messages

    async def fake_get_chat(client, channel_id):
        return SimpleNamespace(id=_CHAT.id, title=_CHAT.title, username=_CHAT.username)

    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_chat_history, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)

    from rss_generator import generate_channel_html, generate_channel_rss
    html = asyncio.run(generate_channel_html(_CHANNEL, client=SimpleNamespace(), limit=50))
    rss = asyncio.run(generate_channel_rss(_CHANNEL, client=SimpleNamespace(), limit=50))
    return html, gr.normalize_rss(rss)


@pytest.mark.parametrize("name,messages", build_corpus(), ids=lambda v: v if isinstance(v, str) else "")
def test_snapshot_render_parity(name, messages, monkeypatch):
    gr.pin_environment(monkeypatch)

    # 1) Render the LIVE objects through the real pipeline.
    live_html, live_rss = _render(messages, monkeypatch)

    # 2) snapshot -> restore, then render the RESTORED objects the same way.
    restored = restore_messages(snapshot_messages(messages))
    restored_html, restored_rss = _render(restored, monkeypatch)

    # 3) Byte-identical HTML and RSS prove the snapshot preserved everything the
    #    renderer read for this case. A dropped allowlist field diverges here.
    assert restored_html == live_html, f"HTML render diverged for case '{name}'"
    assert restored_rss == live_rss, f"RSS render diverged for case '{name}'"


def test_corpus_covers_all_special_media_types():
    """Guard: every special-media type from Fix 1 is exercised by the parity corpus."""
    names = {n for n, _ in build_corpus()}
    required = {"story_photo", "story_video", "contact", "location", "venue",
                "dice", "game", "giveaway", "giveaway_winners", "checklist", "paid_media",
                "live_photo"}
    assert required <= names
