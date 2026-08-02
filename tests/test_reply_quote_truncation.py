# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""Reply-quote truncation for a NEIGHBOURING post of the same channel (post_parser).

A post answering the post right above it used to reprint that post in full, so two adjacent
RSS entries read as half the same text (t.me/univelis/1472 quoted whole inside /1473), while
Telegram itself shows only a short preview there. The quote is therefore cut — but ONLY when
all three conditions hold: the feed is a CHANNEL, the target belongs to that same channel, and
it is at most `reply_quote_truncate_distance` ids above the post. Every other reply (a group or
discussion feed, a foreign channel, a user's message, an older post) keeps the full quote,
because there the quoted text is the only place the reader can see what is being answered.

The cut happens at render time only: the cache stores reply targets in full, so changing the
setting needs no cache invalidation and live/cached renders stay byte-identical (pinned by
test_truncated_block_is_identical_live_and_from_cache).
"""
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pyrogram.enums import ChatType

import post_parser
from message_snapshot import snapshot_message, restore_message
from post_parser import MARKER_QUOTE_END, MARKER_REPLY_OPEN, PostParser, _truncate_quote_html
from sanitizer import sanitize_html


CHANNEL_ID = -1001287848294
GROUP_ID = -1001111111111
LONG_TEXT = ("Очень длинный пост канала, который целиком повторять в соседней записи "
             "ленты не нужно, потому что читатель уже видел его в предыдущем элементе. ") * 4
# Text pyrogram hands over UNESCAPED (it only escapes inside entity ranges), angle brackets and
# all — the shape that used to make the visible counter treat half the quote as free markup.
ANGLE_TEXT = ("если x < 5 то мы получаем очень длинный текст поста, который должен быть обрезан " * 6
              + " и y > 3, конец.")


class FakeStr(str):
    """Stand-in for a live pyrogram Str: a str carrying a .html rendering."""
    def __new__(cls, plain, html):
        obj = str.__new__(cls, plain)
        obj._html = html
        return obj

    @property
    def html(self):
        return self._html


def _parser():
    return PostParser(None)


def _message(reply_to, msg_id=1473, chat_id=CHANNEL_ID, service=None, username="univelis",
             chat_type=ChatType.CHANNEL):
    """A post carrying a reply target; id/chat are what the near-own-channel check reads.

    `chat.type` matters: shortening is restricted to CHANNEL feeds, so a live pyrogram enum
    goes in here and the restored-snapshot form (the bare 'CHANNEL' string) is covered by the
    live-vs-cache parity tests.
    """
    return SimpleNamespace(
        id=msg_id,
        chat=SimpleNamespace(id=chat_id, username=username, title="Univelis", type=chat_type),
        service=service,
        reply_to_message=reply_to,
    )


def _legacy_message(reply_to, service=None):
    """The pre-change fixture shape: NO id and NO chat at all (getattr-only contract)."""
    return SimpleNamespace(service=service, reply_to_message=reply_to)


def _reply(**kwargs):
    """A reply target with only the attributes the caller sets."""
    return SimpleNamespace(**kwargs)


def _own_channel_reply(msg_id=1472, text=LONG_TEXT):
    return _reply(id=msg_id, text=text, caption=None, from_user=None,
                  sender_chat=SimpleNamespace(id=CHANNEL_ID, title="Univelis", username="univelis"))


def _renderable_message(reply_to, text="Ответ на соседний пост."):
    """`_message` plus the fields _generate_html_body reads while rendering a whole post."""
    msg = _message(reply_to)
    msg.text = FakeStr(text, text)
    msg.caption = None
    msg.media = None
    msg.forward_origin = None
    return msg


# --------------------------------------------------------------------------- #
# The truncating case: same channel, one id above.
# --------------------------------------------------------------------------- #
def test_neighbouring_own_post_is_truncated():
    out = _parser()._format_reply_info(_message(_own_channel_reply()))

    assert "…" in out
    assert LONG_TEXT.strip() not in out
    assert len(out) < len(LONG_TEXT)
    # The block keeps its markup: the marker line links to the original, the fence still closes.
    assert '<div class="message-reply">--- Reply to ' in out
    assert '<a href="https://t.me/univelis/1472" title="#1472">Univelis (@univelis)</a>' in out
    assert out.endswith(f"<br>{MARKER_QUOTE_END}</div><br>")
    assert out.count(MARKER_QUOTE_END) == 1


def test_truncation_keeps_the_beginning_of_the_quote():
    out = _parser()._format_reply_info(_message(_own_channel_reply()))

    assert "Очень длинный пост канала" in out
    # The cut lands on the CONFIGURED budget, not at some accidental place: count the visible
    # characters of the quote itself (the quote is plain text here, so no markup to discount)
    # and allow only the documented word-boundary back-off of _QUOTE_WORD_BOUNDARY_SHARE.
    budget = post_parser.Config['reply_quote_truncate_chars']
    quote = out[out.index(MARKER_REPLY_OPEN):out.index("…")]
    kept = len(quote[quote.index("<br>") + len("<br>"):])
    assert budget * (1 - post_parser._QUOTE_WORD_BOUNDARY_SHARE) <= kept <= budget


def test_target_without_sender_chat_and_without_from_user_is_own_channel():
    # Last-line heuristic: the target names no chat at all (no chat.id, no sender_chat.id, no
    # from_user). There is no guarantee it lives in this chat — pyrogram can resolve a reply
    # across chats — but an anonymous chat-less target looks like an ordinary channel post
    # rather than a person's message, and the block header still links into this channel.
    reply = _reply(id=1472, text=LONG_TEXT, caption=None, sender_chat=None, from_user=None)
    out = _parser()._format_reply_info(_message(reply))

    assert "…" in out
    assert LONG_TEXT.strip() not in out


# --------------------------------------------------------------------------- #
# Everything else keeps the full quote.
# --------------------------------------------------------------------------- #
def test_distance_beyond_the_threshold_keeps_the_full_quote():
    # 1473 - 1470 = 3 > reply_quote_truncate_distance (2): an older post is not something the
    # reader has just seen, so the answer must still show what it answers.
    out = _parser()._format_reply_info(_message(_own_channel_reply(msg_id=1470)))

    assert "…" not in out
    assert LONG_TEXT.strip() in out


def test_reply_to_another_channel_keeps_the_full_quote():
    reply = _reply(id=1472, text=LONG_TEXT, caption=None, from_user=None,
                   sender_chat=SimpleNamespace(id=-1009999999999, title="Other", username="other"))
    out = _parser()._format_reply_info(_message(reply))

    assert "…" not in out
    assert LONG_TEXT.strip() in out


def test_reply_to_a_user_message_in_a_group_keeps_the_full_quote():
    # A discussion/supergroup feed: the target is a person's message one id above, carrying the
    # SAME chat.id as the post. Shortening it would contradict README and the docstrings, which
    # promise replies to people are never truncated — the chat type is what rules it out.
    reply = _reply(id=99, text=LONG_TEXT, caption=None, sender_chat=None,
                   chat=SimpleNamespace(id=GROUP_ID, title="Chat", username="chat"),
                   from_user=SimpleNamespace(first_name="Bob", last_name=None, username="bob"))
    message = _message(reply, msg_id=100, chat_id=GROUP_ID, username="chat",
                       chat_type=ChatType.SUPERGROUP)
    out = _parser()._format_reply_info(message)

    assert "…" not in out
    assert LONG_TEXT.strip() in out
    assert _parser()._reply_is_near_own_channel(message, reply) is False


def test_group_reply_is_identical_live_and_from_cache():
    # The restored chat type is the bare name string, so the check must agree on both paths.
    reply = SimpleNamespace(
        id=99, text=FakeStr(LONG_TEXT, LONG_TEXT), caption=None, sender_chat=None,
        chat=SimpleNamespace(id=GROUP_ID, title="Chat", username="chat"),
        from_user=SimpleNamespace(id=5, first_name="Bob", last_name=None, username="bob"),
    )
    live_message = _message(reply, msg_id=100, chat_id=GROUP_ID, username="chat",
                            chat_type=ChatType.SUPERGROUP)
    live_out = _parser()._format_reply_info(live_message)
    restored = restore_message(json.loads(json.dumps(snapshot_message(live_message))))

    assert restored.chat.type == "SUPERGROUP"
    assert _parser()._format_reply_info(restored) == live_out
    assert "…" not in live_out


@pytest.mark.parametrize("chat_type,expected", [
    (ChatType.CHANNEL, True),                 # live pyrogram enum
    ("CHANNEL", True),                        # restored snapshot: the bare name string
    ("ChatType.CHANNEL", True),               # str() of the enum, should anyone store it that way
    (ChatType.SUPERGROUP, False),
    ("SUPERGROUP", False),                    # restored group feed
    ("PRIVATE", False),
    (ChatType.GROUP, False),
    (None, False),                            # old fixture / mock: not proven
    (Mock(), False),                          # .name is another Mock — must not raise
])
def test_is_channel_chat_normalises_every_shape(chat_type, expected):
    assert post_parser._is_channel_chat(SimpleNamespace(type=chat_type)) is expected


def test_is_channel_chat_survives_a_chatless_message():
    assert post_parser._is_channel_chat(None) is False
    assert post_parser._is_channel_chat(SimpleNamespace()) is False


def test_a_mock_chat_type_does_not_break_the_render():
    # The whole feature is written not to raise through process_message; a Mock chat type is the
    # only new code path that could, and it must degrade to "not a channel" instead.
    out = _parser()._format_reply_info(_message(_own_channel_reply(), chat_type=Mock()))

    assert "…" not in out
    assert LONG_TEXT.strip() in out


def test_unknown_chat_type_keeps_the_full_quote():
    # A mock or an older fixture without chat.type is "not proven to be a channel": stay with
    # the pre-feature behaviour rather than guessing.
    out = _parser()._format_reply_info(_message(_own_channel_reply(), chat_type=None))

    assert "…" not in out
    assert LONG_TEXT.strip() in out


# --------------------------------------------------------------------------- #
# The target's own chat.id decides "same channel"; sender_chat is only a fallback.
# --------------------------------------------------------------------------- #
def test_signed_channel_post_is_truncated():
    # REGRESSION: a channel with "Sign messages -> Show authors' profiles" gives every post a
    # from_id, so pyrogram fills from_user and leaves sender_chat None
    # (message.py: `sender_chat = ... if not from_user else None`). Deciding on from_user alone
    # switched the whole feature off for such channels — silently, for every post.
    reply = _reply(id=1472, text=LONG_TEXT, caption=None, sender_chat=None,
                   chat=SimpleNamespace(id=CHANNEL_ID, title="Univelis", username="univelis"),
                   from_user=SimpleNamespace(first_name="Admin", last_name=None, username="admin"))
    out = _parser()._format_reply_info(_message(reply))

    assert "…" in out
    assert LONG_TEXT.strip() not in out


def test_target_in_another_chat_keeps_the_full_quote_even_without_from_user():
    # A cross-chat reply (pyrogram resolves it via reply_to.reply_to_peer_id): the target's
    # chat.id differs, so the reader has NOT just scrolled past it — full quote, and chat.id
    # must win over the "no sender_chat and no from_user" heuristic below it.
    reply = _reply(id=1472, text=LONG_TEXT, caption=None, sender_chat=None, from_user=None,
                   chat=SimpleNamespace(id=-1009999999999, title="Other", username="other"))
    out = _parser()._format_reply_info(_message(reply))

    assert "…" not in out
    assert LONG_TEXT.strip() in out


def test_chat_id_wins_over_a_foreign_sender_chat():
    # Both signals present and disagreeing: that is a message posted "as" another channel
    # (send-as-channel / anonymous admin), where pyrogram derives sender_chat from the sender
    # peer while chat stays the chat the message lives in. chat.id is the primary signal, and
    # the link must then point at OUR chat rather than at the sender's.
    reply = _reply(id=1472, text=LONG_TEXT, caption=None, from_user=None,
                   chat=SimpleNamespace(id=CHANNEL_ID, title="Univelis", username="univelis"),
                   sender_chat=SimpleNamespace(id=-1009999999999, title="Other", username="other"))
    out = _parser()._format_reply_info(_message(reply))

    assert "…" in out
    assert 'href="https://t.me/univelis/1472"' in out
    # The LABEL still names the sender (that is who wrote it); only the link is redirected.
    assert 'href="https://t.me/other/' not in out
    assert ">Other (@other)</a>" in out


def test_signed_channel_post_is_identical_live_and_from_cache():
    # The v8 snapshot exists exactly for this: without chat.id the cache hit would decide
    # "foreign chat" and print the full quote where the live render truncates it.
    reply = SimpleNamespace(
        id=1472,
        text=FakeStr(LONG_TEXT, LONG_TEXT),
        caption=None,
        chat=SimpleNamespace(id=CHANNEL_ID, title="Univelis", username="univelis"),
        sender_chat=None,
        from_user=SimpleNamespace(id=5, first_name="Admin", last_name=None, username="admin"),
    )
    live_message = _message(reply)
    live_out = _parser()._format_reply_info(live_message)

    restored = restore_message(json.loads(json.dumps(snapshot_message(live_message))))
    cached_out = _parser()._format_reply_info(restored)

    assert cached_out == live_out
    assert "…" in cached_out


# --------------------------------------------------------------------------- #
# A shortened quote must ALWAYS keep a way back to the original: the reader now
# sees less than the full quote gave them, so the header link is not optional.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("username,expected", [
    ("univelis", "https://t.me/univelis/1472"),          # public channel
    (None, "https://t.me/c/1287848294/1472"),            # private channel: the t.me/c/ form
])
def test_truncated_block_always_links_to_the_original(username, expected):
    out = _parser()._format_reply_info(_message(_own_channel_reply(), username=username))

    assert "…" in out
    assert f'<a href="{expected}" title="#1472">' in out


def test_truncated_signed_post_links_to_the_original():
    # The shape _reply_target_url cannot serve: sender_chat is None because the channel signs
    # posts with the authors' profiles, so before the fix the block had NO link at all and the
    # reader was left with 200 characters and no way to reach the rest.
    reply = _reply(id=1472, text=LONG_TEXT, caption=None, sender_chat=None,
                   chat=SimpleNamespace(id=CHANNEL_ID, title="Univelis", username="univelis"),
                   from_user=SimpleNamespace(first_name="Admin", last_name=None, username="admin"))
    out = _parser()._format_reply_info(_message(reply))

    assert "…" in out
    assert '<a href="https://t.me/univelis/1472" title="#1472">Admin (@admin)</a>' in out


def test_signed_post_in_a_private_channel_links_via_the_c_form():
    reply = _reply(id=1472, text=LONG_TEXT, caption=None, sender_chat=None,
                   chat=SimpleNamespace(id=CHANNEL_ID, title="Univelis", username=None),
                   from_user=SimpleNamespace(first_name="Admin", last_name=None, username="admin"))
    out = _parser()._format_reply_info(_message(reply, username=None))

    assert "…" in out
    assert '<a href="https://t.me/c/1287848294/1472" title="#1472">' in out


def test_short_quote_of_a_neighbouring_post_still_gets_the_own_chat_link():
    # The override follows the near-own-channel DECISION, not the presence of an ellipsis: a
    # quote that fits the budget is printed whole and still gets the proven-origin link. Without
    # this, a signed channel would link only its long quotes and drop the link on short ones.
    short = "Коротко."
    reply = _reply(id=1472, text=short, caption=None, sender_chat=None,
                   chat=SimpleNamespace(id=CHANNEL_ID, title="Univelis", username="univelis"),
                   from_user=SimpleNamespace(first_name="Admin", last_name=None, username="admin"))
    out = _parser()._format_reply_info(_message(reply))

    assert "…" not in out
    assert short in out
    assert ('<div class="message-reply">--- Reply to '
            '<a href="https://t.me/univelis/1472" title="#1472">Admin (@admin)</a> ---<br>'
            f'{short}<br>{MARKER_QUOTE_END}</div><br>') == out


def test_media_only_neighbouring_target_still_gets_the_own_chat_link():
    # No text and no caption -> no quote at all, so the block is the opening marker alone. The
    # link is the ONLY thing that block carries, which makes it more important here, not less.
    reply = _reply(id=1472, text=None, caption=None, sender_chat=None,
                   chat=SimpleNamespace(id=CHANNEL_ID, title="Univelis", username="univelis"),
                   from_user=SimpleNamespace(first_name="Admin", last_name=None, username="admin"))
    out = _parser()._format_reply_info(_message(reply))

    assert MARKER_QUOTE_END not in out
    assert ('<div class="message-reply">--- Reply to '
            '<a href="https://t.me/univelis/1472" title="#1472">Admin (@admin)</a> ---</div><br>') == out


def test_link_prefers_an_active_collectible_username():
    # pyrogram fills chat.username with `channel.username or channel.usernames[0].username`
    # WITHOUT checking `active`, so reading the attribute would link to a released name. The
    # footer already resolves this through get_channel_username; the block must not disagree.
    message = _message(_own_channel_reply(), username="oldname")
    message.chat.usernames = [SimpleNamespace(username="oldname", active=False),
                              SimpleNamespace(username="newname", active=True)]
    out = _parser()._format_reply_info(message)

    assert "…" in out
    assert '<a href="https://t.me/newname/1472" title="#1472">' in out
    assert "t.me/oldname" not in out
    # Same answer as the post footer's own resolution.
    assert _parser().get_channel_username(message) == "newname"


def test_link_is_untouched_when_nothing_is_shortened():
    # Byte-for-byte the pre-feature rendering on every non-truncating path: the override is only
    # computed when the quote is actually being cut.
    parser = _parser()
    full = parser._format_reply_info(_message(_own_channel_reply(msg_id=1470)))
    target = parser._format_reply_target(_own_channel_reply(msg_id=1470),
                                         parser._reply_author_label(_own_channel_reply(msg_id=1470)))

    assert "…" not in full
    assert target in full


def test_link_is_untouched_when_truncation_is_disabled(monkeypatch):
    monkeypatch.setitem(post_parser.Config, 'reply_quote_truncate_chars', 0)
    reply = _reply(id=1472, text=LONG_TEXT, caption=None, sender_chat=None,
                   chat=SimpleNamespace(id=CHANNEL_ID, title="Univelis", username="univelis"),
                   from_user=SimpleNamespace(first_name="Admin", last_name=None, username="admin"))
    out = _parser()._format_reply_info(_message(reply))

    # No shortening -> no override -> the old link-less "label, #id" form for a signed post.
    assert "…" not in out
    assert "<a href=" not in out
    assert "Admin (@admin), #1472" in out


def test_reply_to_a_later_post_keeps_the_full_quote():
    # A NEGATIVE distance (the target is below the post — a reply to something posted later,
    # which happens after an edit or in an imported history) is not "the entry just above",
    # so it must not be shortened either.
    out = _parser()._format_reply_info(_message(_own_channel_reply(msg_id=1475)))

    assert "…" not in out
    assert LONG_TEXT.strip() in out


def test_message_without_id_and_chat_keeps_the_full_quote():
    # REGRESSION: the older fixtures (and any snapshot missing the fields) build a message with
    # no id and no chat at all — the check must read them via getattr and simply say "no".
    out = _parser()._format_reply_info(_legacy_message(_own_channel_reply()))

    assert "…" not in out
    assert LONG_TEXT.strip() in out


def test_pinned_branch_is_never_truncated():
    # A pinned message is not a neighbouring feed entry the reader has already scrolled past.
    msg = _message(_own_channel_reply(), service="MessageServiceType.PINNED_MESSAGE")
    out = _parser()._format_reply_info(msg)

    assert "…" not in out
    assert LONG_TEXT.strip() in out


# --------------------------------------------------------------------------- #
# Both settings switch the behaviour off (Config is a module-level dict).
# --------------------------------------------------------------------------- #
def test_zero_chars_disables_truncation(monkeypatch):
    monkeypatch.setitem(post_parser.Config, 'reply_quote_truncate_chars', 0)
    out = _parser()._format_reply_info(_message(_own_channel_reply()))

    assert "…" not in out
    assert LONG_TEXT.strip() in out


def test_zero_distance_disables_truncation(monkeypatch):
    monkeypatch.setitem(post_parser.Config, 'reply_quote_truncate_distance', 0)
    out = _parser()._format_reply_info(_message(_own_channel_reply()))

    assert "…" not in out
    assert LONG_TEXT.strip() in out


def test_wider_distance_setting_is_honoured(monkeypatch):
    monkeypatch.setitem(post_parser.Config, 'reply_quote_truncate_distance', 5)
    out = _parser()._format_reply_info(_message(_own_channel_reply(msg_id=1470)))

    assert "…" in out


# --------------------------------------------------------------------------- #
# The cut never breaks the markup: _extract_flags matches the block on UNSANITIZED
# html, so the fragment must already be well-formed when it leaves the renderer.
# --------------------------------------------------------------------------- #
def test_cut_closes_the_open_entity_tag():
    reply = _own_channel_reply(text=FakeStr("A" * 300, "<b>" + "A" * 300 + "</b>"))
    out = _parser()._format_reply_info(_message(reply))

    assert "…</b>" in out
    assert out.count("<b>") == out.count("</b>") == 1
    # nh3 keeps the content instead of dropping an unbalanced fragment.
    clean = sanitize_html(out)
    assert "<b>" in clean and "…" in clean
    assert "A" * 100 in clean


def test_cut_inside_a_link_keeps_the_href_intact():
    href = "https://example.com/a/very/long/path/that/must/not/be/cut/in/half"
    reply = _own_channel_reply(text=FakeStr("link " + "B" * 300,
                                            f'<a href="{href}">' + "B" * 300 + "</a>"))
    out = _parser()._format_reply_info(_message(reply))

    assert f'<a href="{href}">' in out
    assert "…</a>" in out
    assert sanitize_html(out).count(href) == 1


def test_html_entities_are_never_cut_in_half():
    # An entity is ONE visible character and ends with ';', which is also a trailing-separator
    # character — a naive tail regex would leave a broken '&am' behind.
    for entity in ("&amp;", "&lt;", "&#1234;"):
        reply = _own_channel_reply(text=FakeStr("x" * 300, ("x" + entity) * 200))
        out = _parser()._format_reply_info(_message(reply))

        assert "…" in out, entity
        head = out[:out.index("…")]
        assert "&am" not in head.replace("&amp;", ""), entity
        assert head.count("&") == head.count(";"), entity


def test_truncate_helper_leaves_short_and_disabled_quotes_untouched():
    assert _truncate_quote_html("<b>short</b>", 200) == "<b>short</b>"
    assert _truncate_quote_html("A" * 300, 0) == "A" * 300
    assert _truncate_quote_html("", 200) == ""
    # Malformed markup must pass through instead of raising.
    assert _truncate_quote_html("ab<", 200) == "ab<"
    assert "…" in _truncate_quote_html("<b>a<i>" + "c" * 50, 10)


def test_scan_is_bounded_by_the_budget_not_by_the_quote():
    # The tokenizer stops one visible character past the budget. This file already carries a
    # regression of exactly this class (see the _TRAILING_BR_RE note in post_parser: 235 ms ->
    # 3.6 s), and a purely performance-shaped fix is otherwise pinned by nothing.
    big = "x" * 2_000_000
    started = time.perf_counter()
    out = _truncate_quote_html(big, 200)
    assert time.perf_counter() - started < 0.05
    assert out == "x" * 200 + "…"


def test_exact_budget_boundary():
    # Exactly at the limit: no ellipsis, byte-for-byte the input.
    assert _truncate_quote_html("<b>" + "x" * 200 + "</b>", 200) == "<b>" + "x" * 200 + "</b>"
    # One visible character over: cut at the budget and close the open tag. No word boundary
    # exists in a run of 'x', so the back-off finds nothing and the cut stays at max_chars.
    assert _truncate_quote_html("<b>" + "x" * 201 + "</b>", 200) == "<b>" + "x" * 200 + "…</b>"


def test_truncate_helper_backs_off_to_a_word_boundary():
    out = _truncate_quote_html("one two three four five six seven", 20)

    assert out == "one two three four…"
    # <br> counts as one visible character and is a word boundary too.
    assert _truncate_quote_html("line<br>next<br>third", 6) == "line…"


# --------------------------------------------------------------------------- #
# Raw '<' and '>' in the quoted TEXT: pyrogram escapes only inside entity ranges,
# so plain user text arrives verbatim and must cost visible characters like any
# other. Counting it as markup silently switched truncation off (REGRESSION).
# --------------------------------------------------------------------------- #
def test_raw_angle_brackets_in_the_text_do_not_disable_truncation():
    out = _truncate_quote_html(ANGLE_TEXT, 200)

    assert out != ANGLE_TEXT
    assert out.endswith("…")
    # The '<' is an ordinary character now: it neither eats the rest of the line nor the '>'.
    assert len(out) < len(ANGLE_TEXT)
    assert ANGLE_TEXT.startswith(out[:-1])


def test_raw_angle_brackets_are_truncated_on_the_render_path():
    reply = _own_channel_reply(text=FakeStr(ANGLE_TEXT, ANGLE_TEXT))
    out = _parser()._format_reply_info(_message(reply))

    assert "…" in out
    assert ANGLE_TEXT not in out
    assert "если x &lt; 5" in sanitize_html(out)


def test_a_lone_bracket_still_costs_one_character():
    # '<' with no '>' anywhere used to swallow the whole rest of the quote.
    assert _truncate_quote_html("<" * 40, 10) == "<" * 10 + "…"
    assert _truncate_quote_html("a < b " + "c" * 300, 6) == "a < b…"


def test_pseudo_tag_is_never_closed_as_a_tag():
    # '<Word here>' is prose, not markup: closing it would print a '</word>' the input never
    # contained, and the block is compared to the html body as an EXACT fragment.
    out = _truncate_quote_html("a<Word here>b" + "c" * 300, 10)

    assert "</word>" not in out.lower()
    assert out == "a<Word her…"


def test_cut_inside_a_tg_emoji_span_closes_the_full_tag_name():
    # A hyphenated name ('<tg-emoji emoji-id="...">' for custom emoji, '<tg-time unix="...">'
    # for dates) must go on the stack whole — '</tg>' is unbalanced markup before the sanitizer.
    quote = "a" * 20 + '<tg-emoji emoji-id="5368324170671202286">🎉</tg-emoji>' + "b" * 300
    out = _truncate_quote_html(quote, 21)

    assert out.endswith("…</tg-emoji>")
    assert "</tg>" not in out.replace("</tg-emoji>", "")
    assert out.count("<tg-emoji") == out.count("</tg-emoji>") == 1


# Every start tag pyrogram/kurigram can print, taken from pyrogram/parser/html.py `unparse`
# (bold/italic/underline/strikethrough collapse to their first letter; code and spoiler print
# bare; pre may carry a language; blockquote may carry the VALUELESS 'expandable'; text links
# and mentions are <a href>; custom emoji and dates are the hyphenated tg-* tags). '<a href>'
# also arrives from _add_hyperlinks_to_raw_urls. A tag missed by the tokenizer is charged to
# the visible budget and can be cut in half, which leaves broken markup for _extract_flags.
PYROGRAM_TAGS = [
    ("<b>", "</b>"), ("<i>", "</i>"), ("<u>", "</u>"), ("<s>", "</s>"),
    ("<code>", "</code>"), ("<pre>", "</pre>"), ('<pre language="python">', "</pre>"),
    ("<blockquote>", "</blockquote>"), ("<blockquote expandable>", "</blockquote>"),
    ("<spoiler>", "</spoiler>"),
    ('<a href="https://example.com/x">', "</a>"), ('<a href="tg://user?id=7">', "</a>"),
    ('<tg-emoji emoji-id="1">', "</tg-emoji>"),
    ('<tg-time unix="7">', "</tg-time>"), ('<tg-time unix="7" format="dd.MM">', "</tg-time>"),
]


@pytest.mark.parametrize("open_tag,close_tag", PYROGRAM_TAGS)
def test_every_pyrogram_tag_is_closed_after_the_cut(open_tag, close_tag):
    out = _truncate_quote_html(open_tag + "x" * 400 + close_tag, 200)

    assert out.endswith("…" + close_tag)
    # The tag itself is markup: it costs nothing from the visible budget.
    assert out.startswith(open_tag)
    assert out[len(open_tag):-len("…" + close_tag)] == "x" * 200


# Only these runs can reach the truncator: _format_reply_quote strips leading ' \t\r\n' and
# leading <br> from the quote before calling it, so a run of ASCII spaces or line breaks is
# already gone. NBSP survives on purpose (a leading NBSP is a common hand-made indent).
REACHABLE_SEPARATOR_RUNS = [("\u00a0", "nbsp"), ("-", "dashes"), (".", "dots")]


@pytest.mark.parametrize("run,label", REACHABLE_SEPARATOR_RUNS)
def test_a_leading_run_of_separators_does_not_eat_the_whole_budget(run, label):
    # The tail-trim walk is floored where the word-boundary search stops. Unbounded, a quote
    # opening with a long run of [\s.,;:!?-] (a hand-made NBSP indent, an ASCII rule) handed the
    # ENTIRE budget back and rendered as a bare '…'. The floor is asserted EXACTLY rather than as
    # a ">= n" on the string length: everything kept here is separators, so a loose bound would
    # stay green on an output that is a bare ellipsis to the reader.
    budget = 200
    floor = max(1, budget - max(1, int(budget * post_parser._QUOTE_WORD_BOUNDARY_SHARE))) - 1
    quote = run * 250 + "текст"
    out = _truncate_quote_html(quote, budget)

    assert out == run * floor + "…", label


@pytest.mark.parametrize("run,label", REACHABLE_SEPARATOR_RUNS)
def test_readable_text_survives_a_leading_separator_run(run, label):
    # The property a reader actually cares about: with an indent shorter than the budget, real
    # words reach the output instead of the quote collapsing into separators plus '…'.
    quote = run * 50 + "слово тест " * 60
    out = _truncate_quote_html(quote, 200)

    assert out.endswith("…"), label
    assert "слово тест" in out, label
    assert quote.startswith(out[:-1]), label


def test_tg_emoji_survives_the_render_path():
    text = "🎉" + LONG_TEXT
    quote_html = '<tg-emoji emoji-id="5368324170671202286">🎉</tg-emoji>' + LONG_TEXT
    out = _parser()._format_reply_info(_message(_own_channel_reply(text=FakeStr(text, quote_html))))

    assert "…" in out
    assert "</tg>" not in out.replace("</tg-emoji>", "")


# --------------------------------------------------------------------------- #
# Live target and cache-restored target render IDENTICALLY (the cache stores the
# full quote; the cut is applied by the renderer on both paths).
# --------------------------------------------------------------------------- #
def test_truncated_block_is_identical_live_and_from_cache():
    reply = SimpleNamespace(
        id=1472,
        text=FakeStr(LONG_TEXT, LONG_TEXT.replace("читатель", "<b>читатель</b>")),
        caption=None,
        sender_chat=SimpleNamespace(id=CHANNEL_ID, title="Univelis", username="univelis"),
        from_user=None,
    )
    live_message = _message(reply)
    live_out = _parser()._format_reply_info(live_message)

    # tg_cache stores the snapshot as JSON, so the round trip has to go through it as well:
    # only what json.dumps/json.loads preserves is what a cache hit will actually render.
    restored = restore_message(json.loads(json.dumps(snapshot_message(live_message))))
    cached_out = _parser()._format_reply_info(restored)

    assert cached_out == live_out
    assert "…" in cached_out
    assert cached_out.endswith(f"<br>{MARKER_QUOTE_END}</div><br>")


# --------------------------------------------------------------------------- #
# _extract_flags strips the reply block from the html body as an EXACT fragment,
# which only works while the renderer is deterministic and emits the very same
# string both times it is called.
# --------------------------------------------------------------------------- #
def test_truncated_block_is_deterministic_and_strippable_from_the_body():
    parser = _parser()
    message = _renderable_message(_own_channel_reply())

    block = parser._format_reply_info(message)
    assert parser._format_reply_info(message) == block
    assert "…" in block

    body = parser._generate_html_body(message)
    assert block in body
    stripped = body.replace(block, '', 1)
    # The block is gone WHOLE: no orphaned marker, fence or quote text stays behind to leak
    # into mention / link / foreign_channel detection.
    assert MARKER_QUOTE_END not in stripped
    assert "--- Reply to " not in stripped
    assert "…" not in stripped
    assert len(stripped) == len(body) - len(block)
