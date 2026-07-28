# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, line-too-long, logging-fstring-interpolation
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Issue #66 — the "Open in Bridge" link no longer leaks the raw master token.

Two halves:
1. The rendered link embeds a scoped bridge-link *digest*, not Config['token'].
2. api_server._enforce_token accepts that digest as authorization for ONLY the one
   /html/<channel>/<post_id> it is scoped to, and for nothing else (other posts, /rss,
   /raw_json, ...). The master-token path is unchanged; a request with neither is rejected.
   The digest is never logged (issue #56 invariant).
"""
import logging
import types

import pytest
from unittest.mock import MagicMock

from pyrogram.types import Message

import api_server
import post_parser
from post_parser import PostParser
from url_signer import generate_bridge_link_digest, generate_media_digest


SECRET = api_server.Config["token"]  # "test_token" in mock_config


# --------------------------------------------------------------------------- #
# 1. The rendered link contains the digest, NOT the master token
# --------------------------------------------------------------------------- #
def _bridge_message():
    message = MagicMock(spec=Message)
    chat = MagicMock()
    chat.usernames = None
    chat.username = "durov"
    chat.id = -1001234567890
    message.chat = chat
    message.id = 42
    # Suppress the reactions / views / date footer branches so only the links line renders.
    message.reactions = None
    message.views = None
    message.date = None
    return message


def test_bridge_link_embeds_digest_not_token(monkeypatch):
    monkeypatch.setitem(post_parser.Config, "show_bridge_link", True)
    monkeypatch.setitem(post_parser.Config, "pyrogram_bridge_url", "http://bridge.example")
    monkeypatch.setitem(post_parser.Config, "token", SECRET)

    parser = PostParser(MagicMock())
    html = parser._reactions_views_links(_bridge_message())

    assert html is not None
    expected_sig = generate_bridge_link_digest("durov", 42)
    # The scoped digest is present ...
    assert f"?sig={expected_sig}" in html
    assert "/html/durov/42?sig=" in html
    # ... and the raw master token is ABSENT anywhere in the output.
    assert SECRET not in html
    assert "token=" not in html


# --------------------------------------------------------------------------- #
# 2. _enforce_token authorization contract for the bridge-link digest
# --------------------------------------------------------------------------- #
def _remote_request():
    """A non-local request: socket peer is a public IP, so the token gate is enforced."""
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host="203.0.113.9"),
        headers={},
    )


def _raises_403(fn):
    with pytest.raises(api_server.HTTPException) as ei:
        fn()
    assert ei.value.status_code == 403


def test_digest_authorizes_only_its_own_html_post():
    sig = generate_bridge_link_digest("durov", 42)
    # Valid for exactly (durov, 42) on the /html endpoint -> no raise.
    api_server._enforce_token(_remote_request(), None, "HTML post",
                              bridge_link=("durov", 42), bridge_sig=sig)


def test_digest_rejected_for_other_post():
    sig = generate_bridge_link_digest("durov", 42)
    # Same digest, different post id -> rejected.
    _raises_403(lambda: api_server._enforce_token(
        _remote_request(), None, "HTML post", bridge_link=("durov", 43), bridge_sig=sig))
    # Same digest, different channel -> rejected.
    _raises_403(lambda: api_server._enforce_token(
        _remote_request(), None, "HTML post", bridge_link=("other", 42), bridge_sig=sig))


def test_digest_useless_for_other_endpoints():
    """A bridge-link digest presented to /rss or /raw_json (callers that pass no bridge_link)
    must be rejected -- it grants read of one html post only, never the API."""
    sig = generate_bridge_link_digest("durov", 42)
    # /rss and /raw_json call _enforce_token WITHOUT bridge_link; the digest lands as `token`.
    _raises_403(lambda: api_server._enforce_token(_remote_request(), sig, "RSS endpoint"))
    _raises_403(lambda: api_server._enforce_token(_remote_request(), sig, "raw JSON post"))
    # Even if it somehow reached the raw_json handler carrying the scope tuple, raw_json never
    # passes bridge_link, so the enforce call above models the real code path faithfully.


def test_media_digest_not_accepted_as_bridge_link():
    """Domain separation at the enforce layer: a media digest for the same body must not
    authorize the /html post."""
    media_dig = generate_media_digest("durov/42")
    _raises_403(lambda: api_server._enforce_token(
        _remote_request(), None, "HTML post", bridge_link=("durov", 42), bridge_sig=media_dig))


def test_master_token_still_authorizes_everything():
    """The master-token path is byte-unchanged: it authorizes html AND every other endpoint."""
    # html (with a bridge_link present) -> the master token still wins before the digest path.
    api_server._enforce_token(_remote_request(), SECRET, "HTML post",
                              bridge_link=("durov", 42), bridge_sig=None)
    # rss / raw_json / flags (no bridge_link) -> unchanged.
    api_server._enforce_token(_remote_request(), SECRET, "RSS endpoint")
    api_server._enforce_token(_remote_request(), SECRET, "raw JSON post")


def test_no_token_and_no_digest_rejected():
    _raises_403(lambda: api_server._enforce_token(
        _remote_request(), None, "HTML post", bridge_link=("durov", 42), bridge_sig=None))
    _raises_403(lambda: api_server._enforce_token(_remote_request(), None, "RSS endpoint"))


def test_wrong_token_still_403():
    _raises_403(lambda: api_server._enforce_token(_remote_request(), "definitely-wrong", "RSS endpoint"))


# --------------------------------------------------------------------------- #
# 3. #56 invariant: neither the digest nor the secret is ever logged
# --------------------------------------------------------------------------- #
def test_digest_and_secret_not_logged_on_success(caplog):
    caplog.set_level(logging.DEBUG, logger="api_server")
    sig = generate_bridge_link_digest("durov", 42)
    api_server._enforce_token(_remote_request(), None, "HTML post",
                              bridge_link=("durov", 42), bridge_sig=sig)
    assert sig not in caplog.text
    assert SECRET not in caplog.text


def test_digest_and_secret_not_logged_on_failure(caplog):
    caplog.set_level(logging.DEBUG, logger="api_server")
    sig = generate_bridge_link_digest("durov", 42)
    _raises_403(lambda: api_server._enforce_token(
        _remote_request(), None, "HTML post", bridge_link=("durov", 99), bridge_sig=sig))
    assert sig not in caplog.text
    assert SECRET not in caplog.text


def test_malformed_sig_returns_false_not_500():
    """A malformed/non-ASCII `sig` must be a clean unauthorized (False), never a TypeError
    from hmac.compare_digest (which would surface as HTTP 500 — a cheap DoS)."""
    from url_signer import verify_bridge_link_digest
    for bad in ("é" * 32, "abc", "A" * 32, "!" * 32, "z" * 32):
        assert verify_bridge_link_digest("durov", 42, bad) is False
