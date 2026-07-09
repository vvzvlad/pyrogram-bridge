# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Issue #25 (package D) regressions:

  * Задание 12 — the cache sweep must purge a >20-day-old SQLite row whose file is already
    gone from disk EVEN when nothing was removed from disk in that pass (files_removed == 0).
    Previously the DB diff ran only `if files_removed > 0`, so such a fileless-but-expired
    row stayed in the table forever.
  * Задание 14 — download_new_files must not re-enqueue an already-queued/in-flight file on
    every sweep pass (dedup via the module-level _queued_media set), and the worker must free
    the dedup slot in its finally so the file can be re-enqueued later.
"""
import sqlite3
from datetime import datetime

import pytest

import api_server
from file_io import init_db_sync, get_all_media_file_ids_sync


class _StopLoop(Exception):
    """Sentinel used to break the otherwise-infinite background loops after one iteration."""


# --------------------------------------------------------------------------- #
# Задание 12 — expired row with a missing file is purged when files_removed == 0
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_expired_row_missing_file_purged_when_no_disk_removal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "sweep.db")
    init_db_sync(db)
    monkeypatch.setattr(api_server, "DB_PATH", db)

    # A row 21 days old whose cache file does NOT exist on disk. remove_old_cached_files_sync
    # drops it from the surviving list without incrementing files_removed -> files_removed == 0.
    old_ts = datetime.now().timestamp() - 21 * 24 * 3600
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO media_file_ids (channel, post_id, file_unique_id, added) VALUES (?,?,?,?)",
        ("ghostchan", 42, "ghostfid", old_ts),
    )
    conn.commit()
    conn.close()

    # Run exactly one sweep iteration, then break out of the while-True loop.
    async def fake_sleep(_delay):
        raise _StopLoop
    monkeypatch.setattr(api_server.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await api_server.cache_media_files()

    # The expired, fileless row is gone despite no file having been removed from disk.
    assert get_all_media_file_ids_sync(db) == []


# --------------------------------------------------------------------------- #
# Задание 14 — dedup: two identical passes enqueue exactly one item
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_download_new_files_dedups_across_passes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cache_dir = str(tmp_path / "cache")

    api_server._queued_media.clear()
    while not api_server.download_queue.empty():
        api_server.download_queue.get_nowait()

    media_files = [{"channel": "dupchan", "post_id": 7, "file_unique_id": "dupfid"}]

    await api_server.download_new_files(media_files, cache_dir)
    await api_server.download_new_files(media_files, cache_dir)

    assert api_server.download_queue.qsize() == 1
    assert ("dupchan", 7, "dupfid") in api_server._queued_media

    # Cleanup shared module state.
    api_server._queued_media.clear()
    while not api_server.download_queue.empty():
        api_server.download_queue.get_nowait()


# --------------------------------------------------------------------------- #
# Задание 14 — the worker frees the dedup slot so the file can be re-enqueued later
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_worker_clears_queued_key_after_processing(monkeypatch):
    key = ("wchan", 3, "wfid")
    api_server._queued_media.clear()
    api_server._queued_media.add(key)
    while not api_server.download_queue.empty():
        api_server.download_queue.get_nowait()
    api_server.download_queue.put_nowait(("wchan", 3, "wfid"))

    async def fake_download(channel, post_id, file_unique_id):
        return ("/x", False)
    monkeypatch.setattr(api_server, "download_media_file", fake_download)

    async def fake_sleep(_delay):
        return None
    monkeypatch.setattr(api_server.asyncio, "sleep", fake_sleep)

    # Break the while-True worker loop after it has run its finally exactly once.
    real_task_done = api_server.download_queue.task_done
    def stop_task_done():
        real_task_done()
        raise _StopLoop
    monkeypatch.setattr(api_server.download_queue, "task_done", stop_task_done)

    with pytest.raises(_StopLoop):
        await api_server.background_download_worker()

    # Slot freed -> a later sweep may re-enqueue the (still missing) file.
    assert key not in api_server._queued_media

    api_server._queued_media.clear()
