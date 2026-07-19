# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""Generic-document 'file' render block + single-post album page.

Non-image, non-PDF documents (.stl/.zip/.iso/...) used to render as a broken
<img> tag. They now render through the new 'file' kind as an info block (file
name + size + t.me link with the "откройте Telegram, чтобы скачать" hint), and
the single-post HTML page (get_post output_type='html') renders the WHOLE media
group so every file of an album is listed. Snapshot schema v4 carries
document.file_name so cache hits render the same block.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pyrogram.enums import MessageMediaType

import post_parser
from post_parser import (
    PostParser, RENDERERS, RenderCtx,
    _select_document, _format_file_size, _render_file,
)
from message_snapshot import SNAPSHOT_VERSION, snapshot_message, restore_message
from url_signer import KeyManager


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
    monkeypatch.setattr(KeyManager, "signing_key", "test-signing-key-document-file")


@pytest.fixture(autouse=True)
def _no_media_id_db(monkeypatch):
    # get_post flushes collected media ids into ./data/media_file_ids.db — a
    # forbidden side effect in tests. The upsert is imported INTO the post_parser
    # namespace, so patch it there (mirrors tests/golden_replay.pin_environment).
    monkeypatch.setattr(post_parser, "upsert_media_file_ids_bulk_sync", lambda *a, **k: None)


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
    for key, value in extra.items():
        setattr(m, key, value)
    return m


def make_document_message(mid, file_name, media_group_id=None, username="testchan",
                          mime="application/x-navistyle", file_size=440184, text=None):
    return make_message(
        mid, media=MessageMediaType.DOCUMENT, text=text, username=username,
        media_group_id=media_group_id,
        document=SimpleNamespace(file_unique_id=f"doc_uid_{mid}", file_id=f"doc_fid_{mid}",
                                 mime_type=mime, file_name=file_name, file_size=file_size),
    )


# ---------------------------------------------------------------------------
# 1. _select_document: pdf / image inline / everything else -> 'file'
# ---------------------------------------------------------------------------

def test_select_document_pdf():
    m = SimpleNamespace(document=SimpleNamespace(mime_type="application/pdf"))
    assert _select_document(m)[1] == 'pdf'


def test_select_document_image_mime_stays_inline():
    m = SimpleNamespace(document=SimpleNamespace(mime_type="image/png"))
    assert _select_document(m)[1] == 'img_400'


def test_select_document_other_mime_is_file():
    m = SimpleNamespace(document=SimpleNamespace(mime_type="application/x-navistyle"))
    obj, kind = _select_document(m)
    assert kind == 'file'
    assert obj is m.document


def test_select_document_missing_mime_is_file():
    m = SimpleNamespace(document=SimpleNamespace())  # no mime_type attribute at all
    assert _select_document(m)[1] == 'file'


def test_file_kind_registered_in_renderers():
    assert RENDERERS['file'] is _render_file


# ---------------------------------------------------------------------------
# 2. _format_file_size
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size,expected", [
    (None, ''),
    (0, ''),
    (-5, ''),
    (True, ''),      # bool is an int subclass but never a valid size
    ("100", ''),
    (500, '500 B'),
    (1023, '1023 B'),
    (1024, '1.0 KB'),
    (440184, '429.9 KB'),
    (5 * 1024 * 1024, '5.0 MB'),
    (3 * 1024 * 1024 * 1024, '3.00 GB'),
])
def test_format_file_size(size, expected):
    assert _format_file_size(size) == expected


# ---------------------------------------------------------------------------
# 3. _render_file
# ---------------------------------------------------------------------------

def test_render_file_escapes_markup_in_name():
    out = '\n'.join(_render_file(RenderCtx(url="U", tg_link="https://t.me/x/1",
                                           file_name="<b>evil</b>.stl", file_size=100)))
    assert "<b>evil</b>" not in out
    assert "&lt;b&gt;evil&lt;/b&gt;.stl" in out


def test_render_file_contains_name_size_link_and_hint():
    out = '\n'.join(_render_file(RenderCtx(url="U", tg_link="https://t.me/x/1",
                                           file_name="part.stl", file_size=440184)))
    assert 'class="document-file"' in out
    assert 'href="https://t.me/x/1"' in out
    assert ">part.stl</a> (429.9 KB)" in out
    assert "— откройте Telegram, чтобы скачать" in out


def test_render_file_falls_back_to_generic_label():
    out = '\n'.join(_render_file(RenderCtx(url="U", tg_link="https://t.me/x/1",
                                           file_name=None, file_size=None)))
    assert ">Файл</a> — откройте Telegram, чтобы скачать" in out


# ---------------------------------------------------------------------------
# 4. Integration: _generate_html_media renders the file block, not <img>
# ---------------------------------------------------------------------------

def test_generate_html_media_document_file_block(parser):
    msg = make_document_message(546, "8812eu2_rev.stl")
    html_media = parser._generate_html_media(msg)
    assert 'class="document-file"' in html_media
    assert 'href="https://t.me/testchan/546"' in html_media
    assert "8812eu2_rev.stl" in html_media
    assert "(429.9 KB)" in html_media
    assert "откройте Telegram, чтобы скачать" in html_media
    assert "<img" not in html_media


