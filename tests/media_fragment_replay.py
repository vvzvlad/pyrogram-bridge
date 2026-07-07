# flake8: noqa
# pylint: disable=import-outside-toplevel, missing-function-docstring, line-too-long
"""Stage-5 fragment-level snapshot helpers (render-pipeline refactor epic, issue #32/#34).

Captures the raw `_generate_html_media` output (BEFORE sanitize) for every media
render kind and edge branch, as a SEPARATE oracle layer from the stage-0 feed goldens
(spec §4: "два слоя эталонов, не смешивать").

`media_fragments.json` is the PRE-REFACTOR BASE reference: it was captured by running
THIS harness against the BASE `post_parser.py` checkout (the pre-refactor code), NOT
against stage-5 code — so it is a genuine base-anchored golden layer, not a circular
self-snapshot of the refactor. The 5a refactor (MEDIA_SOURCES table + renderers) must
reproduce these base fragments byte-for-byte. The ONLY 5b-registered changes vs the base
are the two entries in REGISTERED_DELTAS (§3.14 unclosed-div close); the §3.13 large-file
guard is collection-only and changes no fragment bytes.

Cases whose type exists in the recorded corpus could be pulled from it, but the media
FRAGMENT is a pure function of a single Message, so deterministic hand-built mocks give
the same bytes with far less machinery and also reach the mock-only exotic types
(PAID_MEDIA, LIVE_PHOTO, STORY) — exactly the set the spec allows mocks for.

Run `python -m tests.media_fragment_replay` from the repo root to (re)generate the snapshot.
"""
import os
import json
from types import SimpleNamespace

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
SNAPSHOT_PATH = os.path.join(TESTS_DIR, "test_data", "media_fragments.json")

# Fixed signing key so digests in the snapshot reproduce on any checkout (mirrors the
# stage-0 golden pin). Applied by the generator and by the comparison test.
FRAGMENT_SIGNING_KEY = "stage5-fragment-fixed-signing-key-000000000000"


class _Str(str):
    """Minimal stand-in for Pyrogram's Str: .html returns the raw string unchanged."""
    @property
    def html(self):
        return str(self)


def _msg(mid, media, *, text=None, username="testchan", chat_id=-1001234567890, **extra):
    """Build a Message-like mock with the attributes _generate_html_media touches.

    Top-level media attributes default to None (truthy checks in _save_media_file_ids);
    new Kurigram 2.2.23 attributes (live_photo/story/giveaway/...) are intentionally
    absent unless passed, so getattr-only production code is exercised as on real objects.
    """
    m = SimpleNamespace()
    m.id = mid
    m.media = media
    m.text = _Str(text) if text is not None else None
    m.caption = None
    m.web_page = None
    m.poll = None
    m.paid_media = None
    m.forward_origin = None
    m.show_caption_above_media = False
    if chat_id is None and username is None:
        m.chat = None
    else:
        m.chat = SimpleNamespace(id=chat_id, username=username)
    for attr in ("photo", "video", "document", "audio", "voice",
                 "video_note", "animation", "sticker"):
        setattr(m, attr, None)
    for key, value in extra.items():
        setattr(m, key, value)
    return m


def _obj(**kw):
    return SimpleNamespace(**kw)


# --------------------------------------------------------------------------- #
# Fragment cases. Each entry: name -> Message factory.
# Covers the full spec §5a "Шаг 0" list + edge branches.
# --------------------------------------------------------------------------- #
def _webpage(photo=None, url="https://example.com", title="Example",
             description=None, site_name=None, wp_type=""):
    return _obj(photo=photo, url=url, title=title, description=description,
                site_name=site_name, type=wp_type, display_url=None)


