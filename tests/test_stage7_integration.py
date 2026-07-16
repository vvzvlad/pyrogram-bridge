# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Stage 7 — cross-stage END-TO-END integration tests.

Unlike the per-stage suites (which pin one seam in isolation), these drive the real
public entry points (`get_media`, `ping`, the access-time flush) so a regression that
only shows up when the stages interact goes red. Every scenario here maps to one of the
plan's "Стадия 7 — сквозные ручные сценарии"; the ones that genuinely need a running
server + real downloads + lsof (fd-leak counting) are NOT faked here — they stay in
docs/stability-verification.md for the operator's prod observation.

Automated here:
- Range semantics at the /media ROUTE level (get_media -> prepare_file_response ->
  FileResponse driven through real ASGI): bytes=0-99 -> 206, bytes=-100 -> 206,
  bytes=999999999- -> 416. (Stage 3 pins these on prepare_file_response directly; this
  adds the end-to-end assertion that get_media's cache-hit branch reaches FileResponse
  with a live Range still honored — i.e. stages 2+3 wired together.)
- /ping (stage 6) stays fast + correct while a deliberately-slow op is in flight, issuing
  ZERO Telegram RPC — proving the healthcheck is decoupled from the hot/blocked paths.
- In-flight dedup + disconnect cleanup (stages 1/2) through the REAL get_media entry
  point: concurrent requests share one download and the _inflight registry drains; a
  cancelled request (client disconnect) leaves neither a stuck key nor a hung sibling.
- str(channel) access-time key consistency (stage 5) end-to-end: a /media cache hit for
  an int-ish channel records the str-keyed timestamp, and the flush UPDATE matches the
  str-keyed DB row (hit -> flush -> DB), the exact affinity gotcha the plan warns about.
