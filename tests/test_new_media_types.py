# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Kurigram 2.2.23 — new/uncovered media types (LIVE_PHOTO, STORY, poll media,
GIVEAWAY, GIVEAWAY_WINNERS, PAID_MEDIA, CHECKLIST, CONTACT, LOCATION, VENUE,
DICE, GAME, INVOICE, UNSUPPORTED).

Covers:
- titles for every new media type (_media_message_title via _generate_title);
- HTML rendering: live photo <video>, story <video>/<img>, poll description_media
  <img>, paid media info block (no download);
- info blocks for non-downloadable types (_format_special_media) incl. XSS escaping;
- flags: no_image semantics for the new types and polls with/without media,
  "video" flag for live photos;
- api_server.find_file_id_in_message: live_photo, story, poll description_media /
  explanation_media lookups.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pyrogram.enums import MessageMediaType

from post_parser import PostParser
from api_server import find_file_id_in_message
from url_signer import KeyManager, generate_media_digest


class _Str(str):
    """Minimal stand-in for Pyrogram's Str: .html returns the raw string unchanged."""
    @property
    def html(self):
        return str(self)


@pytest.fixture(autouse=True)
def _pinned_signing_key(monkeypatch):
    # generate_media_digest reads/creates data/media_digest.key relative to cwd; pin
    # the in-memory key so digests are deterministic and no file IO happens
    # regardless of the invocation directory (repo root or tests/).
    monkeypatch.setattr(KeyManager, "signing_key", "test-signing-key-new-media")


@pytest.fixture
def parser():
    return PostParser(SimpleNamespace())


def make_message(mid=1, media=None, text=None, username="testchan", **extra):
    m = SimpleNamespace()
    m.id = mid
    m.date = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    m.text = _Str(text) if text is not None else None
    m.caption = None
    m.media = media
    m.web_page = None
    m.poll = None
    m.service = None
    m.forward_origin = None
    m.reply_to_message = None
    m.sender_chat = None
    m.from_user = None
    m.reactions = None
    m.views = 100
    m.media_group_id = None
    m.show_caption_above_media = False
    m.chat = SimpleNamespace(id=-1001234567890, username=username)
    for attr in ("photo", "video", "document", "audio", "voice",
                 "video_note", "animation", "sticker"):
        setattr(m, attr, None)
    # New Kurigram 2.2.23 attributes (live_photo, story, giveaway, checklist, ...)
    # are deliberately NOT set by default: production code must survive their
    # absence via getattr (that is exactly what old mocks look like).
    for key, value in extra.items():
        setattr(m, key, value)
    return m


def media_url(mid, fuid, username="testchan"):
    file = f"{username}/{mid}/{fuid}"
    return f"http://test.example.com/media/{file}/{generate_media_digest(file)}"


# ---------------------------------------------------------------------------
# 1. Titles for every new media type
# ---------------------------------------------------------------------------

def test_title_live_photo(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.LIVE_PHOTO)) == "📸 Live Photo"

def test_title_story(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.STORY)) == "📖 Story"

def test_title_giveaway(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.GIVEAWAY)) == "🎁 Giveaway"

def test_title_giveaway_winners(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.GIVEAWAY_WINNERS)) == "🏆 Giveaway winners"

def test_title_paid_media(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.PAID_MEDIA)) == "⭐ Paid media"

def test_title_checklist_with_title(parser):
    msg = make_message(media=MessageMediaType.CHECKLIST,
                       checklist=SimpleNamespace(title="Shopping list", tasks=[]))
    assert parser._generate_title(msg) == "📝 Checklist: Shopping list"

def test_title_checklist_long_title_truncated(parser):
    long_title = "x" * 80
    msg = make_message(media=MessageMediaType.CHECKLIST,
                       checklist=SimpleNamespace(title=long_title, tasks=[]))
    assert parser._generate_title(msg) == f"📝 Checklist: {'x' * 50}"

def test_title_checklist_without_object(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.CHECKLIST)) == "📝 Checklist"

