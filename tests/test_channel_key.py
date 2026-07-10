# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""Tests for the canonical channel key: helper, the three wiring layers, and the
one-shot migration (issue #24, tasks 8-11)."""

import os
import sqlite3
from types import SimpleNamespace

import pytest

import tg_cache
from channel_key import canonical_channel_key
from migrate_channel_keys import migrate_channel_keys_sync
from post_parser import PostParser


# --------------------------------------------------------------------------- #
# Task 11 — unit: canonical_channel_key.
# --------------------------------------------------------------------------- #
def test_canonical_lowercases_username():
    assert canonical_channel_key('Durov') == 'durov'


def test_canonical_strips_at_prefix():
    assert canonical_channel_key('@durov') == 'durov'
    assert canonical_channel_key('@Durov') == 'durov'


def test_canonical_numeric_id_str_preserved():
    assert canonical_channel_key('-1001234567890') == '-1001234567890'


def test_canonical_numeric_id_int_preserved():
    assert canonical_channel_key(-1001234567890) == '-1001234567890'


def test_canonical_already_lowercase_idempotent():
    assert canonical_channel_key('durov') == 'durov'
    assert canonical_channel_key(canonical_channel_key('Durov')) == 'durov'


# --------------------------------------------------------------------------- #
# Task 11 — tg_cache path is identical for the two casings.
# --------------------------------------------------------------------------- #
def test_tgcache_path_identical_for_casings(tmp_path, monkeypatch):
    monkeypatch.setattr(tg_cache, "CACHE_DIR", str(tmp_path))
    p_upper = tg_cache._cache_file_path('Durov', 'history.json')
    p_lower = tg_cache._cache_file_path('durov', 'history.json')
    p_at = tg_cache._cache_file_path('@DUROV', 'history.json')
    assert p_upper == p_lower == p_at
    assert os.path.basename(p_upper) == 'durov.history.json'


def test_tgcache_path_numeric_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(tg_cache, "CACHE_DIR", str(tmp_path))
    p = tg_cache._cache_file_path('-1001234567890', 'chatinfo.json')
    assert os.path.basename(p) == '-1001234567890.chatinfo.json'


# --------------------------------------------------------------------------- #
# Task 11 — get_channel_username lowercases both branches.
# --------------------------------------------------------------------------- #
def _parser():
    return PostParser(SimpleNamespace())


def test_get_channel_username_single_lowercased():
    parser = _parser()
    msg = SimpleNamespace(chat=SimpleNamespace(username='MixedCase', id=1))
    assert parser.get_channel_username(msg) == 'mixedcase'


def test_get_channel_username_usernames_list_lowercased():
    parser = _parser()
    chat = SimpleNamespace(
        usernames=[SimpleNamespace(username='MixedCase', active=True)],
        username=None,
        id=1,
    )
    assert parser.get_channel_username(SimpleNamespace(chat=chat)) == 'mixedcase'


def test_get_channel_username_numeric_id_unchanged():
    parser = _parser()
    chat = SimpleNamespace(usernames=None, username=None, id=-1001234567890)
    assert parser.get_channel_username(SimpleNamespace(chat=chat)) == '-1001234567890'


# --------------------------------------------------------------------------- #
# Migration helpers.
# --------------------------------------------------------------------------- #
def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE media_file_ids (
               channel        TEXT    NOT NULL,
               post_id        INTEGER NOT NULL,
               file_unique_id TEXT    NOT NULL,
               added          REAL    NOT NULL,
               mime_type      TEXT,
               PRIMARY KEY (channel, post_id, file_unique_id)
           )"""
    )
    conn.commit()
    conn.close()


def _rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT channel, post_id, file_unique_id, added, mime_type FROM media_file_ids "
        "ORDER BY channel, post_id, file_unique_id")]
    conn.close()
    return rows


# --------------------------------------------------------------------------- #
# Task 11 (a) — SQL merge of both forms → one row, max(added), non-NULL mime_type.
# --------------------------------------------------------------------------- #
def test_migration_sql_merge(tmp_path):
    db = str(tmp_path / "m.db")
    cache = tmp_path / "cache"
    cache.mkdir()
    _make_db(db)
    conn = sqlite3.connect(db)
    # Old-cased row (higher added, NULL mime) and lowercase twin (lower added, has mime).
    conn.execute("INSERT INTO media_file_ids VALUES (?,?,?,?,?)", ('Durov', 5, 'fid', 200.0, None))
    conn.execute("INSERT INTO media_file_ids VALUES (?,?,?,?,?)", ('durov', 5, 'fid', 100.0, 'image/jpeg'))
    conn.commit()
    conn.close()

    migrate_channel_keys_sync(db, str(cache))

    rows = _rows(db)
    assert len(rows) == 1
    r = rows[0]
    assert r['channel'] == 'durov'
    assert r['added'] == 200.0            # max(added)
    assert r['mime_type'] == 'image/jpeg'  # non-NULL preferred


def test_migration_sql_rekey_no_twin(tmp_path):
    db = str(tmp_path / "m.db")
    cache = tmp_path / "cache"
    cache.mkdir()
    _make_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO media_file_ids VALUES (?,?,?,?,?)", ('Durov', 7, 'fid', 50.0, 'video/mp4'))
    conn.commit()
    conn.close()

    migrate_channel_keys_sync(db, str(cache))

    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]['channel'] == 'durov'
    assert rows[0]['mime_type'] == 'video/mp4'


# --------------------------------------------------------------------------- #
# Task 11 (b) — samefile guard → no-op, data intact (case-insensitive FS simulated).
# --------------------------------------------------------------------------- #
def test_migration_samefile_guard_no_data_loss(tmp_path, monkeypatch):
    db = str(tmp_path / "m.db")
    cache = tmp_path / "cache"
    cache.mkdir()
    _make_db(db)
    # Single dir 'Durov' with a file; on a case-insensitive FS 'durov' would be the same dir.
    src = cache / "Durov" / "5"
    src.mkdir(parents=True)
    (src / "fid").write_bytes(b"payload")

    # Simulate a case-insensitive FS: any existence check on the lowercase twin resolves,
    # and samefile reports src == dst so the FS step must be a pure no-op.
    real_exists = os.path.exists

    def fake_exists(p):
        if os.path.basename(p) == 'durov':
            return True
        return real_exists(p)

    monkeypatch.setattr(os.path, "exists", fake_exists)
    monkeypatch.setattr(os.path, "samefile", lambda a, b: True)

    migrate_channel_keys_sync(db, str(cache))

    # Data intact: the original file must still be present and unchanged.
    assert (cache / "Durov" / "5" / "fid").read_bytes() == b"payload"


# --------------------------------------------------------------------------- #
# Task 11 (c) — merge of two genuinely different dirs → combined, target wins.
# --------------------------------------------------------------------------- #
def test_migration_fs_merge_different_dirs(tmp_path):
    db = str(tmp_path / "m.db")
    cache = tmp_path / "cache"
    cache.mkdir()
    _make_db(db)

    # Old-cased tree.
    (cache / "Durov" / "5").mkdir(parents=True)
    (cache / "Durov" / "5" / "shared").write_bytes(b"OLD")   # collides -> dst wins
    (cache / "Durov" / "5" / "only_old").write_bytes(b"OLD_ONLY")
    # Canonical tree.
    (cache / "durov" / "5").mkdir(parents=True)
    (cache / "durov" / "5" / "shared").write_bytes(b"NEW")   # existing dst wins
    (cache / "durov" / "6").mkdir(parents=True)
    (cache / "durov" / "6" / "only_new").write_bytes(b"NEW_ONLY")

    migrate_channel_keys_sync(db, str(cache))

    # Old dir gone, everything merged under 'durov'.
    assert not (cache / "Durov").exists()
    assert (cache / "durov" / "5" / "shared").read_bytes() == b"NEW"        # target wins
    assert (cache / "durov" / "5" / "only_old").read_bytes() == b"OLD_ONLY"  # brought over
    assert (cache / "durov" / "6" / "only_new").read_bytes() == b"NEW_ONLY"  # untouched


def test_migration_fs_rename_when_dst_missing(tmp_path):
    db = str(tmp_path / "m.db")
    cache = tmp_path / "cache"
    cache.mkdir()
    _make_db(db)
    (cache / "Durov" / "5").mkdir(parents=True)
    (cache / "Durov" / "5" / "fid").write_bytes(b"data")

    migrate_channel_keys_sync(db, str(cache))

    assert not (cache / "Durov").exists()
    assert (cache / "durov" / "5" / "fid").read_bytes() == b"data"


# --------------------------------------------------------------------------- #
# Task 11 (d) — re-run is a no-op.
# --------------------------------------------------------------------------- #
def test_migration_rerun_noop(tmp_path):
    db = str(tmp_path / "m.db")
    cache = tmp_path / "cache"
    cache.mkdir()
    _make_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO media_file_ids VALUES (?,?,?,?,?)", ('Durov', 5, 'fid', 200.0, None))
    conn.execute("INSERT INTO media_file_ids VALUES (?,?,?,?,?)", ('durov', 5, 'fid', 100.0, 'image/jpeg'))
    conn.commit()
    conn.close()
    (cache / "Durov" / "5").mkdir(parents=True)
    (cache / "Durov" / "5" / "fid").write_bytes(b"data")

    migrate_channel_keys_sync(db, str(cache))
    first = _rows(db)
    # Second run: nothing left to migrate.
    migrate_channel_keys_sync(db, str(cache))
    second = _rows(db)

    assert first == second
    assert all(r['channel'] == r['channel'].lower() for r in second)
    assert (cache / "durov" / "5" / "fid").read_bytes() == b"data"
