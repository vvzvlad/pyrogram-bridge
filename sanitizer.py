#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-caught, logging-fstring-interpolation, line-too-long

"""The ONLY HTML-sanitizer configuration in the project.

Before this module the sanitize config was copy-pasted three times (the single-post
path in post_parser plus the RSS and HTML feed paths in rss_generator) and had
drifted: `s`/`del` survived only in the single-post copy, and the three error-log
names diverged. Here the config lives exactly once; every render path routes
through sanitize_html().

Backend: `nh3` (Rust `ammonia` binding). Migrated off `bleach[css]` 6.1.0 after the
bleach maintainer declared the project end-of-life with no further security fixes
(mozilla/bleach#698). nh3 is actively maintained and keeps the same allowlist model.
The one API gap — nh3 has no `CSSSanitizer` equivalent — is closed here by an
`attribute_filter` that scrubs the `style` attribute down to the same 5 CSS
properties, with a hard reject of any active-content token (see _sanitize_style).
"""

import logging
import re
import time as _time
from html import escape as _html_escape

# Imported under this name so tests can monkeypatch `sanitizer.HTMLSanitizer`
# (kept from the bleach era — sanitize_html resolves the name as a module global
# at call time, so the fail-closed path stays test-patchable). No behaviour change.
from nh3 import clean as HTMLSanitizer

logger = logging.getLogger(__name__)

# Union of the three former copies. `s`/`del` (strikethrough) were previously
# allowed only in the single-post path; registry §3.1 makes them survive in feeds too.
ALLOWED_TAGS = ['p', 'a', 'b', 'i', 'strong', 'em', 's', 'del',
                'ul', 'ol', 'li', 'br', 'div', 'span',
                'img', 'video', 'audio', 'source']

# Identical in all three former copies — moved here as-is. nh3 wants per-tag sets.
ALLOWED_ATTRIBUTES = {
    # '*' = attributes allowed on EVERY tag. Empty by design: nh3/ammonia otherwise
    # defaults generic attributes to {lang, title}, but bleach did NOT allow them
    # globally (only title on <a>, via the per-tag entry below). This keeps exact
    # parity — the per-tag entries are the ONLY source of allowed attributes.
    '*': set(),
    'a': {'href', 'title', 'target'},
    'img': {'src', 'alt', 'style'},
    'video': {'controls', 'src', 'style'},
    'audio': {'controls', 'style'},
    'source': {'src', 'type'},
    'div': {'class', 'style'},
    'span': {'class'},
}

# These 5 props SIZE media in readers; dropping `style` outright would change
# rendering, so `style` is kept but its content is filtered down to exactly these.
ALLOWED_CSS_PROPERTIES = ["max-width", "max-height", "object-fit", "width", "height"]

# Non-default! nh3's default url_schemes would strip tg:// links (channel footers /
# service links use them). Load-bearing. Kept as a list so the one-config invariant
# test can assert its exact value; converted to a set for nh3 below.
ALLOWED_PROTOCOLS = ['http', 'https', 'tg']

_ALLOWED_TAGS_SET = frozenset(ALLOWED_TAGS)
_ALLOWED_URL_SCHEMES = frozenset(ALLOWED_PROTOCOLS)

# --- style-attribute sanitizer (replaces bleach's CSSSanitizer) --------------- #
# `<prop>: <value>` inside a single declaration. Property matched against the
# 5-item whitelist; value passed through the guards below.
_STYLE_DECL_RE = re.compile(r'^\s*([A-Za-z-]+)\s*:\s*(.+?)\s*$')
# Any of these tokens in a value => drop the whole declaration. Covers every active
# -content vector: url(...) (image/script fetch), CSS expression() (legacy IE JS),
# javascript:/@import, and the structural chars that could break out of the
# attribute or inject markup ("  <  >  {  }  \).
_STYLE_UNSAFE_RE = re.compile(r'url\(|expression|javascript:|@import|[<>{}"\\]', re.IGNORECASE)
# Positive whitelist of value characters: letters/digits, units & separators only.
# Deliberately excludes '(' ')' so no CSS function (calc, url, expression, …) can
# survive even if the negative check above ever missed one.
_STYLE_VALUE_RE = re.compile(r'^[A-Za-z0-9%.,#/_\s!-]+$')


def _sanitize_style(value: str) -> str:
    """Return a style string containing only whitelisted, provably-inert declarations.

    Parses `value` declaration-by-declaration, keeps only the 5 ALLOWED_CSS_PROPERTIES,
    and drops any declaration whose value carries an active-content token or a char
    outside the safe set. The result can never carry url()/expression()/javascript:/
    @import or attribute-breakout chars, so nh3 can emit it verbatim safely.
    """
    kept = []
    for decl in value.split(';'):
        m = _STYLE_DECL_RE.match(decl.strip())
        if not m:
            continue
        prop = m.group(1).lower()
        val = m.group(2).strip()
        if prop not in ALLOWED_CSS_PROPERTIES:
            continue
        if _STYLE_UNSAFE_RE.search(val):
            continue
        if not _STYLE_VALUE_RE.match(val):
            continue
        kept.append(f"{prop}: {val}")
    return "; ".join(kept)


def _attribute_filter(tag: str, attr: str, value: str):
    """nh3 attribute_filter: scrub `style`, pass every other whitelisted attr through.

    nh3 hands us the RAW attribute value and inserts whatever we return verbatim, so
    `style` MUST be rebuilt from the strict whitelist here. Returning None drops the
    attribute entirely (used when no declaration survives). Non-style attributes are
    already scheme-filtered by url_schemes; we return them unchanged.
    """
    if attr == 'style':
        try:
            return _sanitize_style(value) or None
        except Exception:
            # FAIL-CLOSED at the filter level: nh3/PyO3 swallows an exception raised
            # inside this callback and would then insert the RAW style value. Dropping
            # the attribute instead keeps the fail-closed guarantee on the style path.
            return None
    return value


def sanitize_html(html_raw: str, log_context: str = "") -> str:
    """Sanitize one HTML fragment.

    FAIL-CLOSED: on any sanitizer error the fragment is html.escape()d, never returned
    raw (stored-XSS guard — registry §3.2). log_context (e.g. "channel X, message_id
    Y") is included in the error/slow logs to keep operational grep-ability across
    call sites.
    """
    sanitize_start = _time.monotonic()
    try:
        # Load-bearing config:
        #   url_schemes=_ALLOWED_URL_SCHEMES keeps tg:// links alive (nh3's default
        #     scheme list would strip them);
        #   attribute_filter=_attribute_filter replaces bleach's CSSSanitizer, keeping
        #     only the 5 inert CSS props on `style`;
        #   disallowed tags are REMOVED (nh3 strips them, matching the old strip=True —
        #     the tag vanishes rather than being escaped to visible text);
        #   clean_content_tags=set() preserves bleach's strip=True semantics for raw-text
        #     elements: strip the <script>/<style> TAG but keep its (now-inert) text
        #     content, instead of nh3's default of deleting tag AND content;
        #   strip_comments stays default (True), matching all former call sites;
        #   link_rel=None preserves bleach's exact <a> output (no injected rel).
        sanitized_html = HTMLSanitizer(
            html_raw,
            tags=_ALLOWED_TAGS_SET,
            attributes=ALLOWED_ATTRIBUTES,
            url_schemes=_ALLOWED_URL_SCHEMES,
            attribute_filter=_attribute_filter,
            clean_content_tags=set(),
            link_rel=None,
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
        logger.warning(f"diag_sanitize_slow: nh3.clean() took {elapsed:.3f}s, input_len={len(html_raw)}{_ctx}")
    return sanitized_html
