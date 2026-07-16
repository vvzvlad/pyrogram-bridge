# flake8: noqa
# pylint: disable=missing-function-docstring, redefined-outer-name, line-too-long
"""Unit tests for the single project-wide bleach config (sanitizer.py, issue #28 / §3).

Covers the stage-1 registry items that this module owns:
  §3.1  s / del (strikethrough) survive.
  §3.2  fail-closed: on a bleach error the fragment is html.escape()d, never returned raw.
  §3.16 the single error-log name `html_sanitization_error` + log_context.
Plus the two non-default bleach params the maintainer flagged as load-bearing:
  strip=True (disallowed tags are REMOVED, not escaped into visible text) and
  protocols including 'tg' (tg:// links survive).
And the one-config invariant: only one ALLOWED_TAGS list exists in the repo.
"""
import logging

import pytest

import sanitizer
from sanitizer import sanitize_html, ALLOWED_TAGS, ALLOWED_PROTOCOLS


# --------------------------------------------------------------------------- #
# §3.1 — strikethrough survives (the drift the single config fixes).
# --------------------------------------------------------------------------- #
def test_strikethrough_s_survives():
    assert sanitize_html("<s>gone</s>") == "<s>gone</s>"


def test_strikethrough_del_survives():
    assert sanitize_html("<del>removed</del>") == "<del>removed</del>"


def test_s_and_del_in_allowed_tags():
    assert "s" in ALLOWED_TAGS
    assert "del" in ALLOWED_TAGS


# --------------------------------------------------------------------------- #
# strip=True — disallowed tags are REMOVED (bleach's default False would escape
# them into visible text). Both the tag and its dangerous attrs must vanish.
# --------------------------------------------------------------------------- #
def test_script_is_stripped_not_escaped():
    out = sanitize_html("<b>hi</b><script>alert(1)</script>")
    assert out == "<b>hi</b>alert(1)"
    # strip=True removed the tag entirely — it was NOT escaped into visible &lt;script&gt;.
    assert "&lt;script&gt;" not in out
    assert "<script" not in out


def test_onerror_attribute_is_stripped():
    out = sanitize_html('<img src="http://e/x.png" onerror="alert(1)">')
    assert "onerror" not in out
    assert "alert(1)" not in out


def test_javascript_protocol_href_dropped():
    out = sanitize_html('<a href="javascript:alert(1)">x</a>')
    # The <a> tag survives (whitelisted) but the dangerous href is dropped by the protocol filter.
    assert "javascript:" not in out


def test_svg_onload_is_stripped():
    # <svg> is not whitelisted -> removed; the onload handler must not survive anywhere.
    out = sanitize_html('<p>hi</p><svg onload=alert(1)></svg>')
    assert "svg" not in out.lower()
    assert "onload" not in out
    assert "alert" not in out


def test_data_uri_href_dropped():
    out = sanitize_html('<a href="data:text/html,<script>alert(1)</script>">x</a>')
    assert "data:" not in out
    assert "<script" not in out


def test_disallowed_tag_removed_not_escaped():
    # nh3 strips (like bleach strip=True): the tag vanishes, it is NOT escaped into
    # visible &lt;iframe&gt; text.
    out = sanitize_html('<iframe src="http://e"></iframe><p>ok</p>')
    assert out == "<p>ok</p>"
    assert "&lt;iframe" not in out
    assert "<iframe" not in out


# --------------------------------------------------------------------------- #
# protocols — non-default 'tg' keeps tg:// links (channel footers use them).
# --------------------------------------------------------------------------- #
def test_tg_protocol_href_survives():
    out = sanitize_html('<a href="tg://resolve?domain=x">x</a>')
    assert 'href="tg://resolve?domain=x"' in out


def test_http_and_https_survive():
    assert 'href="https://e/x"' in sanitize_html('<a href="https://e/x">x</a>')
    assert 'href="http://e/x"' in sanitize_html('<a href="http://e/x">x</a>')


def test_tg_in_allowed_protocols():
    assert ALLOWED_PROTOCOLS == ['http', 'https', 'tg']


# --------------------------------------------------------------------------- #
# CSS sanitizer — only the whitelisted properties survive inside style="".
# --------------------------------------------------------------------------- #
def test_css_allowed_property_survives():
    out = sanitize_html('<img src="http://e/x" style="max-width: 100%">')
    assert "max-width" in out


def test_css_disallowed_property_dropped():
    out = sanitize_html('<div style="position: fixed; max-height: 50px">x</div>')
    assert "position" not in out
    assert "max-height" in out


def test_all_five_css_props_survive_a_sixth_is_dropped():
    out = sanitize_html(
        '<img src="http://e/x" style="max-width: 100%; max-height: 50px; '
        'object-fit: cover; width: 10px; height: 20px; color: red">'
    )
    for prop in ("max-width", "max-height", "object-fit", "width", "height"):
        assert prop in out, prop
    # A 6th, non-whitelisted property is dropped.
    assert "color" not in out


