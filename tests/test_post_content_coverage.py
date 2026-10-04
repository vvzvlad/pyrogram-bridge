# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""Post content the bridge used to lose (post-content coverage pass, snapshot v9).

One section per item: service-message filtering, poll media on a cache hit, flags vs
what the bridge itself adds to the body, selected reply fragments and replies to unresolved
chats, story caption / unavailable story, audio tags, animated (TGS) stickers,
author_signature and invoices. Live-vs-restored byte parity for every new field is proven
in test_snapshot_render_parity.py; the tests here pin the rendered content itself.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from pyrogram.enums import MessageMediaType, MessageServiceType

import post_parser
from message_snapshot import snapshot_message, restore_message
from post_parser import PostParser, MARKER_QUOTE_END
from rss_generator import _create_messages_groups
from url_signer import KeyManager


class FakeStr(str):
    """Stand-in for a live pyrogram Str: a str carrying a .html rendering."""
    def __new__(cls, plain, html=None):
        obj = str.__new__(cls, plain)
        obj._html = html if html is not None else plain
        return obj

    @property
    def html(self):
        return self._html


@pytest.fixture(autouse=True)
def _pinned_signing_key(monkeypatch):
    monkeypatch.setattr(KeyManager, "signing_key", "test-signing-key-post-coverage")


@pytest.fixture
def parser():
    return PostParser(SimpleNamespace())


_CHAT = SimpleNamespace(id=-1001234567890, username="testchan", title="Test Chan", usernames=None)
_ids = [5000]


def make_message(**overrides):
    """A message with every attribute the renderer reads set explicitly (SimpleNamespace,
    not a MagicMock: a mock would hand back truthy auto-attributes for the new fields)."""
    _ids[0] += 1
    fields = dict(
        id=_ids[0], date=datetime(2024, 5, 1, 12, 0, 0), text=None, caption=None, media=None,
        service=None, media_group_id=None, views=10, show_caption_above_media=False,
        reply_to_message_id=None, reply_to_message=None, empty=False, chat=_CHAT,
        sender_chat=None, from_user=None, forward_origin=None, reactions=None, poll=None,
        web_page=None, photo=None, video=None, document=None, audio=None, voice=None,
        video_note=None, animation=None, sticker=None, story=None, live_photo=None,
        quote=None, external_reply=None, author_signature=None, invoice=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


# --------------------------------------------------------------------------- #
# 2. Service messages: skipped unless post_parser renders them with content.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("service", [
    MessageServiceType.GIVEAWAY_CREATED, MessageServiceType.GIVEAWAY_COMPLETED,
    MessageServiceType.GIFT, MessageServiceType.CHECKLIST_TASKS_DONE,
    MessageServiceType.CHECKLIST_TASKS_ADDED, MessageServiceType.PINNED_MESSAGE,
    MessageServiceType.NEW_CHAT_PHOTO, MessageServiceType.COMMUNITY_CHAT_ADDED,
    MessageServiceType.UNSUPPORTED,
])
def test_bookkeeping_service_messages_are_skipped(service):
    groups = _create_messages_groups([make_message(service=service), make_message(text=FakeStr("post"))])
    assert [[m.text for m in g] for g in groups] == [["post"]]


def test_restored_service_name_string_is_skipped_too():
    # A cache hit carries the service as its bare NAME string.
    restored = restore_message(snapshot_message(make_message(service=MessageServiceType.GIVEAWAY_COMPLETED)))
    assert restored.service == "GIVEAWAY_COMPLETED"
    assert _create_messages_groups([restored]) == []


def test_custom_action_with_text_is_kept():
    msg = make_message(service=MessageServiceType.CUSTOM_ACTION, text="Something happened")
    assert _create_messages_groups([msg]) == [[msg]]


def test_custom_action_without_text_is_skipped():
    assert _create_messages_groups([make_message(service=MessageServiceType.CUSTOM_ACTION, text=None)]) == []


def test_custom_action_plain_str_text_renders_escaped(parser):
    # kurigram stores action.message as a plain str (no .html); it must render, escaped,
    # exactly like the snapshot's CachedStr does on a cache hit.
    msg = make_message(service=MessageServiceType.CUSTOM_ACTION, text="a <b> & c")
    body = parser.process_message(msg, sanitize=False)["html"]["body"]
    assert "a &lt;b&gt; &amp; c" in body