def test_generate_html_media_document_file_private_channel_link(parser):
    # Channel without a username: tg_link uses the t.me/c/<id-without-100>/ form.
    msg = make_document_message(547, "part.stl", username=None)
    html_media = parser._generate_html_media(msg)
    assert 'href="https://t.me/c/1234567890/547"' in html_media
    assert "<img" not in html_media


# ---------------------------------------------------------------------------
# 5. get_post: HTML single-post page renders the whole album; JSON untouched
# ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self, message, group=None, group_error=None):
        self.message = message
        self.group = group
        self.group_error = group_error
        self.get_media_group_calls = 0

    async def get_messages(self, chat_id, message_id):
        return self.message

    async def get_media_group(self, chat_id, message_id):
        self.get_media_group_calls += 1
        if self.group_error is not None:
            raise self.group_error
        return self.group


def _album():
    return [make_document_message(546, "part1.stl", media_group_id="mg1", text="album post"),
            make_document_message(547, "part2.stl", media_group_id="mg1"),
            make_document_message(548, "part3.stl", media_group_id="mg1")]


async def test_get_post_html_renders_whole_album():
    group = _album()
    client = FakeClient(message=group[0], group=group)
    page = await PostParser(client).get_post("testchan", 546, output_type='html')
    for name in ("part1.stl", "part2.stl", "part3.stl"):
        assert name in page
    assert page.count('class="document-file"') == 3
    assert "merged" in page  # merged-group flags rendered in the footer
    assert "<img" not in page


async def test_get_post_html_album_fetch_failure_falls_back_to_single():
    msg = make_document_message(546, "part1.stl", media_group_id="mg1")
    client = FakeClient(message=msg,
                        group_error=ValueError("The message doesn't belong to a media group"))
    page = await PostParser(client).get_post("testchan", 546, output_type='html')
    assert client.get_media_group_calls == 1
    assert "part1.stl" in page
    assert page.count('class="document-file"') == 1


async def test_get_post_html_album_render_failure_falls_back_to_single(monkeypatch):
    # A broken album neighbor must not 500 the page: get_media_group succeeds but
    # the group RENDER raises -> the page still renders the requested message alone.
    group = _album()
    client = FakeClient(message=group[0], group=group)

    def boom(self, *args, **kwargs):
        raise RuntimeError("broken album neighbor")

    monkeypatch.setattr(PostParser, "_format_group_html", boom)
    page = await PostParser(client).get_post("testchan", 546, output_type='html')
    assert client.get_media_group_calls == 1
    assert "part1.stl" in page
    assert page.count('class="document-file"') == 1
    assert "merged" not in page


async def test_get_post_html_non_group_message_skips_album_fetch():
    msg = make_document_message(546, "solo.stl")  # media_group_id is None
    client = FakeClient(message=msg)
    page = await PostParser(client).get_post("testchan", 546, output_type='html')
    assert client.get_media_group_calls == 0
    assert "solo.stl" in page


async def test_get_post_json_never_fetches_album():
    # The JSON output must stay the single-message shape — API consumers depend on it.
    group = _album()
    client = FakeClient(message=group[0], group=group)
    data = await PostParser(client).get_post("testchan", 546, output_type='json')
    assert client.get_media_group_calls == 0
    assert isinstance(data, dict)
    assert data['message_id'] == 546
    assert 'raw_message' in data


async def test_get_post_html_album_debug_includes_raw_message():
    group = _album()
    client = FakeClient(message=group[0], group=group)
    page = await PostParser(client).get_post("testchan", 546, output_type='html', debug=True)
    assert 'debug-json' in page
    assert page.count('class="document-file"') == 3


# ---------------------------------------------------------------------------
# 6. Snapshot schema v4: document.file_name survives the round trip
# ---------------------------------------------------------------------------

def test_snapshot_version_is_4():
    assert SNAPSHOT_VERSION == 4


def test_document_file_name_survives_roundtrip():
    msg = SimpleNamespace(document=SimpleNamespace(
        file_unique_id="doc1", mime_type="application/x-navistyle",
        file_size=440184, file_name="8812eu2_rev.stl"))
    restored = restore_message(snapshot_message(msg))
    assert restored.document.file_name == "8812eu2_rev.stl"
    assert restored.document.mime_type == "application/x-navistyle"
    assert restored.document.file_size == 440184


def test_v3_shaped_document_dict_restores_file_name_none():
    # A snapshot dict without file_name (the v3 shape) must restore with
    # file_name=None — never raise. (Version gating already invalidates real v3
    # cache files; this guards the reader itself.)
    snap = snapshot_message(SimpleNamespace())
    snap["media"] = "DOCUMENT"
    snap["document"] = {"file_unique_id": "d", "mime_type": "application/zip", "file_size": 1}
    restored = restore_message(snap)
    assert restored.document.file_name is None