"""
import time
import sqlite3
import asyncio
from types import SimpleNamespace

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api_server
from pyrogram import errors
from file_io import init_db_sync


# 2048 deterministic bytes so Range slices are byte-checkable.
BODY = bytes(range(256)) * 8
SIZE = len(BODY)


def _media_app():
    """A bare app that mounts the REAL get_media (and ping) with NO lifespan, so
    client.start() never runs — a pure cache hit never touches Telegram, and FileResponse
    computes Range/206/416 at send time, which only happens when driven through ASGI."""
    app = FastAPI()
    app.add_api_route("/media/{channel}/{post_id}/{file_unique_id}/{digest}", api_server.get_media, methods=["GET"])
    app.add_api_route("/media/{channel}/{post_id}/{file_unique_id}", api_server.get_media, methods=["GET"])
    return app


def _seed_cache(tmp_path, channel, post_id, fid, body=BODY):
    cache_dir = tmp_path / "data" / "cache" / str(channel) / str(post_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / fid).write_bytes(body)
    return cache_dir / fid


# --------------------------------------------------------------------------- #
# Range semantics at the /media ROUTE level (stages 2 + 3 wired together).
# Plan scenario: `curl -H "Range: bytes=0-99" / "bytes=-100" / "bytes=999999999-"`.
# Regression caught: any change that makes get_media's cache-hit branch stop reaching
# FileResponse (e.g. re-buffering the body, hand-rolling headers, dropping the media_key
# path) or that breaks the digest gate wiring — the Range would stop being honored.
# --------------------------------------------------------------------------- #
def test_media_route_range_prefix_0_99(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "data" / "cache"))
    _seed_cache(tmp_path, "chan", 3, "fidR")
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)
    # Keep the MIME path DB-free; the FileResponse/Range machinery is what we exercise.
    monkeypatch.setattr(api_server, "get_mime_type_sync", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "set_mime_type_sync", lambda *a, **k: None)

    c = TestClient(_media_app())
    r = c.get("/media/chan/3/fidR/anydigest", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-99/{SIZE}"
    assert r.headers["content-length"] == "100"
    assert r.content == BODY[:100]
    assert r.headers["accept-ranges"] == "bytes"


def test_media_route_range_suffix_last_100(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "data" / "cache"))
    _seed_cache(tmp_path, "chan", 3, "fidS")
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)
    monkeypatch.setattr(api_server, "get_mime_type_sync", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "set_mime_type_sync", lambda *a, **k: None)

    c = TestClient(_media_app())
    r = c.get("/media/chan/3/fidS/anydigest", headers={"Range": "bytes=-100"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes {SIZE - 100}-{SIZE - 1}/{SIZE}"
    assert r.headers["content-length"] == "100"
    assert r.content == BODY[-100:]


def test_media_route_range_unsatisfiable_416(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "data" / "cache"))
    _seed_cache(tmp_path, "chan", 3, "fidU")
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)
    monkeypatch.setattr(api_server, "get_mime_type_sync", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "set_mime_type_sync", lambda *a, **k: None)

    c = TestClient(_media_app())
    r = c.get("/media/chan/3/fidU/anydigest", headers={"Range": "bytes=999999999-"})
    assert r.status_code == 416
    # Starlette's 416 Content-Range is `*/size` (documented stage-3 RFC-7233 delta).
    assert r.headers["content-range"] == f"*/{SIZE}"


# --------------------------------------------------------------------------- #
# /ping (stage 6) stays fast + correct while a slow op is in flight, zero TG RPC.
# Plan scenario: "Генерация фида на 100+ сообщений + параллельный /ping -> ping < 100 мс".
# We model the concurrent slow op as a coroutine parked on an Event that is NEVER set
# during the ping, and assert ping resolves while it is still pending AND touches no RPC.
# Regression caught: re-coupling /ping to a TG RPC (get_me / safe_get_messages) or to any
# awaitable that a blocked hot path could stall — the ping would no longer return promptly
# or the spy counts would go non-zero.
# --------------------------------------------------------------------------- #
class _FakeKurigram:
    def __init__(self, is_connected=True):
        self.is_connected = is_connected
        self.get_me_calls = 0

    async def get_me(self):
        self.get_me_calls += 1
        raise AssertionError("/ping must never call get_me()")


class _FakeTelegramClient:
    def __init__(self, age, is_connected=True):
        self._age = age
        self.client = _FakeKurigram(is_connected=is_connected)
        self.safe_get_messages_calls = 0

    def watchdog_last_ok_age(self):
        return self._age

    async def safe_get_messages(self, *a, **k):
        self.safe_get_messages_calls += 1
        raise AssertionError("/ping must never call safe_get_messages()")


async def test_ping_prompt_and_rpc_free_while_slow_op_pending(monkeypatch):
    threshold = api_server.Config["tg_ping_unhealthy_after"]
    fake = _FakeTelegramClient(age=threshold - 10, is_connected=True)
    monkeypatch.setattr(api_server, "client", fake)

    # A concurrent slow operation (a stand-in for a hung feed/RPC hot path) parked on an
    # Event we deliberately never set for the duration of the ping.
    gate = asyncio.Event()

    async def slow_op():
        await gate.wait()

    slow = asyncio.create_task(slow_op())
    await asyncio.sleep(0)  # let slow_op start and park on the gate

    # The real proof of decoupling: ping() returns under a tight deadline while the slow op
    # is parked, AND issues zero TG RPC. wait_for reds if ping ever blocks; the spies (which
    # raise if touched) red if ping recouples to any RPC. `not slow.done()` is only a sanity
    # check that ping did not somehow drive the parked op — the gate keeps it pending anyway.
    resp = await asyncio.wait_for(api_server.ping(), timeout=1.0)

    assert resp.status_code == 200            # correct health while connected + fresh
    assert not slow.done()                    # sanity: slow op still parked, ping did not await it
    assert fake.client.get_me_calls == 0      # decoupled: zero TG RPC
    assert fake.safe_get_messages_calls == 0

    gate.set()
    await slow


async def test_ping_reports_degraded_promptly_while_slow_op_pending(monkeypatch):
    threshold = api_server.Config["tg_ping_unhealthy_after"]
    fake = _FakeTelegramClient(age=threshold + 100, is_connected=True)  # stale probe
    monkeypatch.setattr(api_server, "client", fake)

    gate = asyncio.Event()

    async def slow_op():
        await gate.wait()

    slow = asyncio.create_task(slow_op())
    await asyncio.sleep(0)

    resp = await asyncio.wait_for(api_server.ping(), timeout=1.0)

    assert resp.status_code == 503            # stale watchdog probe -> degraded, still instant
    assert not slow.done()                    # sanity: slow op still parked, ping did not await it
    assert fake.client.get_me_calls == 0
    assert fake.safe_get_messages_calls == 0

    gate.set()
    await slow


# --------------------------------------------------------------------------- #
# In-flight dedup + disconnect cleanup (stages 1/2) through the REAL get_media entry.
# Plan scenario: "Параллельные запросы одного большого видео -> нет частичной отдачи" and
# "Отключение клиента на середине стрима -> нет утечки тасков/фд".
# The pure-unit stage-2 tests pin _download_deduped directly; these drive get_media so the
# HTTP semaphore + dedup registry + serve path are proven wired together.
# Regression caught: moving the download back into the request coroutine (so a disconnect
# cancels it), or dropping the finally-pop, would leave a stuck _inflight key / hung sibling.
# --------------------------------------------------------------------------- #
async def test_get_media_concurrent_shares_one_download_and_drains_registry(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "data" / "cache"))
    api_server._inflight.clear()
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)
    # Serve step is not under test here; keep it to a sentinel so we assert on dedup + registry.
    sentinel = object()

    async def fake_prepare(file_path, request=None, delete_after=False, media_key=None):
        return sentinel

    monkeypatch.setattr(api_server, "prepare_file_response", fake_prepare)

    calls = []

    async def slow_dl(channel, post_id, fid):
        calls.append(fid)
        await asyncio.sleep(0.05)  # real overlap window for the two requests
        return (f"/final/{fid}", False)

    monkeypatch.setattr(api_server, "download_media_file", slow_dl)

    req = SimpleNamespace(headers={})
    t1 = asyncio.create_task(api_server.get_media("chan", 9, "vfid", request=req, digest="x"))
    t2 = asyncio.create_task(api_server.get_media("chan", 9, "vfid", request=req, digest="x"))
    r1, r2 = await asyncio.gather(t1, t2)

    assert r1 is sentinel and r2 is sentinel
    assert calls == ["vfid"]            # exactly ONE real download, shared by both requests
    assert not api_server._inflight     # registry drained — no forever-busy key


async def test_get_media_request_cancel_does_not_stick_registry_or_hang_sibling(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "data" / "cache"))
    api_server._inflight.clear()
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)
    sentinel = object()

    async def fake_prepare(file_path, request=None, delete_after=False, media_key=None):
        return sentinel

    monkeypatch.setattr(api_server, "prepare_file_response", fake_prepare)

    gate = asyncio.Event()
    calls = []

    async def held_dl(channel, post_id, fid):
        calls.append(fid)
        await gate.wait()  # hold the shared download open until we release it
        return (f"/final/{fid}", False)

    monkeypatch.setattr(api_server, "download_media_file", held_dl)

    req = SimpleNamespace(headers={})
    t1 = asyncio.create_task(api_server.get_media("chan", 9, "cfid", request=req, digest="x"))
    await asyncio.sleep(0.02)  # t1 registers the future + starts the detached download
    t2 = asyncio.create_task(api_server.get_media("chan", 9, "cfid", request=req, digest="x"))
    await asyncio.sleep(0.02)

    # First client disconnects: its request coroutine is cancelled mid-wait.
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1

    # The detached download is unaffected; releasing it resolves the surviving request.
    gate.set()
    r2 = await asyncio.wait_for(t2, timeout=2.0)
    assert r2 is sentinel
    assert calls == ["cfid"]           # download ran exactly once (not restarted)
    assert not api_server._inflight     # registry drained despite the disconnect


# --------------------------------------------------------------------------- #
# temp_<fid> pre-download fast-path (issue #49): a cached LARGE video lives on disk as
# temp_<fid> (not <fid>). get_media must serve it directly — WITHOUT acquiring a download
# permit and WITHOUT the safe_get_messages RPC that download_media_file would otherwise do
# before its own temp-cache check. Links to >100MB videos legitimately appear in feeds.
# Regression caught: dropping the temp_ fast-path re-couples every large-video re-serve to a
# Telegram RPC + a scarce download permit.
# --------------------------------------------------------------------------- #
async def test_get_media_temp_cached_large_video_served_without_rpc_or_permit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "data" / "cache"))
    api_server._inflight.clear()
    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)
    # Keep the MIME path DB-free; the fast-path (no RPC/permit) is what we exercise.
    monkeypatch.setattr(api_server, "get_mime_type_sync", lambda *a, **k: None)
    monkeypatch.setattr(api_server, "set_mime_type_sync", lambda *a, **k: None)

    channel, post_id, fid = "chan", 11, "bigvid"
    # Seed ONLY the temp_<fid> file; the plain <fid> is absent (as for a large video).
    _seed_cache(tmp_path, channel, post_id, f"temp_{fid}")

    # Any Telegram RPC or a live download during a temp cache hit is the bug.
    async def no_rpc(*a, **k):
        raise AssertionError("temp cache hit must not call safe_get_messages")

    async def no_dl(*a, **k):
        raise AssertionError("temp cache hit must not start a download")

    monkeypatch.setattr(api_server.client, "safe_get_messages", no_rpc)
    monkeypatch.setattr(api_server, "download_media_file", no_dl)

    permits_before = api_server.HTTP_DOWNLOAD_SEMAPHORE._value

    app = _media_app()
    with TestClient(app) as client:
        resp = client.get(f"/media/{channel}/{post_id}/{fid}/x")

    assert resp.status_code == 200
    assert resp.content == BODY                                    # the temp file body
    assert api_server.HTTP_DOWNLOAD_SEMAPHORE._value == permits_before  # no permit taken
    assert not api_server._inflight                                # no download registered
    # Not tracked in SQLite: temp large videos record no _access_updates entry.
    fs = api_server.canonical_channel_key(channel)
    assert (fs, post_id, fid) not in api_server._access_updates


# --------------------------------------------------------------------------- #
# str(channel) access-time key consistency (stage 5) END-TO-END: hit -> flush -> DB.
# Plan gotcha #9: "Ключи SQLite: channel всегда str(...)"; if the key the hot path RECORDS
# and the key the flush UPDATEs by ever diverge, the timestamp silently stops updating and
# the file eventually falls out of the cache. Stage 5 pins hit and flush separately; this
# stitches them: the /media cache-hit records a (str-channel) key, and the flush's bulk
# UPDATE must land on that exact DB row. (get_media's route channel is already a str, so the
# str() there is a defensive no-op; the live int/str hazard is in download_media_file, pinned
# by stage-5's test_cache_hit_keys_channel_as_str — this test covers the get_media+flush leg.)
# Regression caught: any accumulator-key vs UPDATE-WHERE inconsistency (e.g. a transposed
# key-column order in the bulk SQL, verified to turn this test red) leaves `added` stale.
# --------------------------------------------------------------------------- #
async def test_media_cache_hit_flush_updates_str_keyed_db_row(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "data" / "cache"))
    api_server._access_updates = {}

    channel, post_id, fid = "12345", 7, "fidINT"  # int-ish channel, passed as the route str
    _seed_cache(tmp_path, channel, post_id, fid)

    db = str(tmp_path / "access.db")
    init_db_sync(db)
    # Seed the row keyed by the STRING channel with an old access time.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO media_file_ids (channel, post_id, file_unique_id, added) VALUES (?,?,?,?)",
        (channel, post_id, fid, 1.0),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(api_server, "DB_PATH", db)

    monkeypatch.setattr(api_server, "verify_media_digest", lambda url, digest: True)
    sentinel = object()

    async def fake_prepare(file_path, request=None, media_key=None):
        return sentinel

    monkeypatch.setattr(api_server, "prepare_file_response", fake_prepare)

    before = time.time()
    resp = await api_server.get_media(channel, post_id, fid, request=SimpleNamespace(headers={}), digest="x")
    assert resp is sentinel

    # Hot path recorded the str-keyed access time (never the raw int form).
    assert (channel, post_id, fid) in api_server._access_updates

    # Flush the accumulator; the bulk UPDATE must match the str-keyed row and refresh `added`.
    await api_server._flush_access_updates()
    assert api_server._access_updates == {}

    conn = sqlite3.connect(db)
    added = conn.execute(
        "SELECT added FROM media_file_ids WHERE channel = ? AND post_id = ? AND file_unique_id = ?",
        (channel, post_id, fid),
    ).fetchone()[0]
    conn.close()
    assert added >= before  # refreshed from the stale 1.0 to ~now via the str-keyed WHERE