def build_cases():
    from pyrogram.enums import MessageMediaType as T
    cases = {}
    cases["photo"] = lambda: _msg(1, T.PHOTO, photo=_obj(file_unique_id="ph_uid", file_id="ph_fid"))
    cases["video"] = lambda: _msg(2, T.VIDEO, video=_obj(file_unique_id="vid_uid", file_id="vid_fid", file_size=1024))
    cases["animation"] = lambda: _msg(3, T.ANIMATION, animation=_obj(file_unique_id="ani_uid", file_id="ani_fid"))
    cases["video_note"] = lambda: _msg(4, T.VIDEO_NOTE, video_note=_obj(file_unique_id="vn_uid", file_id="vn_fid"))
    cases["audio_default_mime"] = lambda: _msg(5, T.AUDIO, audio=_obj(file_unique_id="au_uid", file_id="au_fid"))
    cases["audio_explicit_mime"] = lambda: _msg(6, T.AUDIO, audio=_obj(file_unique_id="au2_uid", file_id="au2_fid", mime_type="audio/flac"))
    cases["voice_default_mime"] = lambda: _msg(7, T.VOICE, voice=_obj(file_unique_id="vo_uid", file_id="vo_fid"))
    cases["voice_explicit_mime"] = lambda: _msg(8, T.VOICE, voice=_obj(file_unique_id="vo2_uid", file_id="vo2_fid", mime_type="audio/wav"))
    cases["sticker_img"] = lambda: _msg(9, T.STICKER, sticker=_obj(file_unique_id="st_uid", file_id="st_fid", emoji="😀", is_video=False))
    cases["sticker_video"] = lambda: _msg(10, T.STICKER, sticker=_obj(file_unique_id="stv_uid", file_id="stv_fid", emoji="🎬", is_video=True))
    cases["document_pdf_public"] = lambda: _msg(11, T.DOCUMENT, username="pubchan", chat_id=None,
                                                document=_obj(file_unique_id="pdf_uid", file_id="pdf_fid", mime_type="application/pdf"))
    cases["document_pdf_private"] = lambda: _msg(12, T.DOCUMENT, username=None, chat_id=-1009876543210,
                                                 document=_obj(file_unique_id="pdf2_uid", file_id="pdf2_fid", mime_type="application/pdf"))
    cases["document_normal"] = lambda: _msg(13, T.DOCUMENT, document=_obj(file_unique_id="doc_uid", file_id="doc_fid", mime_type="image/png"))
    cases["live_photo"] = lambda: _msg(14, T.LIVE_PHOTO, live_photo=_obj(file_unique_id="lp_uid", file_id="lp_fid"))
    cases["story_video"] = lambda: _msg(15, T.STORY, story=_obj(video=_obj(file_unique_id="sv_uid", file_id="sv_fid"), photo=None))
    cases["story_photo"] = lambda: _msg(16, T.STORY, story=_obj(video=None, photo=_obj(file_unique_id="sp_uid", file_id="sp_fid")))
    cases["poll_media_img"] = lambda: _msg(17, T.POLL, poll=_obj(question=_Str("Q?"), options=[],
                                           description_media=_obj(photo=_obj(file_unique_id="pl_uid", file_id="pl_fid"))))
    cases["poll_media_video"] = lambda: _msg(18, T.POLL, poll=_obj(question=_Str("Q?"), options=[],
                                             description_media=_obj(video=_obj(file_unique_id="plv_uid", file_id="plv_fid"))))
    cases["paid_media"] = lambda: _msg(19, T.PAID_MEDIA,
                                       paid_media=_obj(stars_amount=50, media=[_obj(), _obj()]))
    # WEB_PAGE with photo: opens an EMPTY message-media div (no elif matches WEB_PAGE),
    # plus the webpage-preview block (short text gate <=10).
    cases["webpage_with_photo"] = lambda: _msg(20, T.WEB_PAGE, text="hi",
                                               web_page=_webpage(photo=_obj(file_unique_id="wp_uid", file_id="wp_fid")))
    # WEB_PAGE without photo: file_unique_id is None -> message-media div opened and
    # left UNCLOSED (§3.14 target). Short text so the preview block renders.
    cases["webpage_without_photo"] = lambda: _msg(21, T.WEB_PAGE, text="hi",
                                                  web_page=_webpage(photo=None))
    # WEB_PAGE with photo but long text (>10): preview gate closed, only the empty media div.
    cases["webpage_photo_long_text"] = lambda: _msg(22, T.WEB_PAGE,
                                                    text="this text is definitely longer than ten characters",
                                                    web_page=_webpage(photo=_obj(file_unique_id="wpl_uid", file_id="wpl_fid")))
    # file_unique_id is None on a normal media type -> unclosed div (§3.14 target).
    cases["file_unique_id_none"] = lambda: _msg(23, T.PHOTO, photo=_obj(file_unique_id=None, file_id="x_fid"))
    # channel_username is None but uid present -> the div IS closed on the guard branch.
    cases["channel_username_none"] = lambda: _msg(24, T.PHOTO, username=None, chat_id=555,
                                                  photo=_obj(file_unique_id="cu_uid", file_id="cu_fid"))
    return cases


