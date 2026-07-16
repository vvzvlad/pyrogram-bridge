# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Issue #55 — never serve active content inline from our own origin.

`prepare_file_response` still sniffs the MIME (and persists it to the type cache), but the
RESPONSE content type is now chosen from an allowlist of passive image/video/audio types:
  - allowlisted (e.g. image/png)  -> served inline with that exact type
  - anything else (text/html, image/svg+xml, application/*) -> served as a neutralized
    attachment with Content-Type: application/octet-stream (NOT refused)
Every /media response also carries X-Content-Type-Options: nosniff and a sandbox CSP, on
both the 200 and 206 (Range) paths.
"""
import io
import os
import struct

import pytest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import api_server

try:
    from PIL import Image
    _HAVE_PIL = True
except Exception:  # pragma: no cover - PIL is a hard dep in this repo
    _HAVE_PIL = False


# A minimal but real PNG (magic sniffs it as image/png).
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da6360000002000154a24f5f0000000049454e44ae426082"
)
HTML_BYTES = b"<!DOCTYPE html><html><body><script>alert(document.cookie)</script></body></html>"
SVG_BYTES = (
    b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg">'
    b'<script>alert(1)</script></svg>'
)
CSP_VALUE = "sandbox; default-src 'none'"


def _make_client(file_path):
    app = FastAPI()

    @app.get("/f")
    async def _serve(request: Request):
        return await api_server.prepare_file_response(file_path, request=request)

    return TestClient(app)


def _write(tmp_path, name, data):
    fp = tmp_path / name
    fp.write_bytes(data)
    return str(fp)


def _assert_security_headers(r):
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("content-security-policy") == CSP_VALUE


def test_html_bytes_served_as_neutralized_attachment(tmp_path):
    fp = _write(tmp_path, "payload.bin", HTML_BYTES)
    r = _make_client(fp).get("/f")
    assert r.status_code == 200
    # NOT served inline as text/html — neutralized to an octet-stream attachment.
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert "text/html" not in r.headers["content-type"]
    _assert_security_headers(r)
    # Body is still retrievable (we neutralize, we do not refuse).
    assert r.content == HTML_BYTES


def test_svg_served_as_attachment_not_inline(tmp_path):
    fp = _write(tmp_path, "vector.bin", SVG_BYTES)
    r = _make_client(fp).get("/f")
    assert r.status_code == 200
    # SVG is an image but executes scripts -> deliberately excluded from the inline allowlist.
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert "svg" not in r.headers["content-type"]
    _assert_security_headers(r)


def test_png_served_inline_with_nosniff(tmp_path):
    fp = _write(tmp_path, "real.png", PNG_BYTES)
    r = _make_client(fp).get("/f")
    assert r.status_code == 200
    # A genuine passive image is served inline with its exact allowlisted type.
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"].startswith("inline")
    _assert_security_headers(r)
    assert r.content == PNG_BYTES


def test_svg_is_not_in_inline_allowlist():
    # Guard against anyone "helpfully" adding SVG to the inline set.
    assert "image/svg+xml" not in api_server._INLINE_SAFE_CONTENT_TYPES
    assert "text/html" not in api_server._INLINE_SAFE_CONTENT_TYPES


def test_range_206_still_carries_security_headers(tmp_path):
    # The headers must survive on the 206 Partial Content path, not only the 200 path.
    fp = _write(tmp_path, "real.png", PNG_BYTES)
    r = _make_client(fp).get("/f", headers={"Range": "bytes=0-10"})
    assert r.status_code == 206
    assert r.headers["content-type"] == "image/png"
    _assert_security_headers(r)
    assert r.content == PNG_BYTES[:11]


# --- Positive inline cases: the profile Telegram media types must be served INLINE with
# their exact declared content type. These guard against the allowlist drifting away from
# what python-magic/libmagic actually emits (issue #55 regression: the allowlist used
# canonical IANA names like audio/wav/audio/mp4 while libmagic returns audio/x-wav /
# audio/x-m4a, so legit voice/audio silently flipped to attachment). Each fixture is a
# minimal but real container that libmagic sniffs to the expected type. -----------------

def _pil_bytes(fmt):
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (123, 45, 67)).save(buf, fmt)
    return buf.getvalue()


def _mp4_box(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _m4a_bytes():
    # ISO-BMFF with major brand "M4A " -> libmagic sniffs audio/x-m4a.
    ftyp = _mp4_box(b"ftyp", b"M4A " + struct.pack(">I", 0) + b"M4A mp42isom")
    return ftyp + _mp4_box(b"mdat", b"\x00" * 16)


def _mp4_video_bytes():
    # ISO-BMFF with major brand "isom" -> libmagic sniffs video/mp4.
    ftyp = _mp4_box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2avc1mp41")
    return ftyp + _mp4_box(b"mdat", b"\x00" * 16)


def _ogg_audio_bytes():
    # Minimal Ogg page carrying a Vorbis identification header -> audio/ogg.
    body = b"\x01vorbis" + struct.pack("<IBIIIIB", 0, 2, 44100, 0, 0, 0, 0)[:22]
    seg = bytes([len(body)])
    header = (
        b"OggS" + bytes([0]) + bytes([2]) + struct.pack("<q", 0)
        + struct.pack("<I", 0) + struct.pack("<I", 0) + struct.pack("<I", 0)
        + bytes([1]) + seg
    )
    return header + body


# (fixture-builder, expected libmagic mime == expected inline Content-Type)
_INLINE_FIXTURES = [
    ("jpeg", lambda: _pil_bytes("JPEG"), "image/jpeg"),
    ("webp", lambda: _pil_bytes("WEBP"), "image/webp"),
    ("mp4", _mp4_video_bytes, "video/mp4"),
    ("ogg_audio", _ogg_audio_bytes, "audio/ogg"),
    ("m4a", _m4a_bytes, "audio/x-m4a"),  # issue #55 regression case
]


@pytest.mark.parametrize("label,builder,expected_type", _INLINE_FIXTURES)
def test_profile_media_served_inline(tmp_path, label, builder, expected_type):
    if label in ("jpeg", "webp") and not _HAVE_PIL:
        pytest.skip("PIL not available to build image fixture")
    data = builder()
    fp = _write(tmp_path, f"real.{label}", data)

    # Sanity: this fixture must actually sniff to the type we assert, otherwise the test
    # would be vacuous. This is what catches the allowlist<->libmagic desync.
    import magic
    sniffed = magic.Magic(mime=True).from_file(fp)
    assert sniffed == expected_type, (
        f"{label}: libmagic returned {sniffed!r}, expected {expected_type!r}; "
        f"fixture builder or _INLINE_SAFE_CONTENT_TYPES needs updating"
    )
    assert expected_type in api_server._INLINE_SAFE_CONTENT_TYPES

    r = _make_client(fp).get("/f")
    assert r.status_code == 200
    assert r.headers["content-type"] == expected_type
    assert r.headers["content-disposition"].startswith("inline")
    _assert_security_headers(r)
    assert r.content == data