def test_title_contact(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.CONTACT)) == "👤 Contact"

def test_title_location(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.LOCATION)) == "📍 Location"

def test_title_venue_with_title(parser):
    msg = make_message(media=MessageMediaType.VENUE,
                       venue=SimpleNamespace(title="Blue Bottle Cafe", address="1 Main St"))
    assert parser._generate_title(msg) == "📍 Blue Bottle Cafe"

def test_title_venue_fallback(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.VENUE)) == "📍 Venue"

def test_title_dice(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.DICE)) == "🎲 Dice"

def test_title_game(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.GAME)) == "🎮 Game"

def test_title_invoice(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.INVOICE)) == "🧾 Invoice"

def test_title_unsupported(parser):
    assert parser._generate_title(make_message(media=MessageMediaType.UNSUPPORTED)) == "⚠️ Unsupported content"


# ---------------------------------------------------------------------------
# 2. LIVE_PHOTO rendering
# ---------------------------------------------------------------------------

def test_live_photo_renders_video_with_signed_url(parser):
    msg = make_message(101, media=MessageMediaType.LIVE_PHOTO,
                       live_photo=SimpleNamespace(file_unique_id="lp_uid_1", file_id="lp_fid_1"))
    html_media = parser._generate_html_media(msg)
    assert "<video" in html_media
    assert media_url(101, "lp_uid_1") in html_media


def test_live_photo_collected_for_media_ids(parser):
    msg = make_message(102, media=MessageMediaType.LIVE_PHOTO,
                       live_photo=SimpleNamespace(file_unique_id="lp_uid_2", file_id="lp_fid_2"))
    parser._generate_html_media(msg)
    assert [(c, p, f) for c, p, f, _ in parser._pending_media_ids] == [("testchan", 102, "lp_uid_2")]


def test_live_photo_gets_video_flag_not_no_image(parser):
    msg = make_message(103, media=MessageMediaType.LIVE_PHOTO,
                       live_photo=SimpleNamespace(file_unique_id="lp_uid_3", file_id="lp_fid_3"))
    flags = parser._extract_flags(msg)
    assert "video" in flags
    assert "no_image" not in flags


# ---------------------------------------------------------------------------
# 3. STORY rendering
# ---------------------------------------------------------------------------

def test_story_with_video_renders_video(parser):
    msg = make_message(111, media=MessageMediaType.STORY,
                       story=SimpleNamespace(video=SimpleNamespace(file_unique_id="st_vid"),
                                             photo=None))
    html_media = parser._generate_html_media(msg)
    assert "<video" in html_media
    assert media_url(111, "st_vid") in html_media


def test_story_with_photo_renders_img(parser):
    msg = make_message(112, media=MessageMediaType.STORY,
                       story=SimpleNamespace(video=None,
                                             photo=SimpleNamespace(file_unique_id="st_pic")))
    html_media = parser._generate_html_media(msg)
    assert "<img" in html_media
    assert "<video" not in html_media
    assert media_url(112, "st_pic") in html_media


def test_story_video_wins_over_photo(parser):
    msg = make_message(113, media=MessageMediaType.STORY,
                       story=SimpleNamespace(video=SimpleNamespace(file_unique_id="st_vid2"),
                                             photo=SimpleNamespace(file_unique_id="st_pic2")))
    html_media = parser._generate_html_media(msg)
    assert media_url(113, "st_vid2") in html_media
    assert "st_pic2" not in html_media


def test_story_video_without_uid_falls_back_to_photo_as_img(parser):
    # Review fix 3: the tag choice must follow the SAME object selection as the URL.
    # A story video with an unusable file_unique_id is skipped by the helper in
    # favour of the photo — the render must emit <img>, not <video>.
    msg = make_message(114, media=MessageMediaType.STORY,
                       story=SimpleNamespace(video=SimpleNamespace(file_unique_id=None),
                                             photo=SimpleNamespace(file_unique_id="st_pic3")))
    html_media = parser._generate_html_media(msg)
    assert "<img" in html_media
    assert "<video" not in html_media
    assert media_url(114, "st_pic3") in html_media


