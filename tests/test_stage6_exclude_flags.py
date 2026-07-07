# flake8: noqa
# pylint: disable=missing-function-docstring, redefined-outer-name
"""Stage-6: unit lock for the `exclude_flags` feed filter (issue #33, epic #34).

The filter (rss_generator._render_messages_groups) is a user-facing query-param
that drops posts from the feed by flag. It is NOT exercised by the stage-0 golden
oracle (golden_replay runs without exclude_flags), and stage 6 rewrote it from a
`for/continue` loop into a list-comprehension. These tests pin the membership
semantics so the rewrite (and any future one) stays honest — this is the
"dedicated unit tests" that golden_replay.py's header comment refers to.

Flag shapes under the test config: a plain text post carries ['no_image']; a
merged group additionally carries 'merged'. (Every rendered post carries at least
one flag, so the `"all"` filter drops every real post; the guard's flagless-keep
branch — `and post['flags']` — is a media-path concern proven separately by the
equivalence check in the PR, not reproducible with plain fixtures here.)
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from rss_generator import _render_messages_groups
from post_parser import PostParser
from tests.test_stage4_eventloop import make_message


@pytest.fixture
def parser():
    return PostParser(SimpleNamespace())


def _plain(mid, date=None):
    # Plain text post -> flags == ['no_image'].
    return make_message(mid, text="plain", date=date)


def _merged_group(mid, date=None):
    # Merged group -> flags == ['no_image', 'merged']; message_id is the main id.
    main = make_message(mid, text="m", date=date)
    main.media_group_id = f"g{mid}"
    other = make_message(mid + 1, text="o", date=date)
    other.media_group_id = f"g{mid}"
    return [main, other]


def _ids(posts):
    return [p["message_id"] for p in posts]


def test_exclude_flags_all_drops_flagged_posts(parser):
    # `"all"` special case: any post carrying >=1 flag is dropped.
    posts = _render_messages_groups(
        [[_plain(10)], [_plain(11)]], parser, exclude_flags="all"
    )
    assert _ids(posts) == []  # both ['no_image'] -> dropped


def test_exclude_flags_specific_drops_matching_keeps_nonmatching(parser):
    # Exclude a concrete flag: the post carrying it is dropped, one without it kept.
    posts = _render_messages_groups(
        [_merged_group(20), [_plain(22)]], parser, exclude_flags="merged"
    )
    ids = _ids(posts)
    assert 22 in ids  # ['no_image'] has no 'merged' -> kept
    assert 20 not in ids  # ['no_image', 'merged'] -> dropped


def test_exclude_flags_none_or_nonmatching_keeps_all(parser):
    base = [[_plain(30)], [_plain(31)]]
    assert len(_render_messages_groups(base, parser)) == 2  # no filter
    assert (
        len(_render_messages_groups(base, parser, exclude_flags="nonexistent")) == 2
    )  # flag matches nothing -> all kept


def test_exclude_flags_preserves_survivor_order(parser):
    # Distinct descending dates so the trailing date-sort is deterministic;
    # dropping the middle (merged) post must not reorder the survivors.
    a = make_message(40, text="a", date=datetime(2024, 1, 3, tzinfo=timezone.utc))
    mid = _merged_group(41, date=datetime(2024, 1, 2, tzinfo=timezone.utc))
    c = make_message(43, text="c", date=datetime(2024, 1, 1, tzinfo=timezone.utc))
    posts = _render_messages_groups(
        [[a], mid, [c]], parser, exclude_flags="merged"
    )
    assert _ids(posts) == [40, 43]  # merged(41) dropped; a before c by date-desc
