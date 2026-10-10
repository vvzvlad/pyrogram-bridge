# flake8: noqa
# pylint: disable=missing-function-docstring, missing-class-docstring, redefined-outer-name, line-too-long
"""Strict restore test for the snapshot cache.

The parity oracle (test_snapshot_render_parity.py) and the restored golden variant
(test_stage0_golden.py) compare a cache-hit render with a live one, so they only notice a
field the restore drops when some corpus post carries a value for it. This test watches the
reads instead: every restored object records each attribute the render asks it for and does
not have, as (path, name), and the recorded set must equal EXPECTED_MISSES in both
directions. A field the render reads but the restore does not expose shows up as a new miss
even when no corpus post has a value for it.

Watched: the restored CachedMessage, every nested object (which must be built through
message_snapshot.SimpleNamespace, or the test fails) and every CachedStr. Not watched: values
restored as plain str, enum, datetime or dict (chat.type, service, poll question and option
texts, rich_tree).
"""
from datetime import datetime
from enum import Enum
from types import SimpleNamespace

import pytest

import message_snapshot
from message_snapshot import restore_messages, snapshot_messages
from tests import golden_replay as gr
from tests.test_snapshot_render_parity import _render, build_corpus


# Frozen as recorded at SNAPSHOT_VERSION 9. A new miss is a field the render reads and the
# restore does not expose; a miss that disappears is removed from this set.
EXPECTED_MISSES = {
    # forward_origin restores only the keys the live MessageOrigin* object had: the render
    # tells the origin kinds apart with hasattr.
    ("forward_origin", "chat"),
    ("forward_origin", "chat_id"),
    ("forward_origin", "sender_user"),
    ("forward_origin", "sender_user_name"),
    # rich_tree.tree_of's memo flag, read with a default before it is first set.
    ("message", "_rich_tree_computed"),
    # _save_media_file_ids reads file_size off every selected media object for its >100MB
    # skip; these objects do not snapshot it.
    ("photo", "file_size"),
    ("sticker", "file_size"),
    ("voice", "file_size"),
    ("web_page.photo", "file_size"),
}


def _index_paths(messages, paths):
    """Replace ``paths`` with id(object) -> path for these restored messages: the message
    itself is "message", a nested object is its attribute chain ("poll.description_media.photo"),
    and a list item gets "[]" ("reactions.reactions[]")."""
    paths.clear()

    def walk(obj, prefix):
        for name, value in vars(obj).items():
            path = prefix + name
            children = [(item, path + "[]") for item in value] if isinstance(value, list) else [(value, path)]
            for child, child_path in children:
                if isinstance(child, message_snapshot.CachedStr):
                    paths[id(child)] = child_path
                elif isinstance(child, SimpleNamespace):
                    # A namespace built outside message_snapshot.SimpleNamespace would not
                    # record its misses: the test would silently stop watching that subtree.
                    assert "__getattr__" in vars(type(child)), \
                        f"{child_path}: built outside message_snapshot.SimpleNamespace"
                    paths[id(child)] = child_path
                    walk(child, child_path + ".")
                else:
                    # Exactly str: a str subclass carrying extra attributes would go unwatched.
                    assert child is None or type(child) is str or isinstance(child, (int, float, datetime, Enum, dict)), \
                        (child_path, type(child))

    for message in messages:
        paths[id(message)] = "message"
        walk(message, "")


@pytest.fixture
def recorder(monkeypatch):
    """Make restored objects record their misses. ``__getattr__`` runs only for a missing
    attribute; it records the miss and raises AttributeError, so getattr(x, n, default) and
    hasattr behave as before. An object _index_paths did not place records under the path
    "?". Returns (paths, misses)."""
    paths, misses = {}, set()

    def record_miss(obj, name):
        if not name.startswith("__"):
            misses.add((paths.get(id(obj), "?"), name))
        raise AttributeError(name)

    class RecordingNamespace(SimpleNamespace):
        __getattr__ = record_miss

    monkeypatch.setattr(message_snapshot, "SimpleNamespace", RecordingNamespace)
    monkeypatch.setattr(message_snapshot.CachedMessage, "__getattr__", record_miss, raising=False)
    monkeypatch.setattr(message_snapshot.CachedStr, "__getattr__", record_miss, raising=False)
    return paths, misses


def test_render_misses_on_restored_objects_are_expected(recorder, monkeypatch):
    paths, misses = recorder
    gr.pin_environment(monkeypatch)

    for _name, live in build_corpus():
        restored = restore_messages(snapshot_messages(live))
        _index_paths(restored, paths)
        _render(restored, monkeypatch)

    # GOLDEN_LIMIT and the real chatinfo: with _render's limit=50 only part of a channel's
    # posts would reach the RSS feed.
    for channel in gr.CORPUS_CHANNELS:
        restored = gr.patch_tg_cache(monkeypatch, channel, restored=True)
        _index_paths(restored, paths)
        gr.capture_rss(channel)
        gr.capture_html(channel)

    assert misses, "no miss was recorded: restored objects are no longer built through the recording classes"
    assert misses == EXPECTED_MISSES, (
        f"new misses: {sorted(misses - EXPECTED_MISSES)}; gone misses: {sorted(EXPECTED_MISSES - misses)}")
