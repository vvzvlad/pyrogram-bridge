# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
"""Stage-5 media table tests (render-pipeline refactor epic, issue #32/#34).

Two layers:
  * 5a — the MEDIA_SOURCES table + renderers reproduce the three old ladders
    (_get_file_unique_id, _save_media_file_ids, _generate_html_media). The fragment
    snapshot (tests/test_data/media_fragments.json) is the PRE-REFACTOR BASE reference,
    captured against the base post_parser.py; the 5a code reproduces it byte-for-byte
    (two fragments carry registered §3.14 deltas — see fr.REGISTERED_DELTAS). Plus
    table/selector/invariant unit tests.
  * 5b — the registered fixes (§3.13 large-file guard for every object, §3.14 the
    message-media div is closed in every branch) live in test_stage5b_media_fixes.py.

The cross-module invariant test pins the boundary with api_server.find_file_id_in_message:
every object a selector returns must be resolvable there by its file_unique_id, or a new
table entry would mint a /media URL the download path answers with 404.
"""
from types import SimpleNamespace

import pytest

from pyrogram.enums import MessageMediaType

from post_parser import (
    PostParser, MEDIA_SOURCES, RENDERERS, RenderCtx,
    _select_document, _select_sticker, _select_story, _select_poll_media,
)
from api_server import find_file_id_in_message
from url_signer import KeyManager

from tests import media_fragment_replay as fr


@pytest.fixture(autouse=True)
def _pin_signing_key(monkeypatch):
    # Deterministic media-URL digests regardless of checkout / cwd.
    monkeypatch.setattr(KeyManager, "signing_key", fr.FRAGMENT_SIGNING_KEY)


@pytest.fixture
def parser():
    return PostParser(SimpleNamespace())


# --------------------------------------------------------------------------- #
# 1. Fragment-level snapshot oracle — the 5a byte-for-byte contract.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case_name", sorted(fr.build_cases().keys()))
def test_media_fragment_matches_snapshot(parser, case_name):
    """_generate_html_media (render ladder + _get_file_unique_id ladder) and the
    _save_media_file_ids collection ladder reproduce the frozen pre-refactor bytes."""
    snapshot = fr.load_snapshot()
    factory = fr.build_cases()[case_name]
    parser._pending_media_ids = []
    html = parser._generate_html_media(factory())
    collected = [[c, p, f] for c, p, f, _ in parser._pending_media_ids]
    # The snapshot is the pre-refactor BASE reference; the two §3.14 fragments carry a
    # registered 5b delta (the now-closed message-media div), so expect their 5b bytes.
    expected = fr.REGISTERED_DELTAS.get(case_name, snapshot[case_name])
    assert html == expected["html"], f"fragment HTML diverged for {case_name}"
    assert collected == expected["collected"], f"collection diverged for {case_name}"


def test_snapshot_covers_every_spec_case():
    """Guard: the snapshot and the case builders stay in lockstep (a dropped case
    would otherwise silently reduce coverage)."""
    snapshot = fr.load_snapshot()
    assert set(snapshot.keys()) == set(fr.build_cases().keys())
    # The spec §5a "Шаг 0" edge branches must all be present.
    for required in ("photo", "video", "animation", "video_note", "audio_default_mime",
                     "voice_default_mime", "sticker_img", "sticker_video",
                     "document_pdf_public", "document_pdf_private", "document_normal",
                     "live_photo", "story_video", "story_photo", "poll_media_img",
                     "poll_media_video", "paid_media", "webpage_with_photo",
                     "webpage_without_photo", "webpage_photo_long_text",
                     "file_unique_id_none", "channel_username_none"):
        assert required in snapshot, f"missing spec fragment case: {required}"


