# flake8: noqa
# pylint: disable=import-outside-toplevel, missing-function-docstring, line-too-long
"""Golden-baseline replay helpers for the feed render pipeline.

Replays the frozen recorded corpus (tests/test_data/recorded/) through the REAL
cache-hit path and captures the full generate_channel_rss / generate_channel_html
output as an equivalence oracle for render changes.

The recorded corpus holds the Kurigram objects the prod bridge cache served, one file
per channel, written as JSON where every value carries its type (see the loader below):
  {channel}.json -> {"chatinfo": {"id", "title", "username"}, "messages": [<Message>, ...]}
The loader rebuilds real Kurigram objects from it, so the replay still drives the live
render path on real posts.

Run `python -m tests.golden_replay` from the repo root to (re)generate the goldens.
"""
import base64
import json
import os
import re
import time
from datetime import datetime
from types import SimpleNamespace

import pyrogram
from pyrogram.types.messages_and_media.message import Str

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
RECORDED_DIR = os.path.join(TESTS_DIR, "test_data", "recorded")
GOLDEN_DIR = os.path.join(TESTS_DIR, "test_data", "golden")

# Corpus channels frozen on `main` (spec "Этап 0"). exclude_flags / exclude_text
# scenarios are intentionally NOT in golden: filters do not change the bytes of the
# surviving posts; their membership/regex semantics are covered by dedicated unit tests.
CORPUS_CHANNELS = ["bladerunnerblues", "embedoka", "meow_design", "theyforcedme"]

# Recorded corpus is 100 messages/channel; the feed cap is 200. limit=100 exercises the
# whole corpus (grouping only reduces the post count below the message count).
GOLDEN_LIMIT = 100

# Fixed media-URL signing key so a golden captured on any machine/checkout reproduces on
# regeneration (a fresh checkout otherwise mints a new secrets.token_hex and every URL
# digest changes). Used identically by the generator and the comparison test.
GOLDEN_SIGNING_KEY = "stage0-golden-fixed-signing-key-0000000000000000"


# --------------------------------------------------------------------------- #
# Replay loader.
#
# The corpus is a FROZEN prod recording (no live Telegram to re-record it from). Every
# value is stored with its type, recursively:
#   None / bool / int / float / str  -> as is
#   list (pyrogram.types.List too)   -> list of encoded items, loaded as a plain list
#   Str (a str carrying .entities)   -> {"$str": text, "entities": [...] or null}
#   enum                             -> {"$enum": class name in pyrogram.enums, "name": member}
#   datetime                         -> {"$datetime": isoformat()}, recorded under TZ=UTC
#   bytes                            -> {"$bytes": base64}
#   pyrogram Object                  -> {"$type": class name in pyrogram.types, attr: value, ...}
# An Object keeps every __dict__ attribute except `raw` (the render never reads it) and
# `_client` (the recording never kept it), the None-valued ones included, so hasattr()
# answers as it does on the live object for every data field. Types are
# looked up by their PUBLIC name, so a class moving between modules inside Kurigram does
# not break the load, while a class or enum member the library does not ship raises out
# of the loader: there are no stand-ins. (ChatColor, absent from Kurigram 2.2.26 and never
# read by the feed, is recorded as null.) The goldens are never regenerated to absorb a
# library bump: goldens matching byte-for-byte is the evidence that the bump did not move
# the render.
# --------------------------------------------------------------------------- #
def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$type" in value:
        cls = getattr(pyrogram.types, value["$type"])
        # Restores the state the way Object.__setstate__ does, bypassing properties.
        obj = cls.__new__(cls)
        obj.__dict__.update({attr: _decode(item) for attr, item in value.items() if attr != "$type"})
        return obj
    if "$str" in value:
        return Str(value["$str"]).init(_decode(value["entities"]))
    if "$enum" in value:
        return getattr(pyrogram.enums, value["$enum"])[value["name"]]
    if "$datetime" in value:
        return datetime.fromisoformat(value["$datetime"])
    return base64.b64decode(value["$bytes"])  # the last tag: any other one raises KeyError


def load_recorded(channel):
    """Load a recorded {channel}.json.

    Returns (messages: list[Message], chatinfo_data: dict)."""
    with open(os.path.join(RECORDED_DIR, f"{channel}.json"), encoding="utf-8") as f:
        recorded = json.load(f)
    return _decode(recorded["messages"]), recorded["chatinfo"]


