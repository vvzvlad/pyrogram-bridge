# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=protected-access, wrong-import-position
# pylance: disable=reportMissingImports, reportMissingModuleSource

"""Rich Messages title/flag/special-block gates + defensive parse contours (#84/#85).

Covers the three post_parser gates (now routed through rich_tree.tree_of in phase 2),
the snapshot v7 rich_tree roundtrip, and the two defensive parse contours in
kurigram_compat. The rich RENDER pipeline itself is covered by test_rich_tree.py /
test_rich_pipeline.py; here we only assert the gates fire for an EMPTY rich tree (the
fallback-placard path a sentinel/failed parse takes).

Note on mocks: MagicMock(spec=Message) does NOT expose `rich_message`/`rich_tree` (both
are instance attributes absent from dir(Message)), so `getattr(message, 'rich_message',
None)` returns None unless a test sets it explicitly. This is exactly the production gate,
and makes the "no rich" tests non-vacuous.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pyrogram.types import Message, RichMessage, RichBlockUnsupported
from pyrogram.enums import MessageMediaType

from post_parser import PostParser
import message_snapshot as ms
import kurigram_compat as kc

# Phase-2 fallback placard (empty tree = a sentinel / failed parse / genuinely empty rich
# message). Owned solely by _format_special_media.
RICH_PLACEHOLDER_TEXT = "📰 This post's rich content could not be rendered"
RICH_TITLE = "📰 Rich post"


def _rich_mock(**overrides):
    """A textless, medialess Message mock that IS a rich post."""
    message = MagicMock(spec=Message)
    message.id = 7270
    message.media = None
    message.text = None
    message.caption = None
    message.web_page = None
    message.forward_origin = None
    message.service = None
    message.reactions = None
    message.rich_message = SimpleNamespace(blocks=[])
    for k, v in overrides.items():
        setattr(message, k, v)
    return message


def _plain_mock(**overrides):
    """A textless, medialess Message mock that is NOT a rich post (no rich_message)."""
    message = MagicMock(spec=Message)
    message.id = 42
    message.media = None
    message.text = None
    message.caption = None
    message.web_page = None
    message.forward_origin = None
    message.service = None
    message.reactions = None
    for k, v in overrides.items():
        setattr(message, k, v)
    return message


@pytest.fixture
def parser():
    return PostParser(MagicMock())


# --------------------------------------------------------------------------------------
# Task 7(a): a rich post renders the placeholder title, the `rich` flag and the block
# --------------------------------------------------------------------------------------
class TestRichPlaceholderRender:
    def test_title_is_rich_post_when_text_empty(self, parser):
        assert parser._generate_title(_rich_mock()) == RICH_TITLE

    def test_title_prefers_real_text_over_rich_fallback(self, parser):
        # A rich post that also carries plain text keeps the text-derived title; the rich
        # branch is a fallback only (base title wins).
        msg = _rich_mock(text="Hello this is a real longer caption here")
        assert parser._generate_title(msg) != RICH_TITLE
        assert "Hello" in parser._generate_title(msg)

    def test_rich_flag_present(self, parser):
        flags = parser._extract_flags(_rich_mock(), html_body="")
        assert "rich" in flags

    def test_rich_flag_discoverable(self):
        # get_all_possible_flags scrapes flags.append("...") from _extract_flags source.
        assert "rich" in PostParser.get_all_possible_flags()

    def test_special_media_block_is_placeholder(self, parser):
        block = parser._format_special_media(_rich_mock())
        assert block is not None
        assert RICH_PLACEHOLDER_TEXT in block
        assert "message-special" in block

    def test_rich_placeholder_wins_over_unsupported(self, parser):
        # A rich post that (defensively) still reports UNSUPPORTED media must render the
        # rich placeholder, never the "⚠️ Unsupported content" text.
        block = parser._format_special_media(_rich_mock(media=MessageMediaType.UNSUPPORTED))
        assert RICH_PLACEHOLDER_TEXT in block
        assert "Unsupported content" not in block


# --------------------------------------------------------------------------------------
# Task 7(c): a non-rich post shows nothing rich
# --------------------------------------------------------------------------------------
class TestNoRich:
    def test_no_rich_title(self, parser):
        assert parser._generate_title(_plain_mock()) != RICH_TITLE

    def test_no_rich_flag(self, parser):
        assert "rich" not in parser._extract_flags(_plain_mock(), html_body="")

    def test_no_rich_special_media(self, parser):
        # media=None and not rich → no special block at all.
        assert parser._format_special_media(_plain_mock()) is None


# --------------------------------------------------------------------------------------
# Task 7(b): snapshot v7 rich_tree roundtrip (parity of the placard from cache)
# --------------------------------------------------------------------------------------
class TestSnapshotRoundtrip:
    def test_version_is_current(self):
        # v7 = v6 (rich_present) replaced by the serialised rich_tree (#85).
        assert ms.SNAPSHOT_VERSION == 7

    def test_rich_tree_stored_for_rich_post(self):
        msg = SimpleNamespace(rich_message=SimpleNamespace(blocks=[]))
        snap = ms.snapshot_message(msg)
        # An empty-blocks rich message still yields a (non-None) tree.
        assert snap["rich_tree"] is not None
        assert snap["rich_tree"]["blocks"] == []

    def test_rich_tree_none_for_plain_post(self):
        # SimpleNamespace without rich_message → tree_of returns None.
        snap = ms.snapshot_message(SimpleNamespace())
        assert snap["rich_tree"] is None

    def test_restored_message_is_rich(self):
        snap = ms.snapshot_message(SimpleNamespace(rich_message=SimpleNamespace(blocks=[])))
        restored = ms.restore_message(snap)
        # rich_message is NOT snapshotted (only the tree is); the tree is restored.
        assert restored.rich_message is None
        assert restored.rich_tree is not None

    def test_roundtrip_placard_parity(self):
        parser = PostParser(MagicMock())
        snap = ms.snapshot_message(SimpleNamespace(rich_message=SimpleNamespace(blocks=[])))
        restored = ms.restore_message(snap)
        # The restored (cache-hit) message renders the same placard as a live one.
        assert parser._generate_title(restored) == RICH_TITLE
        assert "rich" in parser._extract_flags(restored, html_body="")
        assert RICH_PLACEHOLDER_TEXT in parser._format_special_media(restored)

    def test_plain_restored_message_has_no_rich(self):
        snap = ms.snapshot_message(SimpleNamespace())
        restored = ms.restore_message(snap)
        assert restored.rich_message is None
        assert ms.restore_message(snap).rich_tree is None


# --------------------------------------------------------------------------------------
# Task 7(d): kurigram_compat defensive contours
# --------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_rich_counters():
    kc.reset_counters()
    yield
    kc.reset_counters()


class TestMessageContour:
    async def test_none_passthrough_returns_none_not_sentinel(self, monkeypatch):
        # None-passthrough is the path of EVERY ordinary post. Neutering the `raw is None`
        # guard makes this test red (the None would reach setattr(parsed=None, ...)).
        called = {}

        async def fake_orig(client, rich_message, users, chats):
            called["rich_message"] = rich_message
            return None  # mirrors real RichMessage._parse for a None argument

        monkeypatch.setattr(kc, "_orig_richmessage_parse", fake_orig)
        result = await kc._wrapped_richmessage_parse(MagicMock(), None)
        assert result is None
        assert called["rich_message"] is None
        assert kc.get_rich_msg_parse_failed_count() == 0

    async def test_success_attaches_part_flag(self, monkeypatch):
        parsed_stub = RichMessage(blocks=[])

        async def fake_orig(*a, **k):
            return parsed_stub

        monkeypatch.setattr(kc, "_orig_richmessage_parse", fake_orig)
        raw = SimpleNamespace(part=False, photos=[1, 2], documents=[3])
        result = await kc._wrapped_richmessage_parse(MagicMock(), raw)
        assert result is parsed_stub
        assert result.part is False
        assert kc.get_rich_part_seen_count() == 0

    async def test_part_true_increments_counter(self, monkeypatch):
        async def fake_orig(*a, **k):
            return RichMessage(blocks=[])

        monkeypatch.setattr(kc, "_orig_richmessage_parse", fake_orig)
        raw = SimpleNamespace(part=True, photos=[], documents=[])
        result = await kc._wrapped_richmessage_parse(MagicMock(), raw)
        assert result.part is True
        assert kc.get_rich_part_seen_count() == 1

    async def test_exception_yields_sentinel(self, monkeypatch):
        async def boom(*a, **k):
            raise ValueError("kaboom")

        monkeypatch.setattr(kc, "_orig_richmessage_parse", boom)
        raw = SimpleNamespace(part=True)
        result = await kc._wrapped_richmessage_parse(MagicMock(), raw)
        assert isinstance(result, RichMessage)
        assert result.blocks == []
        assert getattr(result, "parse_failed") is True
        # part carried from raw so phase 3 can repair (part=True + empty blocks).
        assert getattr(result, "part") is True
        assert kc.get_rich_msg_parse_failed_count() == 1


class TestBlockContour:
    async def test_exception_yields_unsupported_node(self, monkeypatch):
        async def boom(*a, **k):
            raise KeyError("bad block")

        monkeypatch.setattr(kc, "_orig_richblock_parse", boom)
        node = await kc._wrapped_richblock_parse(MagicMock(), object())
        assert isinstance(node, RichBlockUnsupported)
        assert getattr(node, "parse_failed") is True
        assert kc.get_rich_block_parse_failed_count() == 1

    async def test_siblings_survive_one_bad_block(self, monkeypatch):
        good_a, good_b = object(), object()

        async def selective(client, block, *a, **k):
            if block == "BAD":
                raise ValueError("bad")
            return block  # echo the (good) block back

        monkeypatch.setattr(kc, "_orig_richblock_parse", selective)
        results = [
            await kc._wrapped_richblock_parse(MagicMock(), b)
            for b in (good_a, "BAD", good_b)
        ]
        assert results[0] is good_a
        assert results[2] is good_b
        assert isinstance(results[1], RichBlockUnsupported)
        assert getattr(results[1], "parse_failed") is True
        assert kc.get_rich_block_parse_failed_count() == 1


class TestHasParseFailures:
    def test_none(self):
        assert kc.has_parse_failures(None) is False

    def test_top_level_sentinel(self):
        assert kc.has_parse_failures(SimpleNamespace(parse_failed=True, blocks=[])) is True

    def test_clean_tree(self):
        rm = SimpleNamespace(
            blocks=[SimpleNamespace(), SimpleNamespace(items=[SimpleNamespace(blocks=[SimpleNamespace()])])]
        )
        assert kc.has_parse_failures(rm) is False

    def test_deeply_nested_failure(self):
        leaf = SimpleNamespace(parse_failed=True)
        rm = SimpleNamespace(
            blocks=[SimpleNamespace(items=[SimpleNamespace(blocks=[leaf])])]
        )
        assert kc.has_parse_failures(rm) is True

    def test_failure_in_table_cells(self):
        # cells is a list OF lists (RichBlockTable).
        bad_cell = SimpleNamespace(parse_failed=True)
        rm = SimpleNamespace(blocks=[SimpleNamespace(cells=[[SimpleNamespace(), bad_cell]])])
        assert kc.has_parse_failures(rm) is True


class TestNoOpDegradation:
    def test_install_is_noop_without_rich_classes(self, monkeypatch):
        # Simulate a Kurigram (e.g. a rollback to 2.2.23) without Rich* classes: install()
        # must return False and NOT raise — the container must not crash-loop.
        monkeypatch.setattr(kc, "_RICH_AVAILABLE", False)
        monkeypatch.setattr(kc, "_installed", False)
        assert kc.install() is False

    def test_install_is_idempotent(self):
        assert kc.install() is True
        assert kc.install() is True
