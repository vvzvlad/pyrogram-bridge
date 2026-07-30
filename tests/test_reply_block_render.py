# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""Reply-block rendering (post_parser._format_reply_info).

The reply block must name the AUTHOR of the quoted message (channel title / user
name) instead of a bare "#<id>", and must quote the target IN FULL (the former
100-char cut is gone). Reply targets reach the renderer either as live pyrogram
Messages or as restored cache snapshots (SimpleNamespace), so every attribute is
read with getattr — the fixtures below deliberately omit attributes.

The quote is fenced by TEXT marker lines in the style of the forward block
('--- Reply to X ---' / '--- End of quote ---'), so a reader can see where somebody
else's words end and the post's own text begins. CSS cannot do this job —
sanitizer.py keeps only 5 sizing properties on `style` and the project ships no
stylesheet — so the delimiter must be text that also survives a reader's HTML→text
conversion.
"""
from types import SimpleNamespace

from message_snapshot import snapshot_message, restore_message
from post_parser import MARKER_QUOTE_END, PostParser
from sanitizer import sanitize_html


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


def _message(reply_to, service=None):
    return SimpleNamespace(service=service, reply_to_message=reply_to)


def _reply(**kwargs):
    """A reply target with only the attributes the caller sets (getattr-only contract)."""
    return SimpleNamespace(**kwargs)


def _post_with_reply(own_text, reply_to):
    """A message complete enough for _generate_html_body / _extract_flags.

    SimpleNamespace, not MagicMock(spec=Message): a spec'd mock hands back a TRUTHY mock
    for every attribute the renderer touches but the test never set, so the rendered body
    and the flag set would change silently whenever kurigram grows a field (e.g. the
    rich_message of the 2.2.24 pin). Every attribute here is explicit.
    """
    return SimpleNamespace(
        id=123,
        chat=SimpleNamespace(id=-1001234567890, username="test_channel", title="Test Chan"),
        text=FakeStr(own_text, own_text),
        caption=None,
        media=None,
        poll=None,
        web_page=None,
        forward_origin=None,
        service=None,
        reactions=None,
        show_caption_above_media=False,
        reply_to_message=reply_to,
    )


# --------------------------------------------------------------------------- #
# The link text is the channel NAME, not "#<id>".
# --------------------------------------------------------------------------- #
def test_public_channel_renders_name_and_link():
    reply = _reply(id=4145, text="quoted", caption=None,
                   sender_chat=SimpleNamespace(id=-1001234567890, title="Src Chan", username="srcchan"))
    out = _parser()._format_reply_info(_message(reply))

    assert '<a href="https://t.me/srcchan/4145" title="#4145">Src Chan (@srcchan)</a>' in out
    assert "Reply to " in out
    assert '<div class="message-reply">' in out
    # The id is only a tooltip now — it must not appear as visible link text.
    assert ">#4145<" not in out


def test_private_channel_uses_tme_c_link():
    reply = _reply(id=51, text="quoted", caption=None,
                   sender_chat=SimpleNamespace(id=-1001234567890, title="Private Chan", username=None))
    out = _parser()._format_reply_info(_message(reply))

    assert '<a href="https://t.me/c/1234567890/51" title="#51">Private Chan</a>' in out


def test_from_user_label_without_link_keeps_the_id_visible():
    # No link to hang a tooltip on, so the target id stays in the visible text (as the
    # pre-change block did) — the name is what identifies the author, the id is the locator.
    reply = _reply(id=52, text="quoted", caption=None, sender_chat=None,
                   from_user=SimpleNamespace(first_name="John", last_name="Doe", username="johndoe"))
    out = _parser()._format_reply_info(_message(reply))

    assert "--- Reply to John Doe (@johndoe), #52 ---" in out
    assert "<a href=" not in out


def test_id_fallback_when_no_author():
    # The stage-3 fixtures build reply targets WITHOUT a from_user attribute at all.
    reply = _reply(id=53, text="quoted", caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert "--- Reply to #53 ---" in out


def test_unknown_author_when_nothing_identifies_the_target():
    reply = _reply(text="quoted", caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert "--- Reply to Unknown author ---" in out


# --------------------------------------------------------------------------- #
# The quote is FULL and multi-line — no 100-char truncation.
# --------------------------------------------------------------------------- #
def test_quote_is_not_truncated_and_keeps_line_breaks():
    long_text = ("A" * 250) + "\n" + ("B" * 250)
    reply = _reply(id=54, text=long_text, caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert "A" * 250 in out
    assert "B" * 250 in out
    assert "..." not in out
    assert ("A" * 250) + "<br>" + ("B" * 250) in out
    # The whole quote sits between the two markers.
    assert out.endswith("<br>--- End of quote ---</div><br>")


def test_plain_text_quote_is_html_escaped():
    reply = _reply(id=55, text='<script>alert("x")</script> & co', caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp; co" in out


def test_entity_html_is_preserved_for_str_like_values():
    reply = _reply(id=56, text=FakeStr("bold quote", "<b>bold</b> quote"), caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert "<b>bold</b> quote" in out


def test_caption_used_when_text_is_absent():
    reply = _reply(id=57, text=None, caption="captioned quote", sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert "captioned quote" in out


def test_channel_title_is_escaped():
    reply = _reply(id=58, text="quoted", caption=None,
                   sender_chat=SimpleNamespace(id=-1001, title="A & B <Ltd>", username=None))
    out = _parser()._format_reply_info(_message(reply))

    assert "A &amp; B &lt;Ltd&gt;" in out
    assert "<Ltd>" not in out


# --------------------------------------------------------------------------- #
# Empty quote / pinned branch / no reply.
# --------------------------------------------------------------------------- #
def test_empty_quote_renders_the_opening_marker_only():
    # A media-only target has nothing to quote: the opening marker stands alone, with no
    # end marker dangling under an empty quote (exact markup).
    reply = _reply(id=59, text=None, caption=None,
                   sender_chat=SimpleNamespace(id=-1001, title="Src Chan", username="srcchan"))
    out = _parser()._format_reply_info(_message(reply))

    assert out == ('<div class="message-reply">--- Reply to '
                   '<a href="https://t.me/srcchan/59" title="#59">Src Chan (@srcchan)</a> ---</div><br>')
    assert "End of quote" not in out


def test_pinned_branch_quotes_in_full_and_escapes():
    long_text = "P" * 300 + " <b>"
    reply = _reply(id=60, text=long_text, caption=None, sender_chat=None)
    msg = _message(reply, service="MessageServiceType.PINNED_MESSAGE")
    out = _parser()._format_reply_info(msg)

    # The pinned block names its target too (same construction as the reply branch), so
    # the marker says more than the '📌 Pinned message' post title already does. Only the
    # id is known here, and it is the PINNED MESSAGE's own id — so no "from", which would
    # read as "from author #60".
    assert out.startswith('<div class="message-pinned">--- Pinned message #60 ---<br>')
    assert "from #60" not in out
    assert "P" * 300 in out
    assert "&lt;b&gt;" in out
    assert out.endswith("<br>--- End of quote ---</div><br>")


def test_pinned_branch_names_a_linked_target_like_the_reply_branch():
    # A real name/username IS an author, so the "from" form applies here.
    reply = _reply(id=70, text="pinned text", caption=None,
                   sender_chat=SimpleNamespace(id=-1001234567890, title="Src Chan", username="srcchan"))
    out = _parser()._format_reply_info(_message(reply, service="MessageServiceType.PINNED_MESSAGE"))

    assert out == ('<div class="message-pinned">--- Pinned message from '
                   '<a href="https://t.me/srcchan/70" title="#70">Src Chan (@srcchan)</a> ---<br>'
                   'pinned text<br>--- End of quote ---</div><br>')


def test_pinned_branch_falls_back_to_the_bare_marker_when_unnameable():
    # Nothing identifies the target (no sender_chat, no from_user, no id) — 'Unknown author'
    # would add nothing to the marker.
    reply = _reply(text="pinned text", caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply, service="MessageServiceType.PINNED_MESSAGE"))

    assert out == ('<div class="message-pinned">--- Pinned message ---<br>'
                   'pinned text<br>--- End of quote ---</div><br>')


def test_pinned_branch_with_empty_quote_has_no_end_marker():
    reply = _reply(id=69, text=None, caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply, service="MessageServiceType.PINNED_MESSAGE"))

    assert out == '<div class="message-pinned">--- Pinned message #69 ---</div><br>'


def test_no_reply_returns_none():
    assert _parser()._format_reply_info(_message(None)) is None


# --------------------------------------------------------------------------- #
# Live target and cache-restored target render IDENTICALLY (v5 snapshot).
# --------------------------------------------------------------------------- #
def test_restored_plain_text_target_renders_identically():
    # A plain str has no .html: the renderer escapes it, so the snapshot must store the
    # ESCAPED html too — otherwise a cache hit would emit raw markup.
    reply = SimpleNamespace(
        id=4146,
        text='<b>x</b> & "y"',
        caption=None,
        sender_chat=SimpleNamespace(id=-1001234567890, title="Src Chan", username="srcchan"),
        from_user=None,
    )
    live_out = _parser()._format_reply_info(_message(reply))

    restored = restore_message(snapshot_message(SimpleNamespace(id=2, reply_to_message=reply)))
    cached_out = _parser()._format_reply_info(_message(restored.reply_to_message))

    assert cached_out == live_out
    assert "&lt;b&gt;x&lt;/b&gt;" in live_out
    assert "<b>x</b>" not in live_out
    # The markers are added by the renderer, so both paths carry them.
    assert cached_out.endswith("<br>--- End of quote ---</div><br>")


def test_reply_block_survives_the_sanitizer_boundary():
    # The block is sanitized at the output boundary; div/a/br are allowlisted (blockquote,
    # pre and code are NOT — hence the plain div + <br> markup used here). The markers are
    # plain text, so nh3 passes them through untouched — that is exactly why they were
    # chosen over any CSS-based delimiter.
    reply = _reply(id=61, text="first line\nsecond line", caption=None,
                   sender_chat=SimpleNamespace(id=-1001234567890, title="Src Chan", username="srcchan"))
    out = _parser()._format_reply_info(_message(reply))
    clean = sanitize_html(out)

    assert '<div class="message-reply">' in clean
    assert '<a href="https://t.me/srcchan/61" title="#61">Src Chan (@srcchan)</a>' in clean
    assert "--- Reply to " in clean
    assert "---<br>first line<br>second line<br>--- End of quote ---" in clean


def test_restored_snapshot_renders_same_block_as_live_target():
    reply = SimpleNamespace(
        id=4145,
        text=FakeStr("A" * 200 + "\nsecond line", "A" * 200 + "\nsecond <b>line</b>"),
        caption=None,
        sender_chat=SimpleNamespace(id=-1001234567890, title="Src Chan", username="srcchan"),
        from_user=None,
    )
    live_out = _parser()._format_reply_info(_message(reply))

    restored = restore_message(snapshot_message(SimpleNamespace(id=1, reply_to_message=reply)))
    cached_out = _parser()._format_reply_info(_message(restored.reply_to_message))

    assert cached_out == live_out
    # Byte-identical INCLUDING the fenced multi-line quote.
    assert ("A" * 200) + "<br>second <b>line</b><br>--- End of quote ---" in cached_out


# --------------------------------------------------------------------------- #
# Marker lines fence the quote, in the style of the forward block. Without them a
# reader cannot tell where somebody else's words end and the post's own text begins.
# --------------------------------------------------------------------------- #
def test_quote_is_fenced_by_marker_lines():
    reply = _reply(id=62, text="one\ntwo\nthree", caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert out == ('<div class="message-reply">--- Reply to #62 ---<br>'
                   'one<br>two<br>three<br>--- End of quote ---</div><br>')
    # The block's own fence is the last line and the only one here.
    assert out.count(MARKER_QUOTE_END) == 1


def test_marker_markup_with_link_target():
    reply = _reply(id=65, text="quoted", caption=None,
                   sender_chat=SimpleNamespace(id=-1001234567890, title="Src Chan", username="srcchan"))
    out = _parser()._format_reply_info(_message(reply))

    assert out == ('<div class="message-reply">--- Reply to '
                   '<a href="https://t.me/srcchan/65" title="#65">Src Chan (@srcchan)</a> ---<br>'
                   'quoted<br>--- End of quote ---</div><br>')


def test_quoted_marker_literal_is_rendered_verbatim():
    # ACCEPTED, DELIBERATE limitation: the quote is somebody else's text and a marker-looking
    # line inside it is rendered as-is — exactly like the forward block, which has always
    # tolerated a quoted '--- Forwarded post end ---'. The cost is cosmetic attribution
    # ambiguity; nothing here reaches flag detection (the whole block is removed as an exact
    # fragment first) or the sanitizer. Pinned on purpose, so a future change shows as a diff.
    reply = _reply(id=71, caption=None, sender_chat=None,
                   text="innocent line\n--- End of quote ---\nnow I speak as the channel")
    out = _parser()._format_reply_info(_message(reply))

    assert "innocent line<br>--- End of quote ---<br>now I speak as the channel" in out
    assert out.count(MARKER_QUOTE_END) == 2          # the quoted one plus the block's own
    assert out.endswith(f"<br>{MARKER_QUOTE_END}</div><br>")


def test_trailing_newline_does_not_pad_the_closing_marker():
    reply = _reply(id=73, text="line one\nline two\n\n", caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert out == ('<div class="message-reply">--- Reply to #73 ---<br>'
                   'line one<br>line two<br>--- End of quote ---</div><br>')


def test_whitespace_only_quote_counts_as_empty():
    # A quote of only blank lines would otherwise render an empty frame: '\n' becomes the
    # truthy string '<br>'. Emptiness is decided on the PLAIN value, so the html a pyrogram Str
    # would report for the same text (entity markup around the blanks) changes nothing.
    for blank in ("\n", "   \n  \n", "  ", "\u00a0"):
        out = _parser()._format_reply_info(_message(_reply(id=74, text=blank, caption=None, sender_chat=None)))
        assert out == '<div class="message-reply">--- Reply to #74 ---</div><br>', repr(blank)
    for plain, blank_html in (("\n", "<b>\n</b>"), (" ", "<b> </b>"),
                              (" ", "<i><b> </b></i>"), ("\n", "<b></b><br><i></i>")):
        reply = _reply(id=74, caption=None, sender_chat=None, text=FakeStr(plain, blank_html))
        out = _parser()._format_reply_info(_message(reply))
        assert out == '<div class="message-reply">--- Reply to #74 ---</div><br>', blank_html


def test_angle_bracket_text_is_quoted_not_swallowed():
    # REGRESSION: emptiness used to be decided on a TAG-STRIPPED view of the html. A message
    # without entities has .html == the raw text, so a quote whose whole text is angle-bracketed
    # ('<без комментариев>', '<3>', '<-- x -->') looked like markup and the whole quote was
    # erased — while nh3 escapes it at the boundary and a reader sees it fine.
    for text in ("<без комментариев>", "<3>", "<...>", "<-- x -->"):
        reply = _reply(id=96, caption=None, sender_chat=None, text=FakeStr(text, text))
        out = _parser()._format_reply_info(_message(reply))

        assert out == ('<div class="message-reply">--- Reply to #96 ---<br>'
                       f'{text}<br>--- End of quote ---</div><br>'), text
        # The sanitizer escapes it into visible text rather than dropping it.
        clean = sanitize_html(out)
        assert text.replace("<", "&lt;").replace(">", "&gt;") in clean, text


def test_leading_blank_lines_are_trimmed_too():
    # Mirror of the trailing trim: no empty line between the opening marker and the quote.
    reply = _reply(id=97, text="\n\n\nquoted", caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert out == ('<div class="message-reply">--- Reply to #97 ---<br>'
                   'quoted<br>--- End of quote ---</div><br>')


def test_br_variants_are_trimmed_at_both_ends():
    # nh3 renders '<BR>', '<br/>' and '<br >' as real line breaks, so they pad the fences just
    # like '<br>' and must be trimmed too.
    reply = _reply(id=98, caption=None, sender_chat=None,
                   text=FakeStr("quoted", "<BR><br /> quoted <br/> <br >"))
    out = _parser()._format_reply_info(_message(reply))

    assert out == ('<div class="message-reply">--- Reply to #98 ---<br>'
                   'quoted<br>--- End of quote ---</div><br>')


def test_br_inside_an_entity_span_is_a_known_remainder():
    # KNOWN REMAINDER, pinned on purpose: the trim patterns need the <br> at the very edge, so
    # one tucked inside an entity span — or carrying attributes — still pads a fence. Tag-aware
    # trimming is not worth it; this test exists so a change in that area shows up as a diff.
    for plain, quote_html in (("bold", "<b>bold<br></b>"),
                              ("bold", "<b><br>bold</b>"),
                              ("bold", "bold<br class=x>")):
        reply = _reply(id=100, caption=None, sender_chat=None, text=FakeStr(plain, quote_html))
        out = _parser()._format_reply_info(_message(reply))

        assert out == ('<div class="message-reply">--- Reply to #100 ---<br>'
                       f'{quote_html}<br>--- End of quote ---</div><br>'), quote_html


def test_edge_nbsp_is_kept_as_a_hand_made_indent():
    # NBSP is the one whitespace character HTML still renders, and a leading NBSP is a common
    # hand-made indent, so the trim strips only ' \t\r\n' and leaves NBSP in place.
    reply = _reply(id=101, text="\u00a0\u00a0indented quote\u00a0", caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert out == ('<div class="message-reply">--- Reply to #101 ---<br>'
                   '\u00a0\u00a0indented quote\u00a0<br>--- End of quote ---</div><br>')


def test_pinned_target_titled_like_the_fallback_keeps_its_name_and_link():
    # REGRESSION: the pinned wording used to be chosen by comparing the rendered label with the
    # fallback TEXT, so a channel actually titled 'Unknown author' lost both its name and its
    # t.me/c link. The decision comes from the source fields now.
    reply = _reply(id=99, text="pinned body", caption=None,
                   sender_chat=SimpleNamespace(id=-1001234567890, title="Unknown author", username=None))
    out = _parser()._format_reply_info(_message(reply, service="MessageServiceType.PINNED_MESSAGE"))

    assert out == ('<div class="message-pinned">--- Pinned message from '
                   '<a href="https://t.me/c/1234567890/99" title="#99">Unknown author</a> ---<br>'
                   'pinned body<br>--- End of quote ---</div><br>')


def test_blank_line_inside_the_quote_stays_inside_the_markers():
    # The blank line is where the old markup lost the boundary; now the end marker is
    # what closes the quote, so an inner empty line changes nothing.
    reply = _reply(id=63, text="first\n\nlast", caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert "---<br>first<br><br>last<br>--- End of quote ---" in out
    assert out.count("--- End of quote ---") == 1


def test_post_own_text_stays_outside_the_markers():
    # The whole point of the markers: the post's OWN text starts only AFTER the closing
    # marker, so the boundary is unambiguous across the blank line between the blocks.
    reply = _reply(id=67, text="quoted line one\nquoted line two", caption=None, sender_chat=None)
    body = _parser()._generate_html_body(_post_with_reply("my own line one\nmy own line two", reply))

    assert "quoted line one<br>quoted line two<br>--- End of quote ---</div>" in body
    assert "my own line one<br>my own line two" in body
    # The marker block is closed before the post's own text begins.
    assert body.index("--- End of quote ---") < body.index("my own line one")
    assert body.count("--- End of quote ---") == 1


def test_marker_block_does_not_reach_flag_detection():
    # Flag detection strips the reply block as an EXACT fragment, markers included, so it
    # cannot change the public exclude_flags result. The quote carries one of EVERY kind of
    # content the link/mention/channel detectors look for, so none of the asserts is vacuous.
    parser = _parser()
    parser.get_channel_username = lambda message: "test_channel"
    own_text = "Обычный пост без ссылок и упоминаний"
    reply = _reply(id=68, caption=None, sender_chat=None, from_user=None,
                   text="Quoted: @somechannel https://example.com\n"
                        "https://t.me/otherchannel https://t.me/+secret")

    baseline = parser._extract_flags(_post_with_reply(own_text, None))
    with_reply = parser._extract_flags(_post_with_reply(own_text, reply))

    assert sorted(with_reply) == sorted(baseline)
    for leaked in ("mention", "link", "foreign_channel", "hid_channel"):
        assert leaked not in with_reply
    # Sanity: the same content DOES raise those flags when it is the post's own text.
    own_flags = parser._extract_flags(_post_with_reply(str(reply.text), None))
    for raised in ("mention", "link", "foreign_channel", "hid_channel"):
        assert raised in own_flags
