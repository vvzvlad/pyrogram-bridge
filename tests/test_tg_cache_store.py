"""Tests for the tg_cache generic JSON store, prefix-limit, jitter and sweep (issue #23).

Задания 2 / 6 / 17 / 18, verification item 8, 11, 12.
"""
import json
import os
import time
from types import SimpleNamespace

import pytest

import tg_cache
from message_snapshot import SNAPSHOT_VERSION


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "tgcache"
    d.mkdir()
    monkeypatch.setattr(tg_cache, "CACHE_DIR", str(d))
    return d


# --------------------------------------------------------------------------- #
# _store_entry / _load_entry.
# --------------------------------------------------------------------------- #
def test_store_load_roundtrip(cache_dir):
    path = str(cache_dir / "x.history.json")
    tg_cache._store_entry(path, {"limit": 5, "messages": [{"id": 1}]})
    loaded = tg_cache._load_entry(path, max_age_hours=8)
    assert loaded is not None
    assert loaded["limit"] == 5
    assert loaded["messages"] == [{"id": 1}]
    assert loaded["version"] == SNAPSHOT_VERSION
    assert 0.8 <= loaded["jitter"] <= 1.0


def test_load_missing_file(cache_dir):
    assert tg_cache._load_entry(str(cache_dir / "nope.json"), 8) is None


def test_load_expired_ttl(cache_dir):
    path = str(cache_dir / "x.history.json")
    tg_cache._store_entry(path, {"data": 1})
    # max_age_hours=0 -> adjusted max age 0 -> any positive age is expired.
    assert tg_cache._load_entry(path, max_age_hours=0) is None
    # Still fresh with a real TTL.
    assert tg_cache._load_entry(path, max_age_hours=8) is not None


def test_load_version_mismatch(cache_dir):
    path = str(cache_dir / "x.history.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": SNAPSHOT_VERSION + 999, "timestamp": time.time(), "data": 1}, f)
    assert tg_cache._load_entry(path, 8) is None


def test_load_corrupt_json(cache_dir):
    path = str(cache_dir / "x.history.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    assert tg_cache._load_entry(path, 8) is None


def test_store_uses_unique_tmp_names(cache_dir, monkeypatch):
    path = str(cache_dir / "x.history.json")
    seen = []

    real_replace = os.replace

    def spy_replace(src, dst):
        seen.append(src)
        return real_replace(src, dst)

    monkeypatch.setattr(tg_cache.os, "replace", spy_replace)
    tg_cache._store_entry(path, {"data": 1})
    tg_cache._store_entry(path, {"data": 2})

    assert len(seen) == 2
    assert seen[0] != seen[1]  # each writer used a distinct tmp path
    for src in seen:
        assert src.startswith(path + ".tmp.")
    # No tmp leftovers.
    assert [n for n in os.listdir(cache_dir) if ".tmp." in n] == []


# --------------------------------------------------------------------------- #
# cleanup_legacy_cache_files / sweep_tgcache.
# --------------------------------------------------------------------------- #
def test_cleanup_legacy_keeps_new_format(cache_dir):
    (cache_dir / "chan.cache").write_text("x")
    (cache_dir / "chan_history.cache").write_text("x")
    (cache_dir / "chan.chatinfo").write_text("x")
    (cache_dir / "chan.history.json").write_text("x")
    (cache_dir / "chan.chatinfo.json").write_text("x")

    removed = tg_cache.cleanup_legacy_cache_files()
    assert removed == 3
    remaining = set(os.listdir(cache_dir))
    assert remaining == {"chan.history.json", "chan.chatinfo.json"}


def test_sweep_removes_only_old(cache_dir):
    old = cache_dir / "old.history.json"
    new = cache_dir / "new.history.json"
    old.write_text("x")
    new.write_text("x")
    old_time = time.time() - 8 * 86400  # 8 days old, threshold is 7
    os.utime(old, (old_time, old_time))

    removed = tg_cache.sweep_tgcache(max_age_days=7)
    assert removed == 1
    remaining = set(os.listdir(cache_dir))
    assert remaining == {"new.history.json"}


def test_sweep_removes_orphan_tmp(cache_dir):
    orphan = cache_dir / "x.history.json.tmp.deadbeef"
    orphan.write_text("x")
    old_time = time.time() - 10 * 86400
    os.utime(orphan, (old_time, old_time))
    assert tg_cache.sweep_tgcache(max_age_days=7) == 1


# --------------------------------------------------------------------------- #
# History prefix-limit (Задание 6).
# --------------------------------------------------------------------------- #
def _fake_messages(n):
    return [SimpleNamespace(id=i) for i in range(n)]


def test_prefix_larger_cache_serves_smaller_request(cache_dir):
    tg_cache._save_history_to_cache("chan", _fake_messages(100), limit=100)
    served = tg_cache._get_history_from_cache("chan", limit=50)
    assert served is not None
    assert len(served) == 50
    assert [m.id for m in served] == list(range(50))  # order preserved (newest-first slice)


def test_prefix_smaller_cache_is_miss(cache_dir):
    tg_cache._save_history_to_cache("chan", _fake_messages(50), limit=50)
    assert tg_cache._get_history_from_cache("chan", limit=100) is None


def test_prefix_exhausted_channel_serves_larger_request(cache_dir):
    # Fetched with limit 100 but the channel only has 37 messages -> entire history cached.
    tg_cache._save_history_to_cache("chan", _fake_messages(37), limit=100)
    served = tg_cache._get_history_from_cache("chan", limit=200)
    assert served is not None
    assert len(served) == 37


def test_save_stores_fetch_limit_not_len(cache_dir):
    tg_cache._save_history_to_cache("chan", _fake_messages(37), limit=100)
    path = tg_cache._cache_file_path("chan", "history.json")
    payload = tg_cache._load_entry(path, max_age_hours=8)
    assert payload["limit"] == 100
    assert len(payload["messages"]) == 37


def test_history_ttl_independent_of_prefix(cache_dir):
    tg_cache._save_history_to_cache("chan", _fake_messages(100), limit=100)
    # Expired regardless of prefix match.
    assert tg_cache._get_history_from_cache("chan", limit=50, max_age_hours=0) is None


# --------------------------------------------------------------------------- #
# Jitter stability (Задание 17 / item 12).
# --------------------------------------------------------------------------- #
def test_jitter_read_is_stable(cache_dir):
    path = str(cache_dir / "x.history.json")
    tg_cache._store_entry(path, {"data": 1})
    # Repeated reads near a TTL boundary must give a stable result: the jitter is fixed at
    # write time and _load_entry never calls random().
    results = [tg_cache._load_entry(path, max_age_hours=8) is not None for _ in range(10)]
    assert all(results)


def test_load_does_not_call_random(cache_dir, monkeypatch):
    path = str(cache_dir / "x.history.json")
    tg_cache._store_entry(path, {"data": 1})

    def boom(*a, **k):
        raise AssertionError("random.uniform must not be called at read time")

    monkeypatch.setattr(tg_cache.random, "uniform", boom)
    assert tg_cache._load_entry(path, max_age_hours=8) is not None


# --------------------------------------------------------------------------- #
# chatinfo store round trip.
# --------------------------------------------------------------------------- #
def test_chatinfo_roundtrip(cache_dir):
    tg_cache._save_chat_to_cache("chan", {"id": -100, "title": "T", "username": "u"})
    data = tg_cache._get_chat_from_cache("chan")
    assert data == {"id": -100, "title": "T", "username": "u"}


def test_no_pickle_import():
    import pathlib
    src = pathlib.Path(tg_cache.__file__).read_text()
    assert "pickle" not in src
