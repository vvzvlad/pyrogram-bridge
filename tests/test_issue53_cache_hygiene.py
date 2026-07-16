# flake8: noqa
# pylint: disable=missing-function-docstring, missing-module-docstring, protected-access
"""
Issue #53 (cache-hygiene chore) regression tests.

Covers:
- Part 1: the guarded DB delete `remove_media_file_ids_if_unchanged_sync`. A row whose
  `added` was bumped (re-upserted by a live render) BETWEEN the sweeper's snapshot and its
  delete must SURVIVE; a row still at its snapshot `added` (<=) must be deleted.
- Part 2: the `_stat_size_or_none` helper — present file returns its size, a missing file
  returns None (no exception), which also covers the "disappeared mid-check" TOCTOU path.
"""
import os
import sqlite3

import api_server
from file_io import (
    init_db_sync,
    upsert_media_file_id_sync,
    get_all_media_file_ids_sync,
    remove_media_file_ids_if_unchanged_sync,
)


# --------------------------------------------------------------------------- #
# Part 1 — guarded delete: a re-upserted (newer `added`) row survives the sweep.
# --------------------------------------------------------------------------- #
def test_guarded_delete_keeps_reupserted_row_and_removes_stale(tmp_path):
    db = str(tmp_path / "guarded.db")
    init_db_sync(db)

    # Snapshot state: two rows, both inserted at t=100 (what the sweeper would observe).
    upsert_media_file_id_sync(db, "chan", 1, "stale_fid", 100.0)
    upsert_media_file_id_sync(db, "chan", 2, "fresh_fid", 100.0)

    # DURING the (long) disk walk, a live render re-upserts row 2 -> its `added` grows.
    upsert_media_file_id_sync(db, "chan", 2, "fresh_fid", 250.0)

    # The sweeper decides to remove BOTH rows, but passes the SNAPSHOTTED added (100.0).
    removed_entries = [
        ("chan", 1, "stale_fid", 100.0),
        ("chan", 2, "fresh_fid", 100.0),
    ]
    remove_media_file_ids_if_unchanged_sync(db, removed_entries)

    remaining = {(r["channel"], r["post_id"], r["file_unique_id"]): r["added"]
                 for r in get_all_media_file_ids_sync(db)}

    # The stale row (added unchanged, <= snapshot) is deleted.
    assert ("chan", 1, "stale_fid") not in remaining
    # The re-upserted row (added grew above the snapshot) SURVIVES with its fresh value.
    assert remaining.get(("chan", 2, "fresh_fid")) == 250.0


def test_guarded_delete_boundary_equal_added_is_removed(tmp_path):
    # `added == snapshot` must still delete (the guard is `added <= ?`): a row untouched
    # since the snapshot is the exact case the sweep is meant to purge.
    db = str(tmp_path / "boundary.db")
    init_db_sync(db)
    upsert_media_file_id_sync(db, "chan", 3, "eq_fid", 500.0)

    remove_media_file_ids_if_unchanged_sync(db, [("chan", 3, "eq_fid", 500.0)])

    assert get_all_media_file_ids_sync(db) == []


# --------------------------------------------------------------------------- #
# Part 2 — _stat_size_or_none smoke test.
# --------------------------------------------------------------------------- #
def test_stat_size_or_none_returns_size_for_existing_file(tmp_path):
    fp = tmp_path / "present.bin"
    fp.write_bytes(b"abcdef")  # 6 bytes
    assert api_server._stat_size_or_none(str(fp)) == 6


def test_stat_size_or_none_returns_none_for_missing_file(tmp_path):
    missing = tmp_path / "does_not_exist.bin"
    # Missing file (also the "disappeared mid-check" TOCTOU case) -> None, never an exception.
    assert api_server._stat_size_or_none(str(missing)) is None


def test_stat_size_or_none_zero_size_file(tmp_path):
    fp = tmp_path / "empty.bin"
    fp.write_bytes(b"")
    # A present-but-zero-size file returns 0 (distinct from None), so callers can tell an
    # empty cached file apart from a missing one.
    assert api_server._stat_size_or_none(str(fp)) == 0
