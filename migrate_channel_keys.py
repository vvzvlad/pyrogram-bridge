#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=missing-function-docstring, broad-exception-caught, logging-fstring-interpolation

"""One-shot migration of existing cache data to the canonical channel key.

Collapses non-canonical (mixed-case) channel names in both the on-disk media cache
(data/cache/<channel>/...) and the SQLite media_file_ids table onto their canonical,
lowercase form. Numeric '-100...' ids are already canonical and are never touched.

Unit of work = a WHOLE channel, processed FS FIRST then SQL. The order is essential:
if the FS rename/merge fails, the channel's SQL rows are left old-cased so the old tree
is still reachable by the media sweeper via its DB rows (no eternal orphan directory).

The migration is idempotent: a second run finds nothing to do and is a no-op.
"""

import os
import shutil
import sqlite3
import logging

from channel_key import canonical_channel_key

logger = logging.getLogger(__name__)


def _is_safe_channel_segment(name: str) -> bool:
    """True iff ``name`` is a plain single path segment safe to join under cache_dir.

    Defends the destructive FS ops below (os.rename / _merge_dir_tree / shutil.rmtree)
    against a dirty channel string that reached the DB or a crafted dir on disk,
    regardless of any upstream route-traversal. A dirty name (containing '/', '\\' or a
    '..' component, or that is not a bare basename, or empty/'.') would let a rename or
    rmtree escape cache_dir, so it is rejected and the channel is left un-migrated.
    """
    if not name or name == '.':
        return False
    if '/' in name or '\\' in name:
        return False
    if '..' in name.replace('\\', '/').split('/'):
        return False
    if os.path.basename(name) != name:
        return False
    return True


def _merge_dir_tree(src: str, dst: str) -> None:
    """Merge every file under ``src`` into ``dst`` where an existing ``dst`` file WINS.

    Both trees share the same filesystem (siblings under the media cache root), so files
    are moved with os.rename. Any file that already exists in ``dst`` is kept and the
    ``src`` copy is discarded. Emptied ``src`` remnants are removed at the end.
    """
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dst if rel == '.' else os.path.join(dst, rel)
        os.makedirs(target_root, exist_ok=True)
        for name in files:
            s = os.path.join(root, name)
            t = os.path.join(target_root, name)
            if os.path.exists(t):
                os.remove(s)  # existing file in dst wins
            else:
                os.rename(s, t)
    shutil.rmtree(src, ignore_errors=True)


