# flake8: noqa
# pylint: disable=missing-function-docstring, redefined-outer-name, line-too-long
# pylint: disable=protected-access, wrong-import-position, import-outside-toplevel
"""Stage-2 footer tests (render-pipeline refactor epic, issue #29/#34).

Stage 2 removed rss_generator.processed_message_to_tg_message and renders the merged
footer DIRECTLY from the real main Message. These tests lock the registry §3 items that
become visible as a consequence:

  §3.6  custom-emoji reactions in the merged footer get a separate "❓ N" span each
        (no more aggregation into one "❓ N"), matching single posts.
  §3.7  the merged footer date comes from the naive-local date of the real Message,
        not a UTC mock — verified with a NON-UTC TZ where the delta is visible (the
        TZ=UTC golden cannot see it).
  §3.15 an empty reactions object no longer emits a leading footer separator (single
        posts too).

Messages are built with the shared SimpleNamespace helper (naive dates where relevant,
as kurigram emits on prod).
"""
import os
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from pyrogram.enums import MessageMediaType
from post_parser import PostParser
from rss_generator import _render_messages_groups
from tests.test_stage4_eventloop import make_message


def _custom_reaction(count, custom_emoji_id):
    # No `.emoji` attribute -> _reactions_views_links falls to the custom-emoji "❓" branch.
    return SimpleNamespace(count=count, custom_emoji_id=custom_emoji_id)


def _normal_reaction(count, emoji):
    return SimpleNamespace(count=count, emoji=emoji)


def _reactions(*items):
    return SimpleNamespace(reactions=list(items))


def _footer_of(post):
    """Extract the <div class="message-footer">…</div> inner html from a rendered post."""
    html = post["html"]
    marker = '<div class="message-footer">'
    start = html.index(marker) + len(marker)
    return html[start:]


# --------------------------------------------------------------------------- #
# §3.6 — custom-emoji reactions render as one span each, in the merged footer.
# --------------------------------------------------------------------------- #
def test_3_6_two_custom_emoji_render_as_separate_spans():
    """Single-post baseline: _reactions_views_links (the function the merged footer now
    uses on the real Message) emits one '❓ N' span per custom emoji, never aggregated."""
    parser = PostParser(SimpleNamespace())
    msg = make_message(70, text="hi")
    msg.reactions = _reactions(_custom_reaction(4, 111), _custom_reaction(2, 222))
    html = parser._reactions_views_links(msg)
    assert html.count('<span class="reaction">❓ 4') == 1
    assert html.count('<span class="reaction">❓ 2') == 1
    # NOT aggregated into a single "❓ 6".
    assert "❓ 6" not in html


def test_3_6_merged_footer_keeps_custom_spans_separate():
    """Merged footer, rendered from the real main Message, keeps a span per custom emoji.
    The old dict round-trip (_extract_reactions) collapsed both customs into one '❓ 6'."""
    parser = PostParser(SimpleNamespace())
    main = make_message(80, text="main text")
    main.media_group_id = "mg_36"
    main.reactions = _reactions(
        _normal_reaction(13, "🔥"),
        _custom_reaction(4, 111),
        _custom_reaction(2, 222),
    )
    other = make_message(81, text="second part")
    other.media_group_id = "mg_36"

    posts = _render_messages_groups([[main, other]], parser)
    assert len(posts) == 1
    footer = _footer_of(posts[0])
    assert "merged" in posts[0]["flags"]
    assert footer.count('<span class="reaction">❓ 4') == 1
    assert footer.count('<span class="reaction">❓ 2') == 1
    assert "❓ 6" not in footer  # would appear if the customs were still aggregated


# --------------------------------------------------------------------------- #
# §3.15 — an empty reactions object emits no leading footer separator.
# --------------------------------------------------------------------------- #
_SEP = "&nbsp;&nbsp;|&nbsp;&nbsp;"


def test_3_15_empty_reactions_no_leading_separator():
    """A reactions object with an empty list must not produce a leading '…|…' separator.
    Affects single posts (this call site) as well as merged ones."""
    parser = PostParser(SimpleNamespace())
    msg = make_message(90, text="hi")
    msg.reactions = _reactions()  # object present, no reactions inside
    html = parser._reactions_views_links(msg)
    # First visible element must be the views span, not the separator.
    assert html.startswith('<span class="views">')
    assert not html.startswith(_SEP)


def test_3_15_empty_reactions_single_post_render():
    """Same via the real render path: the footer's first line starts with views, no
    leading separator injected by the empty reactions object."""
    parser = PostParser(SimpleNamespace())
    msg = make_message(91, text="hi")
    msg.reactions = _reactions()
    posts = _render_messages_groups([[msg]], parser)
    assert len(posts) == 1
    footer = _footer_of(posts[0])
    assert f'<br>\n{_SEP}<span class="views">' not in footer
    assert '<br>\n<span class="views">' in footer


def test_3_15_present_reactions_still_render():
    """Control: a non-empty reactions object still renders its spans (the §3.15 guard is
    not a blanket drop of the reactions line)."""
    parser = PostParser(SimpleNamespace())
    msg = make_message(92, text="hi")
    msg.reactions = _reactions(_normal_reaction(5, "🔥"))
    html = parser._reactions_views_links(msg)
    assert html.startswith('<span class="reaction">🔥 5')


# --------------------------------------------------------------------------- #
# §3.7 — merged footer date == single-post date under a NON-UTC TZ.
# --------------------------------------------------------------------------- #
@pytest.fixture
def moscow_tz():
    """Pin a non-UTC TZ for the duration of the test, then restore the conftest UTC pin.
    Without restoring, a leaked non-UTC TZ would corrupt the UTC-pinned golden tests."""
    os.environ["TZ"] = "Europe/Moscow"
    time.tzset()
    try:
        yield
    finally:
        os.environ["TZ"] = "UTC"
        time.tzset()


def _date_span(footer):
    marker = '<span class="date">'
    start = footer.index(marker) + len(marker)
    end = footer.index("</span>", start)
    return footer[start:end]


def test_3_7_merged_footer_date_matches_single_post_non_utc(moscow_tz):
    """On a TZ≠UTC server the merged footer date must equal the single-post date for the
    same message. Before stage 2 the merged path built a UTC mock date (from the naive
    date's timestamp) → a visible offset; now both render the real naive-local date."""
    parser = PostParser(SimpleNamespace())
    # NAIVE date, as kurigram emits on prod. Under Europe/Moscow the old UTC mock would
    # have shifted this by the TZ offset.
    naive_date = datetime(2026, 5, 5, 17, 21, 14)

    def _fresh_main():
        m = make_message(100, text="main text", date=naive_date)
        return m

    # Single post.
    single_posts = _render_messages_groups([[_fresh_main()]], parser)
    assert len(single_posts) == 1
    single_date = _date_span(_footer_of(single_posts[0]))

    # Merged group with the same main message + one extra member.
    main = _fresh_main()
    main.media_group_id = "mg_37"
    other = make_message(101, text="second part", date=naive_date)
    other.media_group_id = "mg_37"
    merged_posts = _render_messages_groups([[main, other]], parser)
    assert len(merged_posts) == 1
    assert "merged" in merged_posts[0]["flags"]
    merged_date = _date_span(_footer_of(merged_posts[0]))

    assert merged_date == single_date
    # And it is the naive wall-clock time, not a UTC-shifted one.
    assert merged_date == "05/05/26, 17:21:14"