def test_story_large_video_not_collected_for_media_ids(parser):
    # Review fix 2: the >100MB "don't cache" rule applies to story videos too.
    msg = make_message(115, media=MessageMediaType.STORY,
                       story=SimpleNamespace(video=SimpleNamespace(file_unique_id="st_big",
                                                                   file_size=200 * 1024 * 1024),
                                             photo=None))
    parser._save_media_file_ids(msg)
    assert parser._pending_media_ids == []


def test_story_small_video_collected_for_media_ids(parser):
    msg = make_message(116, media=MessageMediaType.STORY,
                       story=SimpleNamespace(video=SimpleNamespace(file_unique_id="st_small",
                                                                   file_size=5 * 1024 * 1024),
                                             photo=None))
    parser._save_media_file_ids(msg)
    assert [(c, p, f) for c, p, f, _ in parser._pending_media_ids] == [("testchan", 116, "st_small")]


# ---------------------------------------------------------------------------
# 4. POLL with/without description_media
# ---------------------------------------------------------------------------

def _poll_with_photo(fuid="poll_pic", fid="poll_pic_fid"):
    return SimpleNamespace(
        question=_Str("Pick one?"),
        options=[],
        description_media=SimpleNamespace(photo=SimpleNamespace(file_unique_id=fuid, file_id=fid)),
    )


def test_poll_with_description_photo_renders_img(parser):
    msg = make_message(121, media=MessageMediaType.POLL, poll=_poll_with_photo())
    html_media = parser._generate_html_media(msg)
    assert "<img" in html_media
    assert media_url(121, "poll_pic") in html_media


def test_poll_with_description_photo_has_no_no_image_flag(parser):
    msg = make_message(122, media=MessageMediaType.POLL, poll=_poll_with_photo())
    flags = parser._extract_flags(msg)
    assert "no_image" not in flags
    assert "poll" in flags


def test_poll_without_media_keeps_no_image_flag(parser):
    msg = make_message(123, media=MessageMediaType.POLL,
                       poll=SimpleNamespace(question=_Str("Plain poll?"), options=[]))
    flags = parser._extract_flags(msg)
    assert "no_image" in flags
    assert "poll" in flags


def test_poll_media_collected_for_media_ids(parser):
    msg = make_message(124, media=MessageMediaType.POLL, poll=_poll_with_photo("poll_pic4"))
    parser._generate_html_media(msg)
    assert [(c, p, f) for c, p, f, _ in parser._pending_media_ids] == [("testchan", 124, "poll_pic4")]


def test_poll_with_video_description_renders_video(parser):
    poll = SimpleNamespace(
        question=_Str("Video poll?"),
        options=[],
        description_media=SimpleNamespace(video=SimpleNamespace(file_unique_id="poll_vid", file_id="poll_vid_fid")),
    )
    msg = make_message(125, media=MessageMediaType.POLL, poll=poll)
    html_media = parser._generate_html_media(msg)
    assert "<video" in html_media
    assert media_url(125, "poll_vid") in html_media


# ---------------------------------------------------------------------------
# 5. api_server.find_file_id_in_message (async)
# ---------------------------------------------------------------------------

async def test_find_file_id_live_photo():
    msg = make_message(201, media=MessageMediaType.LIVE_PHOTO,
                       live_photo=SimpleNamespace(file_unique_id="lp_uid", file_id="lp_fid"))
    assert await find_file_id_in_message(msg, "lp_uid") == "lp_fid"


async def test_find_file_id_story_video():
    msg = make_message(202, media=MessageMediaType.STORY,
                       story=SimpleNamespace(photo=None,
                                             video=SimpleNamespace(file_unique_id="sv_uid", file_id="sv_fid")))
    assert await find_file_id_in_message(msg, "sv_uid") == "sv_fid"