def migrate_channel_keys_sync(db_path: str, cache_dir: str) -> None:
    """Migrate mixed-case channel cache dirs and DB rows onto the canonical key.

    Blocking (FS renames + SQLite). Call via asyncio.to_thread from an async context.
    """
    rows_merged = 0
    dirs_renamed = 0
    samefile_noops = 0
    failures = 0

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        # ------------------------------------------------------------------ #
        # Candidate channels: mixed-case first-level cache dirs UNION mixed-case DB rows.
        # ------------------------------------------------------------------ #
        candidates: set[str] = set()
        try:
            for entry in os.listdir(cache_dir):
                full = os.path.join(cache_dir, entry)
                if not os.path.isdir(full):
                    continue
                if entry.startswith('-100'):
                    continue
                if entry != entry.lower():
                    candidates.add(entry)
        except FileNotFoundError:
            pass  # No cache dir yet — nothing to migrate on the FS side.

        try:
            cur = conn.execute(
                "SELECT DISTINCT channel FROM media_file_ids "
                "WHERE channel != lower(channel) AND channel NOT LIKE '-100%'"
            )
            for (ch,) in cur.fetchall():
                candidates.add(ch)
        except sqlite3.Error as e:
            logger.error(f"migrate_channel_keys: DB candidate query failed: {e}")

        for name in sorted(candidates):
            # Traversal guard: reject any dirty channel name BEFORE it reaches the
            # destructive FS ops (rename/merge/rmtree). Applies to both candidate sources
            # (DB rows and the dir listing). A rejected channel is left as-is (its old-cased
            # rows/dirs survive until the 20-day sweep, same as any un-migrated channel).
            if not _is_safe_channel_segment(name):
                logger.warning(
                    f"migrate_channel_keys: unsafe channel name {name!r} rejected "
                    f"(would escape cache_dir), skipping"
                )
                failures += 1
                continue
            canonical = canonical_channel_key(name)
            if canonical == name:
                # Nothing to change (already canonical) — defensive, candidates shouldn't hit this.
                continue
            try:
                # ---------------------------- FS step ---------------------------- #
                src = os.path.join(cache_dir, name)
                dst = os.path.join(cache_dir, canonical)
                if os.path.isdir(src):
                    try:
                        if os.path.exists(dst) and os.path.samefile(src, dst):
                            # Case-insensitive FS (macOS/APFS, Docker Desktop for Mac):
                            # src and dst are ONE directory. Renaming/merging would destroy
                            # the channel's entire cache — treat as a pure no-op.
                            samefile_noops += 1
                        elif not os.path.exists(dst):
                            os.rename(src, dst)
                            dirs_renamed += 1
                        else:
                            # Genuinely different dirs (case-sensitive FS, both forms present):
                            # per-file merge with the existing dst file winning.
                            _merge_dir_tree(src, dst)
                            dirs_renamed += 1
                    except OSError as e:
                        # FS step failed (EACCES etc.): mark orphan candidate and SKIP the SQL
                        # step so the old-cased DB rows keep the old tree visible to the sweeper.
                        logger.error(
                            f"migrate_channel_keys: FS step failed for {name!r} -> {canonical!r} "
                            f"(orphan candidate, SQL skipped): {e}"
                        )
                        failures += 1
                        continue

                # ---------------------------- SQL step --------------------------- #
                old_rows = conn.execute(
                    "SELECT post_id, file_unique_id, added, mime_type "
                    "FROM media_file_ids WHERE channel = ?",
                    (name,),
                ).fetchall()
                for (post_id, file_unique_id, added, mime_type) in old_rows:
                    twin = conn.execute(
                        "SELECT added, mime_type FROM media_file_ids "
                        "WHERE channel = ? AND post_id = ? AND file_unique_id = ?",
                        (canonical, post_id, file_unique_id),
                    ).fetchone()
                    if twin is None:
                        # No lowercase twin: a plain re-key is safe (no PK conflict).
                        conn.execute(
                            "UPDATE media_file_ids SET channel = ? "
                            "WHERE channel = ? AND post_id = ? AND file_unique_id = ?",
                            (canonical, name, post_id, file_unique_id),
                        )
                    else:
                        # Twin exists: merge (added = max, prefer non-NULL mime_type) into the
                        # canonical row, then delete the old-cased row. A bare
                        # UPDATE ... SET channel=lower(channel) is FORBIDDEN (PK conflict).
                        t_added, t_mime = twin
                        new_added = max(added, t_added)
                        new_mime = t_mime if t_mime is not None else mime_type
                        conn.execute(
                            "UPDATE media_file_ids SET added = ?, mime_type = ? "
                            "WHERE channel = ? AND post_id = ? AND file_unique_id = ?",
                            (new_added, new_mime, canonical, post_id, file_unique_id),
                        )
                        conn.execute(
                            "DELETE FROM media_file_ids "
                            "WHERE channel = ? AND post_id = ? AND file_unique_id = ?",
                            (name, post_id, file_unique_id),
                        )
                        rows_merged += 1
                conn.commit()
            except Exception as e:
                # Never crash startup: log and move on. A re-run is an idempotent no-op.
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.error(f"migrate_channel_keys: channel {name!r} migration failed: {e}")
                failures += 1
    finally:
        conn.close()

    logger.info(
        f"migration_summary: rows merged {rows_merged}, dirs renamed {dirs_renamed}, "
        f"samefile no-ops {samefile_noops}, failures {failures}"
    )