def test_css_url_token_drops_declaration():
    # url(...) is the primary active-content vector in a style value; the whole
    # declaration is dropped (not merely the function), and no url( survives.
    out = sanitize_html('<div style="width: url(javascript:alert(1)); max-width: 5px">x</div>')
    assert "url(" not in out
    assert "javascript" not in out
    assert "max-width" in out  # the safe sibling declaration is kept


def test_css_expression_token_drops_declaration():
    # nh3's own style pass leaks a mangled `expression(` — our prefilter must kill it.
    out = sanitize_html('<div style="width: expression(alert(1))">x</div>')
    assert "expression" not in out
    assert "alert" not in out


def test_css_import_and_breakout_chars_dropped():
    out = sanitize_html('<div style="max-width: 5px; @import url(evil.css)">x</div>')
    assert "@import" not in out and "url(" not in out
    assert "max-width" in out
    # A value carrying a quote/angle-bracket breakout attempt is dropped whole.
    out2 = sanitize_html('<div style="width: 10px&quot;onmouseover=alert(1)">x</div>')
    assert "onmouseover" not in out2


# --------------------------------------------------------------------------- #
# §3.2 — FAIL-CLOSED: any bleach error escapes the raw input, never returns it raw.
# §3.16 — the single error-log name `html_sanitization_error` + log_context.
# --------------------------------------------------------------------------- #
def test_fail_closed_escapes_on_bleach_error(monkeypatch, caplog):
    def boom(*a, **k):
        raise RecursionError("bleach exploded")
    # sanitize_html resolves HTMLSanitizer as a module global at call time.
    monkeypatch.setattr(sanitizer, "HTMLSanitizer", boom, raising=True)

    payload = '<script>alert(1)</script>'
    with caplog.at_level(logging.ERROR):
        out = sanitize_html(payload, log_context="channel test, message_id 7")

    # Fail-closed: the raw payload was html.escape()d, NOT returned raw.
    assert out == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert "<script" not in out
    # §3.16: single error-log name, with the log_context included for grep-ability.
    assert "html_sanitization_error" in caplog.text
    assert "channel test, message_id 7" in caplog.text


def test_fail_closed_log_name_without_context(monkeypatch, caplog):
    def boom(*a, **k):
        raise ValueError("nope")
    monkeypatch.setattr(sanitizer, "HTMLSanitizer", boom, raising=True)
    with caplog.at_level(logging.ERROR):
        sanitize_html("<b>x</b>")
    # Same single name even when no context is supplied (single-post path).
    assert "html_sanitization_error" in caplog.text
    # The legacy per-path names must be gone.
    assert "rss_html_sanitization_error" not in caplog.text
    assert "html_final_sanitization_error" not in caplog.text


# --------------------------------------------------------------------------- #
# One-config invariant: exactly one ALLOWED_TAGS list in the tree, in sanitizer.py.
# post_parser / rss_generator must route through the module, not re-declare bleach.
# --------------------------------------------------------------------------- #
def test_single_bleach_config_no_reimport_in_render_modules():
    import inspect
    import post_parser
    import rss_generator
    for mod in (post_parser, rss_generator):
        src = inspect.getsource(mod)
        assert "allowed_tags" not in src.lower() or "sanitizer" in src, \
            f"{mod.__name__} appears to re-declare a bleach tag list instead of using sanitizer.py"
        assert "from bleach" not in src and "import bleach" not in src, \
            f"{mod.__name__} still imports bleach directly; the only config lives in sanitizer.py"


def test_generic_lang_title_stripped_but_title_kept_on_a():
    # bleach did NOT allow lang/title globally; nh3/ammonia defaults them on every
    # tag. The '*': set() entry restores parity: stripped on a generic tag...
    out = sanitize_html('<p lang="en" title="t">x</p>')
    assert 'lang' not in out and 'title' not in out
    out_div = sanitize_html('<div title="t" style="width:1px">y</div>')
    assert 'title' not in out_div
    # ...but title survives on <a> via its per-tag entry (bleach kept it there).
    out_a = sanitize_html('<a href="https://x.test" title="t">z</a>')
    assert 'title="t"' in out_a


def test_style_filter_exception_fails_closed_drops_attribute(monkeypatch):
    # nh3/PyO3 SWALLOWS an exception raised inside attribute_filter and would then
    # insert the RAW style. The try/except in _attribute_filter must FAIL-CLOSED:
    # drop the style attribute rather than let an unsanitised value through.
    def boom(_value):
        raise RuntimeError("style filter blew up")
    monkeypatch.setattr(sanitizer, "_sanitize_style", boom)
    out = sanitize_html('<div style="width:10px; background:url(javascript:alert(1))">x</div>')
    # The style attribute is gone entirely; no raw url(/javascript: leaked through.
    assert 'style=' not in out
    assert 'javascript:' not in out and 'url(' not in out
    assert '>x</div>' in out or 'x' in out  # content preserved