async def test_find_file_id_story_photo():
    msg = make_message(203, media=MessageMediaType.STORY,
                       story=SimpleNamespace(photo=SimpleNamespace(file_unique_id="sp_uid", file_id="sp_fid"),
                                             video=None))
    assert await find_file_id_in_message(msg, "sp_uid") == "sp_fid"


async def test_find_file_id_poll_description_photo():
    msg = make_message(204, media=MessageMediaType.POLL, poll=_poll_with_photo("pd_uid", "pd_fid"))
    assert await find_file_id_in_message(msg, "pd_uid") == "pd_fid"


async def test_find_file_id_poll_explanation_video():
    poll = SimpleNamespace(
        question=_Str("Quiz?"),
        options=[],
        explanation_media=SimpleNamespace(video=SimpleNamespace(file_unique_id="ex_uid", file_id="ex_fid")),
    )
    msg = make_message(205, media=MessageMediaType.POLL, poll=poll)
    assert await find_file_id_in_message(msg, "ex_uid") == "ex_fid"


async def test_find_file_id_poll_without_media_returns_none():
    msg = make_message(206, media=MessageMediaType.POLL,
                       poll=SimpleNamespace(question=_Str("Plain?"), options=[]))
    assert await find_file_id_in_message(msg, "whatever") is None


async def test_find_file_id_poll_none_object_returns_none():
    msg = make_message(207, media=MessageMediaType.POLL)  # message.poll stays None
    assert await find_file_id_in_message(msg, "whatever") is None


# ---------------------------------------------------------------------------
# 6. XSS: user-controlled strings in info blocks are escaped
# ---------------------------------------------------------------------------

XSS = "<script>alert(1)</script>"
XSS_ESCAPED = "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_contact_name_is_escaped(parser):
    msg = make_message(301, media=MessageMediaType.CONTACT,
                       contact=SimpleNamespace(first_name=XSS, last_name="Doe",
                                               phone_number="+1234567890", user_id=None, vcard=None))
    body = parser._generate_html_body(msg)
    assert XSS not in body
    assert XSS_ESCAPED in body
    assert "+1234567890" in body


def test_checklist_task_text_is_escaped(parser):
    checklist = SimpleNamespace(
        title="My list",
        tasks=[
            SimpleNamespace(id=1, text=XSS, completed_by=None, completion_date=None),
            SimpleNamespace(id=2, text="done task", completed_by=SimpleNamespace(id=7), completion_date=None),
        ],
    )
    msg = make_message(302, media=MessageMediaType.CHECKLIST, checklist=checklist)
    body = parser._generate_html_body(msg)
    assert XSS not in body
    assert f"☐ {XSS_ESCAPED}" in body
    assert "☑ done task" in body
    assert "📝 My list" in body


def test_venue_title_is_escaped(parser):
    # VENUE title feeds venue_label -> html.escape(venue_label)
    venue = SimpleNamespace(title=XSS, address="1 Main St",
                            location=SimpleNamespace(latitude=1.5, longitude=2.5))
    msg = make_message(303, media=MessageMediaType.VENUE, venue=venue)
    body = parser._generate_html_body(msg)
    assert XSS not in body
    assert XSS_ESCAPED in body


def test_venue_address_label_is_escaped(parser):
    # VENUE address also feeds venue_label -> html.escape(venue_label)
    venue = SimpleNamespace(title="Blue Bottle", address=XSS,
                            location=SimpleNamespace(latitude=1.5, longitude=2.5))
    msg = make_message(304, media=MessageMediaType.VENUE, venue=venue)
    body = parser._generate_html_body(msg)
    assert XSS not in body
    assert XSS_ESCAPED in body


def test_dice_emoji_is_escaped(parser):
    # DICE emoji -> html.escape(str(dice_emoji))
    msg = make_message(305, media=MessageMediaType.DICE,
                       dice=SimpleNamespace(emoji=XSS, value=6))
    body = parser._generate_html_body(msg)
    assert XSS not in body
    assert XSS_ESCAPED in body