# --------------------------------------------------------------------------- #
# 3. Poll description_media survives the snapshot.
# --------------------------------------------------------------------------- #
def _poll(description_media):
    return SimpleNamespace(question=SimpleNamespace(text="Q?"), options=[],
                           description_media=description_media, explanation_media=None)


def test_poll_description_media_roundtrip_keeps_object_and_size():
    big = 200 * 1024 * 1024
    msg = make_message(media=MessageMediaType.POLL, poll=_poll(SimpleNamespace(
        photo=None, video=SimpleNamespace(file_unique_id="pv", file_size=big), animation=None, sticker=None)))
    restored = restore_message(snapshot_message(msg))
    media_obj, kind = post_parser._poll_media_object(restored)
    assert (media_obj.file_unique_id, media_obj.file_size, kind) == ("pv", big, "video")


def test_restored_poll_media_renders_like_live(parser):
    msg = make_message(media=MessageMediaType.POLL, poll=_poll(SimpleNamespace(
        photo=SimpleNamespace(file_unique_id="pp", file_size=10), video=None, animation=None, sticker=None)))
    live = parser._generate_html_body(msg)
    restored = parser._generate_html_body(restore_message(snapshot_message(msg)))
    assert "/pp/" in live
    assert restored == live


def test_poll_without_description_media_restores_none():
    restored = restore_message(snapshot_message(make_message(media=MessageMediaType.POLL, poll=_poll(None))))
    assert restored.poll.description_media is None


# --------------------------------------------------------------------------- #
# 4. Flags ignore what the bridge itself adds to the body.
# --------------------------------------------------------------------------- #
def test_photo_only_post_has_no_link_flag(parser):
    msg = make_message(media=MessageMediaType.PHOTO, photo=SimpleNamespace(file_unique_id="ph"))
    body = parser._generate_html_body(msg)
    assert "http://test.example.com/media/" in body
    assert "link" not in parser._extract_flags(msg)


def test_external_url_still_sets_link_flag(parser):
    msg = make_message(media=MessageMediaType.PHOTO, photo=SimpleNamespace(file_unique_id="ph"),
                       caption=FakeStr("see https://example.org/page for details"))
    assert "link" in parser._extract_flags(msg)


def test_forward_header_sets_no_mention_or_channel_flags(parser):
    origin = SimpleNamespace(chat=SimpleNamespace(title="Other Chan", username="otherchan"))
    msg = make_message(text=FakeStr("own words"), forward_origin=origin)
    assert "(@otherchan)" in parser._generate_html_body(msg)
    flags = parser._extract_flags(msg)
    assert "fwd" in flags
    assert not {"mention", "foreign_channel"} & set(flags)


def test_rich_photo_post_has_no_link_flag(parser):
    msg = make_message(rich_tree={"v": 1, "blocks": [{"t": "photo", "fid": "rp"}]})
    assert "http://test.example.com/media/" in parser._generate_html_body(msg)
    assert "link" not in parser._extract_flags(msg)


def test_rich_map_post_has_no_link_flag(parser):
    msg = make_message(rich_tree={"v": 1, "blocks": [{"t": "map", "lat": 55.75, "lon": 37.62}]})
    assert "openstreetmap.org" in parser._generate_html_body(msg)
    assert "link" not in parser._extract_flags(msg)


def test_preview_url_counts_but_preview_text_does_not(parser):
    msg = make_message(text=FakeStr("a long post about something without any links at all"),
                       web_page=SimpleNamespace(url="https://t.me/otherchan/5", title="by @someone",
                                                description="follow @someone"))
    flags = parser._extract_flags(msg)
    assert "foreign_channel" in flags
    assert "mention" not in flags


# --------------------------------------------------------------------------- #
# 5. Selected reply fragment and replies to an unresolved chat.
# --------------------------------------------------------------------------- #
def _reply_target(text="the whole original text of the target"):
    return SimpleNamespace(id=900, text=FakeStr(text), caption=None, chat=SimpleNamespace(id=-1009999),
                           sender_chat=SimpleNamespace(id=-1009999, title="Other", username="other"),
                           from_user=None)


def test_selected_fragment_replaces_the_whole_target_text(parser):
    msg = make_message(reply_to_message=_reply_target(),
                       quote=SimpleNamespace(text=FakeStr("original text", "<b>original</b> text")))
    block = parser._format_reply_info(msg)
    assert "<b>original</b> text" in block
    assert "the whole original" not in block
    assert '<a href="https://t.me/other/900" title="#900">Other (@other)</a>' in block


