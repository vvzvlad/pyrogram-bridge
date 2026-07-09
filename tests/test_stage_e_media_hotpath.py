# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
"""
Issue #26 package E — media-cache path helper + in-memory MIME cache on the hot /media path.

Covers:
- media_cache_path builds EXACTLY the pre-existing on-disk layout (path-format regression).
- The in-memory _mime_types dict short-circuits the SQLite MIME read on a repeat hit
  (get_mime_type_sync not called a second time; no new SQLite connection opened).
- _mime_types overflow (>= _MIME_CACHE_MAX) clears the dict without raising and repopulates.
"""
import os

import pytest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import file_io
import api_server


BODY = bytes(range(256)) * 8  # 2048 deterministic bytes


def _make_client(file_path, media_key=None):
    """Drive prepare_file_response through real ASGI (as the stage-3 tests do)."""
    app = FastAPI()

    @app.get("/f")
    async def _serve(request: Request):
        return await api_server.prepare_file_response(file_path, request=request, media_key=media_key)

    return TestClient(app)


@pytest.fixture
def sample_file(tmp_path):
    fp = tmp_path / "myfile.bin"
    fp.write_bytes(BODY)
    return str(fp)


# --------------------------------------------------------------------------- #
# Task 15 — media_cache_path path-format regression.
# --------------------------------------------------------------------------- #
def test_media_cache_path_layout():
    root = api_server.MEDIA_CACHE_DIR
    # The constant must resolve to the exact absolute string every legacy site built.
    assert root == os.path.abspath("./data/cache")

    # Without file_unique_id -> the per-post directory <root>/<channel>/<post_id>.
    assert api_server.media_cache_path("mychan", 42) == os.path.join(root, "mychan", "42")

    # With file_unique_id -> the full per-file path <root>/<channel>/<post_id>/<fid>.
    assert api_server.media_cache_path("mychan", 42, "fidABC") == os.path.join(root, "mychan", "42", "fidABC")

    # Faithful drop-in: identical to the old manual assembly (incl. int channel stringified).
    assert api_server.media_cache_path(-100123, 7, "fidABC") == os.path.join(
        os.path.abspath("./data/cache"), "-100123", "7", "fidABC"
    )


# --------------------------------------------------------------------------- #
# Task 16 — in-memory MIME cache short-circuits the SQLite read on a repeat hit.
# --------------------------------------------------------------------------- #
def test_second_request_served_from_dict_not_sqlite(sample_file, monkeypatch):
    calls = {"get": 0, "magic": 0}

    def fake_get(db, ch, pid, fid):
        calls["get"] += 1
        return "video/mp4"  # SQLite HIT on the first (dict-miss) request

    def fake_magic(_path):
        calls["magic"] += 1
        return "application/octet-stream"

    monkeypatch.setattr(api_server, "get_mime_type_sync", fake_get)
    monkeypatch.setattr(api_server.magic_mime, "from_file", fake_magic)

    key = ("chanE", 100, "fidE")
    c = _make_client(sample_file, media_key=key)

    r1 = c.get("/f")
    r2 = c.get("/f")

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.headers["content-type"].startswith("video/mp4")
    assert r2.headers["content-type"].startswith("video/mp4")
    # First request populated the dict from SQLite; the second is served from the dict.
    assert calls["get"] == 1
    assert calls["magic"] == 0
    assert api_server._mime_types[key] == "video/mp4"


def test_repeat_hit_opens_no_new_db_connection(tmp_path, sample_file, monkeypatch):
    """Acceptance recipe: monkeypatch file_io._open_db with a counter; two consecutive
    requests for the same file — the counter must NOT increase on the second."""
    db_path = str(tmp_path / "media.db")
    file_io.init_db_sync(db_path)
    # Seed a row whose MIME is already stored, so the first request reads it from SQLite
    # (no python-magic, no set-write) and caches it in the dict.
    file_io.upsert_media_file_id_sync(db_path, "chanDB", 55, "fidDB", 0.0)
    file_io.set_mime_type_sync(db_path, "chanDB", 55, "fidDB", "image/png")

    monkeypatch.setattr(api_server, "DB_PATH", db_path)

    count = {"n": 0}
    orig_open = file_io._open_db

    def counting_open(p):
        count["n"] += 1
        return orig_open(p)

    monkeypatch.setattr(file_io, "_open_db", counting_open)

    c = _make_client(sample_file, media_key=("chanDB", 55, "fidDB"))

    r1 = c.get("/f")
    after_first = count["n"]
    r2 = c.get("/f")
    after_second = count["n"]

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.headers["content-type"].startswith("image/png")
    assert r2.headers["content-type"].startswith("image/png")
    assert after_first >= 1  # the first (dict-miss) hit did open a connection for the read
    # The repeat hit opened NO new SQLite connection.
    assert after_second == after_first


# --------------------------------------------------------------------------- #
# Task 16 — overflow clears the dict without raising, then repopulates.
# --------------------------------------------------------------------------- #
def test_mime_cache_overflow_clears_and_repopulates(sample_file, monkeypatch):
    def fake_get(db, ch, pid, fid):
        return "text/plain"

    monkeypatch.setattr(api_server, "get_mime_type_sync", fake_get)
    monkeypatch.setattr(api_server.magic_mime, "from_file", lambda _p: "text/plain")

    # Fill the dict up to the bound with dummy entries.
    api_server._mime_types.clear()
    for i in range(api_server._MIME_CACHE_MAX):
        api_server._mime_types[("dummy", i, "f")] = "x/y"
    assert len(api_server._mime_types) == api_server._MIME_CACHE_MAX

    key = ("chanOverflow", 1, "fidOverflow")
    c = _make_client(sample_file, media_key=key)

    r = c.get("/f")  # must clear-all on overflow, then insert the new key — no exception
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    # Cleared then repopulated with just the one fresh key.
    assert len(api_server._mime_types) == 1
    assert api_server._mime_types[key] == "text/plain"

    # The next request is now a clean dict hit.
    r2 = c.get("/f")
    assert r2.status_code == 200
