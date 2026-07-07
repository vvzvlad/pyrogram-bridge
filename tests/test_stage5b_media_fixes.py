# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, redefined-outer-name, line-too-long
"""Stage-5b registered media fixes (render-pipeline refactor epic, issue #32/#34).

Two registry items, both routed through the single MEDIA_SOURCES table:
  * §3.13 — the ">100MB don't cache" rule now applies to ANY selected media object,
    not just message.video (previously only video, plus live_photo/story/poll).
  * §3.14 — the <div class="message-media"> container is CLOSED in every branch;
    a None file_unique_id used to leave it open (html5lib then swallowed following
    posts / webpage previews into it).

The fragment snapshot (tests/test_data/media_fragments.json) was regenerated in the 5b
changeset: exactly two cases moved — `file_unique_id_none` and `webpage_without_photo`
each gained the now-balanced `</div>` (see test_stage5_media_table for the oracle).
The feed goldens moved only by the same `</div>` relocation on webpage-without-photo posts.
"""
from types import SimpleNamespace

import pytest

from pyrogram.enums import MessageMediaType

from post_parser import PostParser
from url_signer import KeyManager

from tests import media_fragment_replay as fr


@pytest.fixture(autouse=True)
def _pin_signing_key(monkeypatch):
    monkeypatch.setattr(KeyManager, "signing_key", fr.FRAGMENT_SIGNING_KEY)


@pytest.fixture
def parser():
    return PostParser(SimpleNamespace())


def _msg(mid, media, **extra):
    return fr._msg(mid, media, **extra)


# --------------------------------------------------------------------------- #
# §3.14 — the message-media div is closed in every branch.
# --------------------------------------------------------------------------- #
def test_none_file_unique_id_closes_media_div(parser):
    # A media type whose object has no usable file_unique_id: the container used to
    # be left open. Now it must be closed.
    msg = _msg(1, MessageMediaType.PHOTO, photo=SimpleNamespace(file_unique_id=None, file_id="x"))
    html = parser._generate_html_media(msg)
    assert html == '<div class="message-media">\n</div>'


def test_webpage_without_photo_closes_media_div_before_preview(parser):
    # WEB_PAGE without photo: the empty media div must close BEFORE the webpage-preview
    # block (previously the open div swallowed the preview and, at feed level, following
    # posts). Every <div class="message-media"> is now paired with a </div>.
    msg = _msg(2, MessageMediaType.WEB_PAGE, text="hi",
               web_page=SimpleNamespace(photo=None, url="https://e.com", title="E",
                                        description=None, site_name=None, type="", display_url=None))
    html = parser._generate_html_media(msg)
    assert html.startswith('<div class="message-media">\n</div>\n<div class="webpage-preview">')
    assert html.count('<div class="message-media">') == 1
    # The media container is now empty and closed, not wrapping the preview.
    assert '<div class="message-media">\n</div>' in html


def test_channel_username_missing_still_closes_div(parser):
    # The username-guard branch also closes the div (unchanged behavior, asserted so a
    # future refactor cannot regress it).
    msg = _msg(3, MessageMediaType.PHOTO, username=None, chat_id=555,
               photo=SimpleNamespace(file_unique_id="u", file_id="f"))
    html = parser._generate_html_media(msg)
    assert html == '<div class="message-media">\n</div>'


@pytest.mark.parametrize("case_name", ["file_unique_id_none", "webpage_without_photo"])
def test_5b_fragment_cases_are_now_balanced(parser, case_name):
    """The two fragments that 5b intentionally changed must have a balanced media div."""
    factory = fr.build_cases()[case_name]
    html = parser._generate_html_media(factory())
    assert html.count('<div class="message-media">') == 1
    # There is at least one closing tag for the media container now.
    assert '</div>' in html


# --------------------------------------------------------------------------- #
# §3.13 — the >100MB guard applies to ANY selected object, not only video.
# --------------------------------------------------------------------------- #
BIG = 200 * 1024 * 1024
SMALL = 5 * 1024 * 1024


@pytest.mark.parametrize("media_type,attr", [
    (MessageMediaType.PHOTO, "photo"),
    (MessageMediaType.DOCUMENT, "document"),
    (MessageMediaType.AUDIO, "audio"),
    (MessageMediaType.ANIMATION, "animation"),
])
def test_large_non_video_object_not_collected(parser, media_type, attr):
    """§3.13: a >100MB photo/document/audio/animation is no longer collected for the
    download cache (before 5b only large VIDEO objects were skipped)."""
    obj = SimpleNamespace(file_unique_id="big", file_id="f", file_size=BIG, mime_type="image/png")
    msg = _msg(10, media_type, **{attr: obj})
    parser._pending_media_ids = []
    parser._save_media_file_ids(msg)
    assert parser._pending_media_ids == []


@pytest.mark.parametrize("media_type,attr", [
    (MessageMediaType.PHOTO, "photo"),
    (MessageMediaType.DOCUMENT, "document"),
    (MessageMediaType.AUDIO, "audio"),
])
def test_small_non_video_object_still_collected(parser, media_type, attr):
    obj = SimpleNamespace(file_unique_id="small", file_id="f", file_size=SMALL, mime_type="image/png")
    msg = _msg(11, media_type, **{attr: obj})
    parser._pending_media_ids = []
    parser._save_media_file_ids(msg)
    assert [(c, p, f) for c, p, f, _ in parser._pending_media_ids] == [("testchan", 11, "small")]


def test_exactly_100mb_object_is_collected(parser):
    # §3.13 boundary: the guard is strictly `>` 100MB, so an object of exactly 100MB is
    # still collected. Pins the operator — a mutation to `>=` would drop this and fail.
    obj = SimpleNamespace(file_unique_id="edge", file_id="f",
                          file_size=100 * 1024 * 1024, mime_type="image/png")
    msg = _msg(14, MessageMediaType.PHOTO, photo=obj)
    parser._pending_media_ids = []
    parser._save_media_file_ids(msg)
    assert [(c, p, f) for c, p, f, _ in parser._pending_media_ids] == [("testchan", 14, "edge")]


def test_large_video_still_not_collected(parser):
    # Regression: the video case (the only one guarded before 5b) still works.
    msg = _msg(12, MessageMediaType.VIDEO,
               video=SimpleNamespace(file_unique_id="v", file_id="f", file_size=BIG))
    parser._pending_media_ids = []
    parser._save_media_file_ids(msg)
    assert parser._pending_media_ids == []


def test_object_without_file_size_is_collected(parser):
    # No file_size attribute -> not guarded (the common case for photos).
    msg = _msg(13, MessageMediaType.PHOTO, photo=SimpleNamespace(file_unique_id="nofs", file_id="f"))
    parser._pending_media_ids = []
    parser._save_media_file_ids(msg)
    assert [(c, p, f) for c, p, f, _ in parser._pending_media_ids] == [("testchan", 13, "nofs")]