# --------------------------------------------------------------------------- #
# Registered 5b deltas vs the pre-refactor base snapshot.
#
# Spec §3.14: the empty <div class="message-media"> container is now CLOSED in every
# render branch. The base (pre-refactor) code left it OPEN whenever the selected media
# object had no usable file_unique_id, so the base snapshot captured an unbalanced div
# for exactly these two cases. `collected` is byte-identical to the base; only `html`
# differs by the added `</div>`. These are the ONLY fragments the 5b registered fixes
# change relative to the pre-refactor base — every other case reproduces base bytes.
REGISTERED_DELTAS = {
    "file_unique_id_none": {
        "collected": [],
        "html": "<div class=\"message-media\">\n</div>",
    },
    "webpage_without_photo": {
        "collected": [],
        "html": "<div class=\"message-media\">\n</div>\n<div class=\"webpage-preview\">\n<div class=\"webpage-preview\" style=\"border-left: 3px solid #ccc; padding-left: 10px; margin: 10px 0;\">\n<div class=\"webpage-title\" style=\"font-weight:bold; margin:5px 0;\">\n<a href=\"https://example.com\" target=\"_blank\">Example</a></div>\n<div class=\"webpage-url\" style=\"color:#666; font-size:0.9em;margin-bottom:5px;\">https://example.com</div>\n</div>\n</div>",
    },
}


# --------------------------------------------------------------------------- #
# Capture / compare.
# --------------------------------------------------------------------------- #
def capture_fragments():
    """Return {case_name: {"html": fragment, "collected": [[chan, id, fuid], ...]}}.

    Covers all three ladders in one shot: _generate_html_media renders the fragment
    (render ladder + _get_file_unique_id ladder) and calls _save_media_file_ids
    (collection ladder), whose result is read off _pending_media_ids."""
    from post_parser import PostParser
    parser = PostParser(SimpleNamespace())
    out = {}
    for name, factory in build_cases().items():
        parser._pending_media_ids = []
        html = parser._generate_html_media(factory())
        collected = [[c, p, f] for c, p, f, _ in parser._pending_media_ids]
        out[name] = {"html": html, "collected": collected}
    return out


def pin_signing_key(monkeypatch):
    from url_signer import KeyManager
    monkeypatch.setattr(KeyManager, "signing_key", FRAGMENT_SIGNING_KEY)


def load_snapshot():
    with open(SNAPSHOT_PATH, encoding="utf-8") as f:
        return json.load(f)


def _bootstrap_standalone():
    import sys
    import time
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import tests.mock_config as _mock_config
    sys.modules["config"] = _mock_config
    os.environ["TZ"] = "UTC"
    time.tzset()


def generate_snapshot():
    # WARNING: this MUST be run against the BASE (pre-refactor) `post_parser.py`, never
    # against stage-5 code. Regenerating from stage-5 output would make the oracle a
    # circular self-snapshot instead of a base-anchored golden layer (spec §4).
    from url_signer import KeyManager
    KeyManager.signing_key = FRAGMENT_SIGNING_KEY
    data = capture_fragments()
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"wrote {len(data)} fragment snapshots to {SNAPSHOT_PATH}")


if __name__ == "__main__":
    _bootstrap_standalone()
    generate_snapshot()