# --------------------------------------------------------------------------- #
# 2. Table structure: every render kind has a renderer; the only kind=None entry
#    is WEB_PAGE; PAID_MEDIA has no entry.
# --------------------------------------------------------------------------- #
def test_every_render_kind_has_a_renderer():
    # Fixed-kind lambda entries (branchy selectors are covered by the selector unit
    # tests below and the fragment oracle).
    static = {
        MessageMediaType.PHOTO: 'img_400',
        MessageMediaType.VIDEO: 'video_400',
        MessageMediaType.ANIMATION: 'video_400',
        MessageMediaType.VIDEO_NOTE: 'video_400',
        MessageMediaType.AUDIO: 'audio',
        MessageMediaType.VOICE: 'audio',
        MessageMediaType.LIVE_PHOTO: 'video_loop_400',
    }
    for mt, expected_kind in static.items():
        assert expected_kind in RENDERERS, f"{mt} kind {expected_kind} has no renderer"
    # Selector-produced kinds (document/sticker/story/poll) all resolve to renderers
    # or, for WEB_PAGE, to None.
    for kind in ('img_400', 'video_400', 'audio', 'pdf', 'video_loop_200',
                 'img_200_sticker', 'video_loop_400'):
        assert kind in RENDERERS


def test_web_page_is_the_only_kind_none_entry():
    assert MEDIA_SOURCES[MessageMediaType.WEB_PAGE](SimpleNamespace(web_page=None))[1] is None


def test_paid_media_has_no_table_entry():
    assert MessageMediaType.PAID_MEDIA not in MEDIA_SOURCES


# --------------------------------------------------------------------------- #
# 3. Selector unit tests (the branchy selectors).
# --------------------------------------------------------------------------- #
def test_select_document_pdf_vs_image():
    pdf = SimpleNamespace(document=SimpleNamespace(mime_type="application/pdf", file_unique_id="d1"))
    png = SimpleNamespace(document=SimpleNamespace(mime_type="image/png", file_unique_id="d2"))
    assert _select_document(pdf)[1] == 'pdf'
    assert _select_document(png)[1] == 'img_400'
    assert _select_document(pdf)[0] is pdf.document


def test_select_sticker_video_vs_image():
    vid = SimpleNamespace(sticker=SimpleNamespace(is_video=True, file_unique_id="s1"))
    img = SimpleNamespace(sticker=SimpleNamespace(is_video=False, file_unique_id="s2"))
    assert _select_sticker(vid)[1] == 'video_loop_200'
    assert _select_sticker(img)[1] == 'img_200_sticker'


def test_select_story_maps_helper_kind():
    vid = SimpleNamespace(story=SimpleNamespace(video=SimpleNamespace(file_unique_id="v"), photo=None))
    pic = SimpleNamespace(story=SimpleNamespace(video=None, photo=SimpleNamespace(file_unique_id="p")))
    none = SimpleNamespace(story=None)
    assert _select_story(vid)[1] == 'video_400'
    assert _select_story(pic)[1] == 'img_400'
    assert _select_story(none) == (None, None)


def test_select_poll_media_maps_helper_kind():
    img = SimpleNamespace(poll=SimpleNamespace(description_media=SimpleNamespace(photo=SimpleNamespace(file_unique_id="p"))))
    vid = SimpleNamespace(poll=SimpleNamespace(description_media=SimpleNamespace(video=SimpleNamespace(file_unique_id="v"))))
    none = SimpleNamespace(poll=None)
    assert _select_poll_media(img)[1] == 'img_400'
    assert _select_poll_media(vid)[1] == 'video_400'
    assert _select_poll_media(none) == (None, None)


# --------------------------------------------------------------------------- #
# 4. Renderer byte structure (audio emits two items; pdf emits its two-append block).
# --------------------------------------------------------------------------- #
def test_audio_renderer_emits_tag_and_br():
    out = RENDERERS['audio'](RenderCtx(url="U", mime="audio/mpeg"))
    assert len(out) == 2 and out[1] == '<br>'
    assert 'type="audio/mpeg"' in out[0]


def test_pdf_renderer_emits_two_item_block():
    out = RENDERERS['pdf'](RenderCtx(url="U", tg_link="https://t.me/x/1"))
    assert len(out) == 2
    assert out[0] == '<div class="document-pdf" style="padding: 10px;">'
    assert out[1] == '<a href="https://t.me/x/1" target="_blank">[PDF-файл]</a></div>'