def test_blank_fragment_falls_back_to_the_whole_text(parser):
    msg = make_message(reply_to_message=_reply_target(), quote=SimpleNamespace(text=FakeStr("   ")))
    assert "the whole original text of the target" in parser._format_reply_info(msg)


def _external(origin, chat=None, message_id=2161):
    return SimpleNamespace(message_id=message_id, chat=chat, origin=origin)


_HUB = SimpleNamespace(id=-1001198983871, title="Zhovner Hub", username="zhovner_hub")


def test_external_reply_renders_channel_label_link_and_fragment(parser):
    msg = make_message(external_reply=_external(SimpleNamespace(chat=_HUB, message_id=2161), chat=_HUB),
                       quote=SimpleNamespace(text=FakeStr("selected words")))
    assert parser._format_reply_info(msg) == (
        '<div class="message-reply">--- Reply to <a href="https://t.me/zhovner_hub/2161" title="#2161">'
        f'Zhovner Hub (@zhovner_hub)</a> ---<br>selected words<br>{MARKER_QUOTE_END}</div><br>')


def test_external_reply_without_fragment_is_the_header_alone(parser):
    msg = make_message(external_reply=_external(SimpleNamespace(chat=_HUB), chat=_HUB))
    assert parser._format_reply_info(msg) == (
        '<div class="message-reply">--- Reply to <a href="https://t.me/zhovner_hub/2161" title="#2161">'
        'Zhovner Hub (@zhovner_hub)</a> ---</div><br>')


def test_external_reply_user_origin_names_the_user_and_links_the_group(parser):
    group = SimpleNamespace(id=-1005550001, title="Some Group", username=None)
    origin = SimpleNamespace(sender_user=SimpleNamespace(first_name="Ann", last_name="Lee", username="annlee"))
    block = parser._format_reply_info(make_message(external_reply=_external(origin, chat=group, message_id=12)))
    assert '<a href="https://t.me/c/5550001/12" title="#12">Ann Lee (@annlee)</a>' in block


def test_external_reply_hidden_user_without_chat(parser):
    block = parser._format_reply_info(make_message(
        external_reply=_external(SimpleNamespace(sender_user_name="Anon"), message_id=7)))
    assert "--- Reply to Anon, #7 ---" in block


def test_resolved_reply_wins_over_external_reply(parser):
    msg = make_message(reply_to_message=_reply_target(),
                       external_reply=_external(SimpleNamespace(chat=_HUB), chat=_HUB))
    block = parser._format_reply_info(msg)
    assert "Other (@other)" in block and "zhovner_hub" not in block


def test_external_reply_block_does_not_leak_into_flags(parser):
    msg = make_message(text=FakeStr("own words"),
                       external_reply=_external(SimpleNamespace(chat=_HUB), chat=_HUB),
                       quote=SimpleNamespace(text=FakeStr("see https://example.org and @somechannel")))
    assert "zhovner_hub" in parser._generate_html_body(msg)
    flags = parser._extract_flags(msg)
    assert not {"foreign_channel", "mention", "link"} & set(flags)


# --------------------------------------------------------------------------- #
# 6. Story caption / unavailable story.
# --------------------------------------------------------------------------- #
def test_story_caption_is_rendered_escaped(parser):
    msg = make_message(media=MessageMediaType.STORY, story=SimpleNamespace(
        video=None, photo=SimpleNamespace(file_unique_id="sp"), caption="look <here>\nnow"))
    body = parser._generate_html_body(msg)
    assert "/sp/" in body
    assert '<div class="message-special">look &lt;here&gt;<br>now</div>' in body


def test_deleted_story_renders_unavailable_line_not_an_empty_media_block(parser):
    msg = make_message(media=MessageMediaType.STORY,
                       story=SimpleNamespace(video=None, photo=None, caption=None, deleted=True))
    body = parser._generate_html_body(msg)
    assert "Story unavailable" in body
    assert "message-media" not in body


def test_story_with_media_and_no_caption_adds_no_block(parser):
    msg = make_message(media=MessageMediaType.STORY,
                       story=SimpleNamespace(video=SimpleNamespace(file_unique_id="sv"), photo=None))
    assert parser._format_special_media(msg) is None


