# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Regression tests for issue #52 — download a poll's description_media.

Before the fix, download_media_file short-circuited every POLL message with
`return None, False` BEFORE resolving the file_id. Consequences in prod:
  - a poll's rendered /media URL always 404'd (file_path=None) -> broken image;
  - the background worker treated the (None, False) as SUCCESS -> _clear_download_failure
    -> backoff never armed -> the file was re-queued every cache sweep forever.

The early return is gone, so a POLL now flows through the normal path:
  - a poll WITH the requested description_media resolves its fid and downloads to cache;
  - a poll WITHOUT it falls into the standard "fid not found" branch: 404 + the SQLite row
    is removed, so the endless re-queue dies on its own.
"""
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pyrogram.enums import MessageMediaType

import api_server


def _poll_message(post_id, *, description_media=None):
    """Minimal POLL Message stand-in. `.video` is None so is_large_video stays False."""
    poll = SimpleNamespace(question="Pick one?", options=[])
    if description_media is not None:
        poll.description_media = description_media
    return SimpleNamespace(
        id=post_id,
        media=MessageMediaType.POLL,
        poll=poll,
        video=None,
    )


@pytest.mark.asyncio
async def test_poll_with_description_media_downloads(monkeypatch, tmp_path):
    """A poll carrying description_media.photo resolves its fid and downloads to cache
    instead of being short-circuited to (None, False)."""
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "cache"))

    msg = _poll_message(
        501,
        description_media=SimpleNamespace(
            photo=SimpleNamespace(file_unique_id="poll_uid", file_id="poll_fid")
        ),
    )

    async def fake_get_messages(channel_id, post_id):
        return msg

    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get_messages)

    downloaded = []

    async def fake_download_atomic(file_id, final_path, timeout):
        # Only the resolved poll fid must reach the downloader.
        downloaded.append(file_id)
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        with open(final_path, "wb") as fh:
            fh.write(b"pollpic")
        return final_path

    monkeypatch.setattr(api_server, "_download_atomic", fake_download_atomic)

    file_path, delete_after = await api_server.download_media_file("PollChan", 501, "poll_uid")

    assert delete_after is False
    assert file_path is not None
    assert downloaded == ["poll_fid"]
    # File landed in the per-post cache dir under its file_unique_id.
    assert os.path.exists(file_path)
    assert os.path.getsize(file_path) > 0
    assert file_path.endswith(os.path.join("PollChan", "501", "poll_uid"))


@pytest.mark.asyncio
async def test_poll_without_media_404_and_removes_db_row(monkeypatch, tmp_path):
    """A poll whose requested media is absent must NOT return a (None, False) 'success':
    it hits the standard fid-not-found branch -> 404 + SQLite row removal, so the background
    dribble stops on its own."""
    monkeypatch.setattr(api_server, "MEDIA_CACHE_DIR", str(tmp_path / "cache"))

    # Plain poll: no description_media at all.
    msg = _poll_message(502)

    async def fake_get_messages(channel_id, post_id):
        return msg

    monkeypatch.setattr(api_server.client, "safe_get_messages", fake_get_messages)

    removed = []

    def fake_remove(db_path, entries):
        removed.extend(entries)

    monkeypatch.setattr(api_server, "remove_media_file_ids_sync", fake_remove)

    # A poll without media must never attempt a download.
    async def fail_download_atomic(*a, **k):
        raise AssertionError("_download_atomic must not be called for a poll without media")

    monkeypatch.setattr(api_server, "_download_atomic", fail_download_atomic)

    with pytest.raises(HTTPException) as ei:
        await api_server.download_media_file("PollChan", 502, "missing_uid")

    assert ei.value.status_code == 404
    # Row removed in canonical (str(channel), post_id, fid) form -> no more sweep re-queue.
    assert removed == [("PollChan", 502, "missing_uid")]