# --------------------------------------------------------------------------- #
# 5. Cross-module invariant: every selector object is resolvable in api_server.
# --------------------------------------------------------------------------- #
def _invariant_messages():
    """One mock per MEDIA_SOURCES type whose selected object carries a real
    file_unique_id/file_id, so find_file_id_in_message can resolve it."""
    def base(**extra):
        m = SimpleNamespace(id=1, chat=SimpleNamespace(id=-100, username="c"), web_page=None, poll=None)
        for a in ("photo", "video", "document", "audio", "voice", "video_note",
                  "animation", "sticker"):
            setattr(m, a, None)
        for k, v in extra.items():
            setattr(m, k, v)
        return m

    media = SimpleNamespace(file_unique_id="UID", file_id="FID")
    cases = {
        MessageMediaType.PHOTO: base(media=MessageMediaType.PHOTO, photo=media),
        MessageMediaType.VIDEO: base(media=MessageMediaType.VIDEO, video=media),
        MessageMediaType.ANIMATION: base(media=MessageMediaType.ANIMATION, animation=media),
        MessageMediaType.VIDEO_NOTE: base(media=MessageMediaType.VIDEO_NOTE, video_note=media),
        MessageMediaType.AUDIO: base(media=MessageMediaType.AUDIO, audio=media),
        MessageMediaType.VOICE: base(media=MessageMediaType.VOICE, voice=media),
        MessageMediaType.DOCUMENT: base(media=MessageMediaType.DOCUMENT,
                                        document=SimpleNamespace(mime_type="image/png", file_unique_id="UID", file_id="FID")),
        MessageMediaType.STICKER: base(media=MessageMediaType.STICKER,
                                       sticker=SimpleNamespace(is_video=False, file_unique_id="UID", file_id="FID")),
        MessageMediaType.LIVE_PHOTO: base(media=MessageMediaType.LIVE_PHOTO, live_photo=media),
        MessageMediaType.STORY: base(media=MessageMediaType.STORY,
                                     story=SimpleNamespace(video=media, photo=None)),
        MessageMediaType.POLL: base(media=MessageMediaType.POLL,
                                    poll=SimpleNamespace(description_media=SimpleNamespace(photo=media))),
        MessageMediaType.WEB_PAGE: base(media=MessageMediaType.WEB_PAGE,
                                        web_page=SimpleNamespace(photo=media)),
    }
    return cases


@pytest.mark.parametrize("media_type", list(MEDIA_SOURCES.keys()))
async def test_selector_object_is_resolvable_in_api_server(media_type):
    """The object MEDIA_SOURCES selects is found by api_server.find_file_id_in_message
    via its file_unique_id — the /media download path can always resolve a URL the
    table produced (spec §5a inter-module invariant)."""
    message = _invariant_messages()[media_type]
    selected_obj, _kind = MEDIA_SOURCES[media_type](message)
    file_unique_id = getattr(selected_obj, "file_unique_id", None)
    assert file_unique_id, f"{media_type} selector returned no usable file_unique_id"
    resolved = await find_file_id_in_message(message, file_unique_id)
    assert resolved == getattr(selected_obj, "file_id", None), \
        f"{media_type}: selected object not resolvable in find_file_id_in_message"


def test_media_sources_covers_all_old_ladder_types():
    """Every type the pre-refactor _get_file_unique_id dict handled is in the table."""
    old_types = {
        MessageMediaType.PHOTO, MessageMediaType.VIDEO, MessageMediaType.DOCUMENT,
        MessageMediaType.AUDIO, MessageMediaType.VOICE, MessageMediaType.VIDEO_NOTE,
        MessageMediaType.ANIMATION, MessageMediaType.STICKER, MessageMediaType.WEB_PAGE,
        MessageMediaType.LIVE_PHOTO, MessageMediaType.STORY, MessageMediaType.POLL,
    }
    assert old_types <= set(MEDIA_SOURCES.keys())


# --------------------------------------------------------------------------- #
# 6. /flags endpoint constraint: flags.append(...) stays inside _extract_flags so
#    inspect.getsource still discovers them.
# --------------------------------------------------------------------------- #
def test_get_all_possible_flags_nonempty_and_known():
    flags = PostParser.get_all_possible_flags()
    assert flags, "flag introspection returned nothing"
    for known in ("video", "audio", "no_image", "sticker", "poll", "fwd"):
        assert known in flags, f"known flag {known} not discovered by /flags introspection"