def patch_tg_cache(monkeypatch, channel, restored=False):
    """Monkeypatch tg_cache.cached_get_chat_history / cached_get_chat to return the
    recorded objects for `channel`. The feed functions lazy-import tg_cache, so patching
    the module resolves late and works (mirrors test_stage4_eventloop.py).

    restored=True serves restore_messages(snapshot_messages(...)) of the recorded objects
    instead: what a history-cache hit hands to the feed. Returns the served message list."""
    messages, chatinfo = load_recorded(channel)
    if restored:
        from message_snapshot import restore_messages, snapshot_messages
        messages = restore_messages(snapshot_messages(messages))

    async def fake_get_chat_history(client, channel_id, limit=20):
        return messages

    async def fake_get_chat(client, channel_id):
        # cached_get_chat returns SimpleNamespace(**data) with .id/.title/.username.
        return SimpleNamespace(**chatinfo)

    monkeypatch.setattr("tg_cache.cached_get_chat_history", fake_get_chat_history, raising=False)
    monkeypatch.setattr("tg_cache.cached_get_chat", fake_get_chat, raising=False)
    return messages


def pin_environment(monkeypatch):
    """Apply the spec's non-TZ determinism pins (TZ=UTC is pinned globally in conftest /
    the __main__ bootstrap so both the test runner and the generator agree)."""
    import post_parser
    import rss_generator
    from url_signer import KeyManager

    # Pin the media-URL signing key (see GOLDEN_SIGNING_KEY).
    monkeypatch.setattr(KeyManager, "signing_key", GOLDEN_SIGNING_KEY)

    # time_based_merge=True so the meow_design time-cluster core is actually exercised.
    # The flag is read from rss_generator.Config at call time; post_parser.Config is a
    # sibling dict from the same get_settings() — pin both (cheap insurance vs. drift).
    monkeypatch.setitem(rss_generator.Config, "time_based_merge", True)
    monkeypatch.setitem(post_parser.Config, "time_based_merge", True)

    # No media-id DB side effect outside tests/ (byte-neutral for the feed, but the write
    # to ./data/media_file_ids.db is a forbidden side effect). upsert is imported INTO the
    # post_parser namespace, so patch it there.
    monkeypatch.setattr(post_parser, "upsert_media_file_ids_bulk_sync", lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# Capture.
# --------------------------------------------------------------------------- #
def capture_rss(channel):
    import asyncio
    from rss_generator import generate_channel_rss
    return asyncio.run(generate_channel_rss(channel, client=SimpleNamespace(), limit=GOLDEN_LIMIT))


def capture_html(channel):
    import asyncio
    from rss_generator import generate_channel_html
    return asyncio.run(generate_channel_html(channel, client=SimpleNamespace(), limit=GOLDEN_LIMIT))


# --------------------------------------------------------------------------- #
# Normalizations (applied SYMMETRICALLY to golden and actual before comparison).
# Only the spec-sanctioned set — over-normalizing is exactly how a regression hides.
# --------------------------------------------------------------------------- #
# feedgen sets <lastBuildDate> to now() once in the FeedGenerator constructor: stable
# within a process, but changes between capture runs — regex it out on both sides.
_LASTBUILDDATE_RE = re.compile(r"<lastBuildDate>.*?</lastBuildDate>", re.DOTALL)
# feedgen 1.0.0 emits no <generator>; normalized anyway as cheap insurance vs. a lib upgrade.
_GENERATOR_RE = re.compile(r"<generator>.*?</generator>", re.DOTALL)
# No flag-order normalization: merged-post flags are built in deterministic first-seen
# order (rss_generator._render_messages_groups: dict.fromkeys(...)), so the golden stores
# the real order.


def normalize_rss(xml):
    xml = _LASTBUILDDATE_RE.sub("<lastBuildDate/>", xml)
    xml = _GENERATOR_RE.sub("<generator/>", xml)
    return xml


def normalize_html(html):
    return html


def golden_path(channel, kind):
    ext = {"rss": "rss.xml", "html": "feed.html"}[kind]
    return os.path.join(GOLDEN_DIR, f"{channel}.{ext}")


# --------------------------------------------------------------------------- #
# Generator entry point: `python -m tests.golden_replay`
# --------------------------------------------------------------------------- #
def _bootstrap_standalone():
    """Reproduce the conftest bootstrap for the standalone generator: repo root on the
    path, mocked config, UTC timezone."""
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import tests.mock_config as _mock_config
    sys.modules["config"] = _mock_config
    os.environ["TZ"] = "UTC"
    time.tzset()


def generate_all():
    from _pytest.monkeypatch import MonkeyPatch

    os.makedirs(GOLDEN_DIR, exist_ok=True)
    for channel in CORPUS_CHANNELS:
        mp = MonkeyPatch()
        try:
            pin_environment(mp)
            patch_tg_cache(mp, channel)
            rss = capture_rss(channel)
            html = capture_html(channel)
        finally:
            mp.undo()
        with open(golden_path(channel, "rss"), "w", encoding="utf-8") as f:
            f.write(rss)
        with open(golden_path(channel, "html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(f"{channel}: rss={len(rss)}B items={rss.count('<item>')} "
              f"html={len(html)}B posts={html.count('message-body') if html else 0}")


if __name__ == "__main__":
    _bootstrap_standalone()
    generate_all()