def test_game_title_is_escaped(parser):
    # GAME title -> html.escape(game_title.strip())
    msg = make_message(306, media=MessageMediaType.GAME,
                       game=SimpleNamespace(title=XSS))
    body = parser._generate_html_body(msg)
    assert XSS not in body
    assert XSS_ESCAPED in body


def test_checklist_title_is_escaped(parser):
    # CHECKLIST title -> html.escape(title_str)
    checklist = SimpleNamespace(title=XSS, tasks=[])
    msg = make_message(307, media=MessageMediaType.CHECKLIST, checklist=checklist)
    body = parser._generate_html_body(msg)
    assert XSS not in body
    assert XSS_ESCAPED in body


def test_giveaway_description_is_escaped(parser):
    # GIVEAWAY description -> html.escape(description.strip())
    giveaway = SimpleNamespace(quantity=5, months=None, stars=None,
                               until_date=None, description=XSS)
    msg = make_message(308, media=MessageMediaType.GIVEAWAY, giveaway=giveaway)
    body = parser._generate_html_body(msg)
    assert XSS not in body
    assert XSS_ESCAPED in body


def test_giveaway_winners_prize_description_is_escaped(parser):
    # GIVEAWAY_WINNERS prize_description -> html.escape(prize_description.strip())
    winners = SimpleNamespace(winner_count=2, quantity=5, prize_description=XSS,
                              unclaimed_prize_count=0, winners=[])
    msg = make_message(309, media=MessageMediaType.GIVEAWAY_WINNERS, giveaway_winners=winners)
    body = parser._generate_html_body(msg)
    assert XSS not in body
    assert XSS_ESCAPED in body


# ---------------------------------------------------------------------------
# 7. Giveaway / giveaway winners info blocks
# ---------------------------------------------------------------------------

def test_giveaway_block_quantity_and_months(parser):
    giveaway = SimpleNamespace(quantity=10, months=3, stars=None,
                               until_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
                               description="Win big!")
    msg = make_message(311, media=MessageMediaType.GIVEAWAY, giveaway=giveaway)
    body = parser._generate_html_body(msg)
    assert "🎁 Giveaway: 10 prize(s)" in body
    assert "3 months Premium" in body
    assert "until 01/02/2026" in body
    assert "Win big!" in body


def test_giveaway_block_with_stars(parser):
    giveaway = SimpleNamespace(quantity=5, months=None, stars=500,
                               until_date=None, description=None)
    msg = make_message(312, media=MessageMediaType.GIVEAWAY, giveaway=giveaway)
    body = parser._generate_html_body(msg)
    assert "🎁 Giveaway: 5 prize(s)" in body
    assert "500 Stars" in body


def test_giveaway_winners_block(parser):
    winners = SimpleNamespace(winner_count=2, quantity=5, prize_description="Cool prize",
                              unclaimed_prize_count=3, winners=[])
    msg = make_message(313, media=MessageMediaType.GIVEAWAY_WINNERS, giveaway_winners=winners)
    body = parser._generate_html_body(msg)
    assert "🏆 Giveaway winners: 2 of 5" in body
    assert "Cool prize" in body


# ---------------------------------------------------------------------------
# Other info blocks and paid media
# ---------------------------------------------------------------------------

def test_paid_media_renders_info_block_without_download(parser):
    paid = SimpleNamespace(stars_amount=50,
                           media=[SimpleNamespace(width=100, height=100, duration=None, thumbnail=None),
                                  SimpleNamespace(width=200, height=200, duration=5, thumbnail=None)])
    msg = make_message(321, media=MessageMediaType.PAID_MEDIA, paid_media=paid)
    html_media = parser._generate_html_media(msg)
    assert "⭐ Paid media (50 stars, 2 item(s)) — available in Telegram" in html_media
    assert "/media/" not in html_media  # nothing downloadable
    assert parser._pending_media_ids == []  # nothing collected for the download cache
    assert "no_image" in parser._extract_flags(msg)


