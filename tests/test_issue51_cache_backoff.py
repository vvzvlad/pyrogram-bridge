# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Regression tests for issue #51 — background cache download: working backoff + dedup.

Symptom fixed here: dead media files were retried on every ~15 min sweep for up to 20 days,
the same file could download twice at once (live + background), and empty cache dirs piled up.

  1. Backoff cap >= the sweep interval, so a dead file is skipped for whole sweeps instead of
     being re-queued every pass (the retry-storm). Cap is env-tunable (MEDIA_BACKOFF_MAX_S).
  2. After N consecutive failures the media row is dropped from SQLite (MEDIA_FAILURES_DROP_ROW).
  3. The background worker downloads via _download_deduped (shared with live requests) so one
     file is fetched once, and it does NOT keep its own failure accounting (runner owns it).
  4. makedirs is lazy (inside _download_atomic); the sweep removes leftover empty dirs.
"""
import asyncio
import math
import os
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

import api_server
from file_io import init_db_sync, get_all_media_file_ids_sync


class _StopLoop(Exception):
    """Sentinel to break an otherwise-infinite background loop after one iteration."""


# --------------------------------------------------------------------------- #
# 1. Backoff cap >= sweep interval; a capped dead file is not re-queued next sweep
# --------------------------------------------------------------------------- #
def test_backoff_cap_is_at_least_sweep_interval():
    # The core correctness property of #51: with a cap below the sweep interval the backoff
    # always expires before the next sweep and download_new_files re-queues the dead file
    # every pass. The cap must therefore be >= cache_sweep_interval regardless of env.
    assert api_server._DOWNLOAD_BACKOFF_MAX >= float(api_server.Config["cache_sweep_interval"])
    # Default is 6h when MEDIA_BACKOFF_MAX_S is unset (well above the 900s default sweep).
    assert api_server._DOWNLOAD_BACKOFF_MAX >= 900.0


def test_capped_dead_file_backoff_exceeds_sweep_interval():
    key = ("i51chan", 1, "fid_dead")
    api_server._download_failures.pop(key, None)
    # Fail enough times for the raw exponential to reach the cap.
    n_fail = int(math.log2(api_server._DOWNLOAD_BACKOFF_MAX / api_server._DOWNLOAD_BACKOFF_BASE)) + 4
    for _ in range(n_fail):
        api_server._record_download_failure(key)
    remaining = api_server._download_backoff_remaining(key)
    # Once at the cap, the remaining backoff strictly exceeds one sweep interval, so the next
    # sweep skips this file rather than re-queueing it.
    assert remaining > float(api_server.Config["cache_sweep_interval"])
    api_server._clear_download_failure(key)


@pytest.mark.asyncio
async def test_capped_dead_file_not_requeued_on_sweep(tmp_path):
    cache_dir = str(tmp_path / "cache")
    channel, post_id, fid = "i51chan", 2, "fid_skip"
    key = (channel, post_id, fid)

    api_server._queued_media.clear()
    api_server._download_failures.pop(key, None)
    while not api_server.download_queue.empty():
        api_server.download_queue.get_nowait()

    # Drive the file's backoff to the cap.
    n_fail = int(math.log2(api_server._DOWNLOAD_BACKOFF_MAX / api_server._DOWNLOAD_BACKOFF_BASE)) + 4
    for _ in range(n_fail):
        api_server._record_download_failure(key)

    media_files = [{"channel": channel, "post_id": post_id, "file_unique_id": fid}]
    await api_server.download_new_files(media_files, cache_dir)

    # Backoff is far above the sweep interval -> the dead file is NOT enqueued.
    assert api_server.download_queue.qsize() == 0
    assert key not in api_server._queued_media

    api_server._clear_download_failure(key)
    api_server._queued_media.clear()


# --------------------------------------------------------------------------- #
# 2. Row dropped from SQLite after N consecutive failures (fire-and-forget)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_row_dropped_after_n_failures(tmp_path, monkeypatch):
    db = str(tmp_path / "drop.db")
    init_db_sync(db)
    monkeypatch.setattr(api_server, "DB_PATH", db)
    monkeypatch.setattr(api_server, "_DOWNLOAD_FAILURES_DROP_ROW", 3)

    channel, post_id, fid = "dropchan", 5, "dropfid"
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO media_file_ids (channel, post_id, file_unique_id, added) VALUES (?,?,?,?)",
        (channel, post_id, fid, datetime.now().timestamp()),
    )
    conn.commit()
    conn.close()

    key = (channel, post_id, fid)
    api_server._download_failures.pop(key, None)

    # Below the threshold: no drop yet.
    api_server._record_download_failure(key)
    api_server._record_download_failure(key)
    assert len(get_all_media_file_ids_sync(db)) == 1

    # The 3rd consecutive failure fires the fire-and-forget row drop.
    api_server._record_download_failure(key)
    # Await the scheduled drop task(s) to completion.
    pending = [t for t in api_server._drop_row_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending)

    assert get_all_media_file_ids_sync(db) == []
    # The in-memory failure counter is intentionally preserved (repeat drop stays cheap).
    assert api_server._download_failures[key][0] == 3
    api_server._clear_download_failure(key)


def test_drop_media_row_no_running_loop_is_noop():
    # _record_download_failure runs synchronously; when called with no running loop (a sync
    # unit test / off-loop context) the fire-and-forget drop must be a safe no-op, not crash.
    api_server._drop_media_row(("noloopchan", 1, "fid"))  # must not raise


# --------------------------------------------------------------------------- #
# 3. Dedup: the background worker downloads via _download_deduped and does not
#    keep its own failure accounting.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_worker_routes_through_deduped_with_bg_semaphore(monkeypatch):
    api_server._queued_media.clear()
    while not api_server.download_queue.empty():
        api_server.download_queue.get_nowait()
    api_server._queued_media.add(("wchan", 9, "wfid"))
    api_server.download_queue.put_nowait(("wchan", 9, "wfid"))

    seen = {}

    async def fake_deduped(channel, post_id, fid, semaphore):
        seen["args"] = (channel, post_id, fid)
        seen["sem"] = semaphore
        return ("/x", False)

    monkeypatch.setattr(api_server, "_download_deduped", fake_deduped)

    # The worker must NOT do its own accounting — the runner owns it.
    rec, clr = [], []
    monkeypatch.setattr(api_server, "_record_download_failure", lambda *a, **k: rec.append(a))
    monkeypatch.setattr(api_server, "_clear_download_failure", lambda *a, **k: clr.append(a))

    async def fake_sleep(_d):
        return None
    monkeypatch.setattr(api_server.asyncio, "sleep", fake_sleep)

    real_task_done = api_server.download_queue.task_done
    def stop_task_done():
        real_task_done()
        raise _StopLoop
    monkeypatch.setattr(api_server.download_queue, "task_done", stop_task_done)

    with pytest.raises(_StopLoop):
        await api_server.background_download_worker()

    assert seen["args"] == ("wchan", 9, "wfid")
    assert seen["sem"] is api_server.BACKGROUND_DOWNLOAD_SEMAPHORE  # background permit, per #49
    assert rec == [] and clr == []  # worker keeps NO own failure accounting
    api_server._queued_media.clear()


@pytest.mark.asyncio
async def test_worker_does_not_record_failure_on_error(monkeypatch):
    """A failing background download is negative-cached by the runner, so the worker must not
    _record_download_failure again (that would double-increment the counter)."""
    api_server._queued_media.clear()
    while not api_server.download_queue.empty():
        api_server.download_queue.get_nowait()
    api_server._queued_media.add(("wchan", 10, "wfid"))
    api_server.download_queue.put_nowait(("wchan", 10, "wfid"))

    async def boom(channel, post_id, fid, semaphore):
        raise RuntimeError("download blew up")
    monkeypatch.setattr(api_server, "_download_deduped", boom)

    rec = []
    monkeypatch.setattr(api_server, "_record_download_failure", lambda *a, **k: rec.append(a))

    async def fake_sleep(_d):
        return None
    monkeypatch.setattr(api_server.asyncio, "sleep", fake_sleep)

    real_task_done = api_server.download_queue.task_done
    def stop_task_done():
        real_task_done()
        raise _StopLoop
    monkeypatch.setattr(api_server.download_queue, "task_done", stop_task_done)

    with pytest.raises(_StopLoop):
        await api_server.background_download_worker()

    assert rec == []  # runner owns the accounting; the worker only logs
    assert ("wchan", 10, "wfid") not in api_server._queued_media  # dedup slot freed
    api_server._queued_media.clear()


@pytest.mark.asyncio
async def test_live_and_background_share_one_download(monkeypatch):
    """A live request and the background worker asking for the SAME file must result in exactly
    ONE actual download (shared _inflight future), not two parallel fetches."""
    key = ("dchan", 11, "dfid")
    api_server._inflight.pop(key, None)
    api_server._download_failures.pop(key, None)

    calls = []
    gate = asyncio.Event()

    async def fake_dl(channel, post_id, fid):
        calls.append(fid)
        await gate.wait()  # hold the download open so both callers overlap on the future
        return (f"/final/{fid}", False)

    monkeypatch.setattr(api_server, "download_media_file", fake_dl)

    # "live" request starts the download and creates the shared future.
    live = asyncio.create_task(
        api_server._download_deduped("dchan", 11, "dfid", api_server.HTTP_DOWNLOAD_SEMAPHORE)
    )
    await asyncio.sleep(0.02)  # let the live runner register the future and enter fake_dl
    # "background worker" asks for the same key while the live download is still in flight.
    bg = asyncio.create_task(
        api_server._download_deduped("dchan", 11, "dfid", api_server.BACKGROUND_DOWNLOAD_SEMAPHORE)
    )
    await asyncio.sleep(0.02)

    gate.set()  # release the single download
    r_live, r_bg = await asyncio.gather(live, bg)

    assert calls == ["dfid"]  # exactly ONE actual download despite two callers
    assert r_live == r_bg == ("/final/dfid", False)
    api_server._inflight.pop(key, None)


# --------------------------------------------------------------------------- #
# 4. Lazy makedirs + empty-dir sweep
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_download_new_files_creates_no_empty_dir(tmp_path):
    cache_dir = str(tmp_path / "cache")
    channel, post_id, fid = "nodir_chan", 3, "nodir_fid"

    api_server._queued_media.clear()
    api_server._download_failures.pop((channel, post_id, fid), None)
    while not api_server.download_queue.empty():
        api_server.download_queue.get_nowait()

    media_files = [{"channel": channel, "post_id": post_id, "file_unique_id": fid}]
    await api_server.download_new_files(media_files, cache_dir)

    # The file is queued, but NO cache directory was created for it (lazy makedirs).
    assert api_server.download_queue.qsize() == 1
    assert not os.path.exists(os.path.join(cache_dir, channel, str(post_id)))

    api_server._queued_media.clear()
    while not api_server.download_queue.empty():
        api_server.download_queue.get_nowait()


@pytest.mark.asyncio
async def test_download_atomic_creates_dir_lazily(tmp_path, monkeypatch):
    final_path = str(tmp_path / "cache" / "chan" / "5" / "fid")
    assert not os.path.exists(os.path.dirname(final_path))

    async def fake_dl(file_id, part_path, timeout=None, **kw):
        with open(part_path, "wb") as f:
            f.write(b"DATA")
        return part_path
    monkeypatch.setattr(api_server.client, "safe_download_media", fake_dl)

    out = await api_server._download_atomic("fileid", final_path, timeout=10.0)
    assert out == final_path
    with open(final_path, "rb") as f:
        assert f.read() == b"DATA"


def test_sweep_removes_empty_dirs_keeps_populated(tmp_path):
    cache_dir = str(tmp_path / "cache")
    # An empty post/channel dir (a dead file that never downloaded).
    empty_post = os.path.join(cache_dir, "deadchan", "1")
    os.makedirs(empty_post)
    # A populated post/channel dir (a live file) must be kept.
    live_post = os.path.join(cache_dir, "livechan", "2")
    os.makedirs(live_post)
    with open(os.path.join(live_post, "livefid"), "wb") as f:
        f.write(b"X")

    updated, _removed = api_server.remove_old_cached_files_sync([], cache_dir)

    # Empty post and its now-empty channel dir are gone; the populated tree is untouched.
    assert not os.path.exists(os.path.join(cache_dir, "deadchan"))
    assert os.path.exists(os.path.join(live_post, "livefid"))
    assert os.path.exists(cache_dir)  # the cache root itself is never removed


def test_sweep_survives_rmdir_race(tmp_path, monkeypatch):
    """A concurrent fresh mkdir between listdir and rmdir (rmdir-vs-mkdir race) surfaces as
    OSError; the sweep must swallow it and never crash."""
    cache_dir = str(tmp_path / "cache")
    os.makedirs(os.path.join(cache_dir, "racechan", "9"))

    def boom_rmdir(path):
        raise OSError("dir not empty (race)")
    monkeypatch.setattr(api_server.os, "rmdir", boom_rmdir)

    # Must not raise despite every rmdir failing.
    updated, _removed = api_server.remove_old_cached_files_sync([], cache_dir)
    assert updated == []
