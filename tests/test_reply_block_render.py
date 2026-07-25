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
"""
from types import SimpleNamespace

from message_snapshot import snapshot_message, restore_message
from post_parser import PostParser
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

    assert "Reply to John Doe (@johndoe), #52:" in out
    assert "<a href=" not in out


def test_id_fallback_when_no_author():
    # The stage-3 fixtures build reply targets WITHOUT a from_user attribute at all.
    reply = _reply(id=53, text="quoted", caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert "Reply to #53:" in out


def test_unknown_author_when_nothing_identifies_the_target():
    reply = _reply(text="quoted", caption=None, sender_chat=None)
    out = _parser()._format_reply_info(_message(reply))

    assert "Reply to Unknown author:" in out


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
def test_empty_quote_has_no_dangling_colon():
    reply = _reply(id=59, text=None, caption=None,
                   sender_chat=SimpleNamespace(id=-1001, title="Src Chan", username="srcchan"))
    out = _parser()._format_reply_info(_message(reply))

    assert out == ('<div class="message-reply">Reply to '
                   '<a href="https://t.me/srcchan/59" title="#59">Src Chan (@srcchan)</a></div><br>')


def test_pinned_branch_quotes_in_full_and_escapes():
    long_text = "P" * 300 + " <b>"
    reply = _reply(id=60, text=long_text, caption=None, sender_chat=None)
    msg = _message(reply, service="MessageServiceType.PINNED_MESSAGE")
    out = _parser()._format_reply_info(msg)

    assert '<div class="message-pinned">Pinned: ' in out
    assert "P" * 300 in out
    assert "&lt;b&gt;" in out


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


def test_reply_block_survives_the_sanitizer_boundary():
    # The block is sanitized at the output boundary; div/a/br are allowlisted (blockquote,
    # pre and code are NOT — hence the plain div + <br> markup used here).
    reply = _reply(id=61, text="first line\nsecond line", caption=None,
                   sender_chat=SimpleNamespace(id=-1001234567890, title="Src Chan", username="srcchan"))
    out = _parser()._format_reply_info(_message(reply))
    clean = sanitize_html(out)

    assert '<div class="message-reply">' in clean
    assert '<a href="https://t.me/srcchan/61" title="#61">Src Chan (@srcchan)</a>' in clean
    assert "first line<br>second line" in clean
    assert "Reply to " in clean


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
