#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=missing-function-docstring

import os
import sqlite3
from contextlib import contextmanager
from typing import List

# Path to SQLite database file
DB_PATH = os.path.join(os.path.abspath("./data"), "media_file_ids.db")


def _open_db(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL journal mode enabled."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _db_connection(db_path: str):
    """Context manager that opens a WAL-mode SQLite connection and ensures it is closed."""
    conn = _open_db(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db_sync(db_path: str) -> None:
    """Create the media_file_ids table if it does not exist."""
    with _db_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_file_ids (
                channel        TEXT    NOT NULL,
                post_id        INTEGER NOT NULL,
                file_unique_id TEXT    NOT NULL,
                added          REAL    NOT NULL,
                PRIMARY KEY (channel, post_id, file_unique_id)
            )
            """
        )


def upsert_media_file_id_sync(db_path: str, channel: str, post_id: int, file_unique_id: str, added: float) -> None:
    """Insert or replace a single media file ID record."""
    with _db_connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO media_file_ids (channel, post_id, file_unique_id, added) VALUES (?, ?, ?, ?)",
            (channel, post_id, file_unique_id, added),
        )


def update_media_file_access_sync(db_path: str, channel: str, post_id: int, file_unique_id: str, added: float) -> None:
    """Update the access timestamp for an existing media file ID record."""
    with _db_connection(db_path) as conn:
        conn.execute(
            "UPDATE media_file_ids SET added = ? WHERE channel = ? AND post_id = ? AND file_unique_id = ?",
            (added, channel, post_id, file_unique_id),
        )


def get_all_media_file_ids_sync(db_path: str) -> List[dict]:
    """Return all rows from media_file_ids as a list of dicts."""
    with _db_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT channel, post_id, file_unique_id, added FROM media_file_ids")
        rows = cursor.fetchall()
    return [dict(row) for row in rows]


def remove_media_file_ids_sync(db_path: str, entries: List[tuple]) -> None:
    """Remove media file ID records identified by (channel, post_id, file_unique_id) tuples."""
    with _db_connection(db_path) as conn:
        conn.executemany(
            "DELETE FROM media_file_ids WHERE channel = ? AND post_id = ? AND file_unique_id = ?",
            entries,
        )