def test_location_block_has_osm_link(parser):
    msg = make_message(322, media=MessageMediaType.LOCATION,
                       location=SimpleNamespace(latitude=55.75123456, longitude=37.61761111))
    body = parser._generate_html_body(msg)
    assert "📍 Location:" in body
    assert "https://www.openstreetmap.org/?mlat=55.75123456&mlon=37.61761111" in body
    assert "55.75123, 37.61761" in body
    assert "no_image" in parser._extract_flags(msg)


def test_venue_block_with_osm_link(parser):
    venue = SimpleNamespace(title="Blue Bottle", address="1 Main St",
                            location=SimpleNamespace(latitude=1.5, longitude=2.5))
    msg = make_message(323, media=MessageMediaType.VENUE, venue=venue)
    body = parser._generate_html_body(msg)
    assert "📍 Blue Bottle, 1 Main St" in body
    assert "openstreetmap.org/?mlat=1.5&mlon=2.5" in body


# Review fix 1: info-block-only media types must not open an empty
# <div class="message-media"> container — only the special block is rendered.

def test_venue_has_no_empty_media_div(parser):
    venue = SimpleNamespace(title="Cafe", address="2 Side St",
                            location=SimpleNamespace(latitude=1.0, longitude=2.0))
    msg = make_message(328, media=MessageMediaType.VENUE, venue=venue)
    body = parser._generate_html_body(msg)
    assert 'class="message-media"' not in body
    assert 'class="message-special"' in body


def test_contact_has_no_empty_media_div(parser):
    msg = make_message(329, media=MessageMediaType.CONTACT,
                       contact=SimpleNamespace(first_name="Jane", last_name="Doe",
                                               phone_number="+1999", user_id=None, vcard=None))
    body = parser._generate_html_body(msg)
    assert 'class="message-media"' not in body
    assert 'class="message-special"' in body


def test_paid_media_block_survives_media_div_gate(parser):
    # PAID_MEDIA is in NO_IMAGE_MEDIA_TYPES but has its own render branch — the
    # info block (inside its message-media container) must keep rendering.
    paid = SimpleNamespace(stars_amount=10, media=[SimpleNamespace(width=1, height=1,
                                                                   duration=None, thumbnail=None)])
    msg = make_message(330, media=MessageMediaType.PAID_MEDIA, paid_media=paid)
    body = parser._generate_html_body(msg)
    assert 'class="message-media"' in body
    assert "⭐ Paid media (10 stars, 1 item(s)) — available in Telegram" in body


def test_dice_block(parser):
    msg = make_message(324, media=MessageMediaType.DICE,
                       dice=SimpleNamespace(emoji="🎯", value=6))
    body = parser._generate_html_body(msg)
    assert "🎲 🎯: 6" in body


def test_game_block_with_title(parser):
    msg = make_message(325, media=MessageMediaType.GAME,
                       game=SimpleNamespace(title="Tetris"))
    body = parser._generate_html_body(msg)
    assert "🎮 Game: Tetris" in body


def test_invoice_block(parser):
    msg = make_message(326, media=MessageMediaType.INVOICE)
    body = parser._generate_html_body(msg)
    assert "🧾 Invoice" in body


def test_unsupported_block(parser):
    msg = make_message(327, media=MessageMediaType.UNSUPPORTED)
    body = parser._generate_html_body(msg)
    assert "⚠️ This post contains content not supported by the bridge" in body
    assert "no_image" in parser._extract_flags(msg)


# ---------------------------------------------------------------------------
# Regression: messages without any of the new attributes still render
# ---------------------------------------------------------------------------

def test_plain_text_message_unaffected(parser):
    msg = make_message(331, text="just a plain post with enough text")
    body = parser._generate_html_body(msg)
    assert "just a plain post with enough text" in body
    assert "message-special" not in body
    assert "paid-media" not in body
    flags = parser._extract_flags(msg)
    assert "no_image" in flags
