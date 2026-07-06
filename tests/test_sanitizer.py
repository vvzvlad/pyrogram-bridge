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