# --------------------------------------------------------------------------- #
# 7. Audio performer — title.
# --------------------------------------------------------------------------- #
def _audio_body(parser, **tags):
    msg = make_message(media=MessageMediaType.AUDIO,
                       audio=SimpleNamespace(file_unique_id="au", mime_type="audio/mpeg", **tags))
    return parser._generate_html_media(msg)


def test_audio_shows_performer_and_title_above_the_player(parser):
    media = _audio_body(parser, performer="Open Kids", title="На десерт")
    assert "🎵 Open Kids — На десерт<br>\n<audio controls" in media


def test_audio_with_title_only(parser):
    assert "🎵 Song<br>" in _audio_body(parser, performer=None, title="Song")


def test_audio_tags_are_escaped(parser):
    assert "🎵 A&amp;B — &lt;x&gt;<br>" in _audio_body(parser, performer="A&B", title="<x>")


def test_untagged_audio_and_voice_are_unchanged(parser):
    assert "🎵" not in _audio_body(parser)
    voice = make_message(media=MessageMediaType.VOICE,
                         voice=SimpleNamespace(file_unique_id="vo", mime_type="audio/ogg", performer="X", title="Y"))
    assert "🎵" not in parser._generate_html_media(voice)


# --------------------------------------------------------------------------- #
# 8. Animated (TGS) stickers render as text.
# --------------------------------------------------------------------------- #
def test_animated_sticker_renders_text_not_img(parser):
    msg = make_message(media=MessageMediaType.STICKER, sticker=SimpleNamespace(
        file_unique_id="tgs", emoji="🐱", is_video=False, is_animated=True))
    media = parser._generate_html_media(msg)
    assert "Sticker 🐱" in media
    assert "<img" not in media and "<video" not in media


def test_static_and_video_stickers_keep_their_elements(parser):
    static = make_message(media=MessageMediaType.STICKER, sticker=SimpleNamespace(
        file_unique_id="st", emoji="🙂", is_video=False, is_animated=False))
    video = make_message(media=MessageMediaType.STICKER, sticker=SimpleNamespace(
        file_unique_id="sv", emoji="🙂", is_video=True, is_animated=False))
    assert '<img src=' in parser._generate_html_media(static)
    assert '<video controls autoplay loop muted' in parser._generate_html_media(video)


# --------------------------------------------------------------------------- #
# 9. author_signature is the entry author.
# --------------------------------------------------------------------------- #
def test_author_signature_wins_over_the_channel(parser):
    msg = make_message(sender_chat=SimpleNamespace(title="Chan", username="chan"), author_signature=" Jane Writer ")
    assert parser._get_author_info(msg) == "Jane Writer"


@pytest.mark.parametrize("signature", [None, "", "   "])
def test_missing_signature_keeps_the_channel_author(parser, signature):
    msg = make_message(sender_chat=SimpleNamespace(title="Chan", username="chan"), author_signature=signature)
    assert parser._get_author_info(msg) == "Chan (@chan)"


def test_author_signature_survives_the_snapshot(parser):
    restored = restore_message(snapshot_message(make_message(author_signature="Jane Writer")))
    assert parser._get_author_info(restored) == "Jane Writer"


# --------------------------------------------------------------------------- #
# 10. Invoice: title, description, price.
# --------------------------------------------------------------------------- #
def _invoice_block(parser, **fields):
    return parser._format_special_media(make_message(media=MessageMediaType.INVOICE,
                                                     invoice=SimpleNamespace(**fields)))


def test_invoice_block_has_title_description_and_price(parser):
    block = _invoice_block(parser, title="Course <1>", description="Ten\nlessons", currency="USD", total_amount=1250)
    assert block == ('<div class="message-special">🧾 Invoice: Course &lt;1&gt;<br>Ten<br>lessons'
                     '<br>Price: 12.50 USD</div>')


@pytest.mark.parametrize("currency,amount,expected", [
    ("XTR", 50, "Price: 50 ⭐"),
    ("JPY", 1500, "Price: 1500 JPY"),
    ("BHD", 1250, "Price: 1.250 BHD"),
    ("eur", 5, "Price: 0.05 EUR"),
])
def test_invoice_price_uses_the_currency_exponent(parser, currency, amount, expected):
    assert expected in _invoice_block(parser, title="T", description=None, currency=currency, total_amount=amount)


def test_invoice_without_details_keeps_the_bare_label(parser):
    assert parser._format_special_media(make_message(media=MessageMediaType.INVOICE)) == \
        '<div class="message-special">🧾 Invoice</div>'
