#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-caught, logging-fstring-interpolation, line-too-long

"""The ONLY bleach configuration in the project.

Before this module the sanitize config was copy-pasted three times (the single-post
path in post_parser plus the RSS and HTML feed paths in rss_generator) and had
drifted: `s`/`del` survived only in the single-post copy, and the three error-log
names diverged. Here the config lives exactly once; every render path routes
through sanitize_html().
"""

import logging
import time as _time
from html import escape as _html_escape

# Imported under this name so tests can monkeypatch `sanitizer.HTMLSanitizer`
# (API relocation from rss_generator — no behaviour change).
from bleach import clean as HTMLSanitizer
from bleach.css_sanitizer import CSSSanitizer

logger = logging.getLogger(__name__)

# Union of the three former copies. `s`/`del` (strikethrough) were previously
# allowed only in the single-post path; registry §3.1 makes them survive in feeds too.
ALLOWED_TAGS = ['p', 'a', 'b', 'i', 'strong', 'em', 's', 'del',
                'ul', 'ol', 'li', 'br', 'div', 'span',
                'img', 'video', 'audio', 'source']

# Identical in all three former copies — moved here as-is.
ALLOWED_ATTRIBUTES = {
    'a': ['href', 'title', 'target'],
    'img': ['src', 'alt', 'style'],
    'video': ['controls', 'src', 'style'],
    'audio': ['controls', 'style'],
    'source': ['src', 'type'],
    'div': ['class', 'style'],
    'span': ['class'],
}

ALLOWED_CSS_PROPERTIES = ["max-width", "max-height", "object-fit", "width", "height"]

# Non-default! bleach's default protocol list would strip tg:// links (channel
# footers / service links use them). Load-bearing.
ALLOWED_PROTOCOLS = ['http', 'https', 'tg']

# Shared instance is safe: CSSSanitizer only holds the allowed-properties list
# (stateless config); bleach builds a fresh Cleaner per clean() call anyway.
_CSS_SANITIZER = CSSSanitizer(allowed_css_properties=ALLOWED_CSS_PROPERTIES)


def sanitize_html(html_raw: str, log_context: str = "") -> str:
    """Sanitize one HTML fragment.

    FAIL-CLOSED: on any bleach error the fragment is html.escape()d, never returned
    raw (stored-XSS guard — registry §3.2). log_context (e.g. "channel X, message_id
    Y") is included in the error/slow logs to keep operational grep-ability across
    call sites.
    """
    sanitize_start = _time.monotonic()
    try:
        # Both non-default params are load-bearing:
        #   protocols=ALLOWED_PROTOCOLS keeps tg:// links alive;
        #   strip=True REMOVES disallowed tags (bleach's default False would escape
        #   them into visible text). strip_comments stays default (True), matching
        #   all former call sites.
        sanitized_html = HTMLSanitizer(
            html_raw,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
            protocols=ALLOWED_PROTOCOLS,
            css_sanitizer=_CSS_SANITIZER,
            strip=True,
        )
    except Exception as e:
        # Single error-log name across all call sites (registry §3.16); log_context
        # distinguishes them. Fail-closed: escape rather than emit the raw payload.
        _ctx = f"{log_context}, " if log_context else ""
        logger.error(f"html_sanitization_error: {_ctx}error {str(e)}")
        return _html_escape(html_raw)
    elapsed = _time.monotonic() - sanitize_start
    if elapsed > 0.05:
        _ctx = f", {log_context}" if log_context else ""
        logger.warning(f"diag_sanitize_slow: bleach.clean() took {elapsed:.3f}s, input_len={len(html_raw)}{_ctx}")
    return sanitized_html
