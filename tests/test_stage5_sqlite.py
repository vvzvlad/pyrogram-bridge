# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Stage 5 (SQLite access-time batching) tests.

Covers:
- 5.1 accumulator: a /media cache hit records into api_server._access_updates and does NOT
      call update_media_file_access_sync (no synchronous SQLite write on the hot path).
- 5.1 flush: seeding _access_updates + running the flush once writes the accumulated
      timestamps to a real temp DB (hit -> flush -> value updated in DB), and clears the dict.
- 5.1 bulk fn update_media_file_access_bulk_sync: empty no-op, multi-row, and updating an
      EXISTING row's `added` (WHERE matches on the str channel key).
- 5.1 snapshot-then-clear: an update arriving DURING the flush lands in the fresh dict and
      is not lost.
- gotcha: str(channel) key discipline — an int-ish channel on the hot path keys the
      accumulator (and thus the UPDATE) by the string form.
"""
import os
import sys
import sqlite3
import asyncio
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.modules['config'] = __import__('tests.mock_config', fromlist=['get_settings'])

import api_server
from file_io import init_db_sync, update_media_file_access_bulk_sync


def _fake_plain_message():
    """A non-poll, non-video message so download_media_file takes the normal cache flow."""
    return SimpleNamespace(media=None, video=None, empty=False)


@pytest.fixture(autouse=True)
def _clean_accumulator():
    """Each test starts with an empty accumulator and restores it afterwards."""
    api_server._access_updates = {}
    yield
    api_server._access_updates = {}


# --------------------------------------------------------------------------- #
# 5.1 hot path: cache hit records into the accumulator, no synchronous SQLite.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cache_hit_records_accumulator_no_sqlite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    channel, post_id, fid = "testchan", 5, "fidHIT"
    cache_dir = tmp_path / "data" / "cache" / channel / str(post_id)
    cache_dir.mkdir(parents=True)
    (cache_dir / fid).write_bytes(b"cached-bytes")

    async def fake_get(_cid, _pid):
        return _fake_plain_message()
    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get)

    # Spy: the single-row synchronous updater must NOT be called on the hot path.
    called = []
    monkeypatch.setattr(api_server, "update_media_file_access_sync",
                        lambda *a, **k: called.append(a))

    path, delete_after = await api_server.download_media_file(channel, post_id, fid)

    assert path == str(cache_dir / fid)
    assert delete_after is False
    assert called == []  # no synchronous SQLite access-write happened
    assert (channel, post_id, fid) in api_server._access_updates


# --------------------------------------------------------------------------- #
# 5.1 hot path (DoD guard): get_media's pre-semaphore cache-hit — the hottest
# changed site, the one this PR exists for — records into the accumulator and
# does NO synchronous SQLite access-write. Mirrors the download_media_file spy
# so a regression re-introducing a per-hit write into THIS branch goes red.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_media_pre_semaphore_cache_hit_no_sqlite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    channel, post_id, fid = "gmchan", 11, "fidGM"
    cache_dir = tmp_path / "data" / "cache" / channel / str(post_id)
    cache_dir.mkdir(parents=True)
    (cache_dir / fid).write_bytes(b"cached-bytes")

    # Bypass the HMAC digest gate and the FileResponse machinery — the test targets
    # only the access-time write on the pre-semaphore cache-hit branch.
    monkeypatch.setattr(api_server, "verify_media_digest", lambda *a, **k: True)
    sentinel = object()
    async def fake_prepare(cache_path, request=None, media_key=None):
        return sentinel
    monkeypatch.setattr(api_server, "prepare_file_response", fake_prepare)

    # Spy: the single-row synchronous updater must NOT be called on the hot path.
    called = []
    monkeypatch.setattr(api_server, "update_media_file_access_sync",
                        lambda *a, **k: called.append(a))

    resp = await api_server.get_media(channel, post_id, fid, request=object(), digest="x")

    assert resp is sentinel
    assert called == []  # no synchronous SQLite access-write on the hottest path
    assert (channel, post_id, fid) in api_server._access_updates


# --------------------------------------------------------------------------- #
# gotcha: str(channel) key discipline on the hot path (int-ish channel).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cache_hit_keys_channel_as_str(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    channel_int, post_id, fid = 12345, 7, "fidINT"
    cache_dir = tmp_path / "data" / "cache" / str(channel_int) / str(post_id)
    cache_dir.mkdir(parents=True)
    (cache_dir / fid).write_bytes(b"x")

    async def fake_get(_cid, _pid):
        return _fake_plain_message()
    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get)

    await api_server.download_media_file(channel_int, post_id, fid)

    assert ("12345", post_id, fid) in api_server._access_updates      # string form
    assert (channel_int, post_id, fid) not in api_server._access_updates  # never the raw int


# --------------------------------------------------------------------------- #
# 5.1 flush: hit -> flush -> the accumulated timestamp is written to the DB.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_flush_writes_accumulated_timestamps(tmp_path, monkeypatch):
    db = str(tmp_path / "flush.db")
    init_db_sync(db)
    # Seed two existing rows with an old timestamp.
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO media_file_ids (channel, post_id, file_unique_id, added) VALUES (?,?,?,?)",
        [("chA", 1, "fA", 1.0), ("chB", 2, "fB", 2.0)],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(api_server, "DB_PATH", db)
    api_server._access_updates = {
        ("chA", 1, "fA"): 111.0,
        ("chB", 2, "fB"): 222.0,
    }

    await api_server._flush_access_updates()

    # Dict cleared after flush.
    assert api_server._access_updates == {}

    conn = sqlite3.connect(db)
    rows = dict(((c, p, f), a) for c, p, f, a in
                conn.execute("SELECT channel, post_id, file_unique_id, added FROM media_file_ids"))
    conn.close()
    assert rows[("chA", 1, "fA")] == 111.0
    assert rows[("chB", 2, "fB")] == 222.0


@pytest.mark.asyncio
async def test_flush_empty_is_noop(monkeypatch):
    api_server._access_updates = {}
    # Must not raise and must not touch the threadpool/DB.
    monkeypatch.setattr(api_server, "update_media_file_access_bulk_sync",
                        lambda *a, **k: pytest.fail("bulk sync should not run for an empty batch"))
    await api_server._flush_access_updates()


# --------------------------------------------------------------------------- #
# 5.1 snapshot-then-clear: an update arriving DURING the flush is not lost.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_snapshot_then_clear_does_not_lose_late_update(tmp_path, monkeypatch):
    db = str(tmp_path / "race.db")
    init_db_sync(db)
    monkeypatch.setattr(api_server, "DB_PATH", db)

    late_key = ("chLate", 9, "fLate")

    def fake_bulk(_db, entries):
        # Simulates a cache hit landing WHILE the flush's to_thread runs: because the flush
        # already replaced the module dict with a fresh one, this write goes into the NEW dict.
        api_server._access_updates[late_key] = 999.0

    monkeypatch.setattr(api_server, "update_media_file_access_bulk_sync", fake_bulk)

    api_server._access_updates = {("chA", 1, "fA"): 111.0}
    await api_server._flush_access_updates()

    # The snapshot (chA) was flushed and cleared; the late update survives in the new dict.
    assert api_server._access_updates == {late_key: 999.0}


# --------------------------------------------------------------------------- #
# 5.1 re-queue on failure: a failed bulk write is not lost; setdefault keeps a
# fresher write that arrived during the flush.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_failed_flush_requeues_batch_without_clobbering_fresh(tmp_path, monkeypatch):
    db = str(tmp_path / "fail.db")
    init_db_sync(db)
    monkeypatch.setattr(api_server, "DB_PATH", db)

    stale_key = ("chStale", 1, "fS")   # only in the failing snapshot
    fresh_key = ("chFresh", 2, "fF")   # re-written FRESHER during the flush

    def fake_bulk(_db, _entries):
        # A newer cache hit for fresh_key lands in the fresh dict WHILE the bulk write runs,
        # then the write fails. The re-queue must restore stale_key but must NOT overwrite
        # the newer fresh_key value (setdefault, not assignment).
        api_server._access_updates[fresh_key] = 999.0
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(api_server, "update_media_file_access_bulk_sync", fake_bulk)

    api_server._access_updates = {stale_key: 1.0, fresh_key: 2.0}
    with pytest.raises(sqlite3.OperationalError):
        await api_server._flush_access_updates()

    # stale_key restored with its snapshot value; fresh_key keeps the NEWER value (not 2.0).
    assert api_server._access_updates == {stale_key: 1.0, fresh_key: 999.0}


# --------------------------------------------------------------------------- #
# 5.1 bulk fn: empty no-op, multi-row, and existing-row update via str channel key.
# --------------------------------------------------------------------------- #
def test_bulk_access_update_real_sql(tmp_path):
    db = str(tmp_path / "bulk.db")
    init_db_sync(db)

    # Empty batch is a no-op (no crash).
    update_media_file_access_bulk_sync(db, [])

    # Seed existing rows.
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO media_file_ids (channel, post_id, file_unique_id, added) VALUES (?,?,?,?)",
        [("chA", 1, "fA", 1.0), ("chB", 2, "fB", 2.0)],
    )
    conn.commit()
    conn.close()

    # Multi-row update of EXISTING rows: WHERE matches on the str channel key.
    update_media_file_access_bulk_sync(db, [
        ("chA", 1, "fA", 500.0),
        ("chB", 2, "fB", 600.0),
    ])

    conn = sqlite3.connect(db)
    rows = dict(((c, p, f), a) for c, p, f, a in
                conn.execute("SELECT channel, post_id, file_unique_id, added FROM media_file_ids"))
    # A row keyed by an int channel does NOT match the string "chA" WHERE (documents the gotcha).
    n = conn.execute("SELECT COUNT(*) FROM media_file_ids WHERE channel = 1").fetchone()[0]
    conn.close()
    assert rows[("chA", 1, "fA")] == 500.0
    assert rows[("chB", 2, "fB")] == 600.0
    assert n == 0
