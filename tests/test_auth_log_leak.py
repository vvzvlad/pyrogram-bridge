# flake8: noqa
# pylint: disable=protected-access, missing-function-docstring, missing-class-docstring
# pylint: disable=redefined-outer-name, logging-fstring-interpolation, line-too-long
# pylance: disable=reportMissingImports, reportMissingModuleSource
"""
Issue #56 — secret-in-log leaks + constant-time token compare.

Guards the three fixes in api_server:
- Token check (get_rss_feed / get_available_flags / ... via the shared _enforce_token
  helper): a wrong token must 403 WITHOUT the expected secret (Config["token"]) or the
  raw presented token ever reaching the logs; a correct token is accepted WITHOUT logging
  the token verbatim; comparison is constant-time (hmac.compare_digest).
- Media digest check (get_media): an invalid digest must 403 WITHOUT logging the expected
  digest (a signing-oracle output over the secret key).

The /flags endpoint is used as the token-check probe because it needs no Telegram client
or DB — it exercises the exact same _enforce_token gate every authenticated route uses.
"""
import logging

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api_server
import url_signer


SECRET = api_server.Config["token"]  # "test_token" in mock_config


def _flags_app():
    """Bare app mounting the REAL get_available_flags (no lifespan, no client)."""
    app = FastAPI()
    app.add_api_route("/flags", api_server.get_available_flags, methods=["GET"])
    app.add_api_route("/flags/{token}", api_server.get_available_flags, methods=["GET"])
    return app


def _media_app():
    app = FastAPI()
    app.add_api_route("/media/{channel}/{post_id}/{file_unique_id}/{digest}", api_server.get_media, methods=["GET"])
    app.add_api_route("/media/{channel}/{post_id}/{file_unique_id}", api_server.get_media, methods=["GET"])
    return app


# --------------------------------------------------------------------------- #
# Token check — TestClient's socket peer is "testclient", never local, so the
# token gate is always enforced here.
# --------------------------------------------------------------------------- #
def test_wrong_token_403_and_secret_not_logged(caplog):
    caplog.set_level(logging.DEBUG, logger="api_server")
    c = TestClient(_flags_app())
    r = c.get("/flags/definitely-wrong-token")
    assert r.status_code == 403
    # The expected secret must NEVER appear in the logs at any level.
    assert SECRET not in caplog.text
    # The raw presented token must not be logged verbatim either.
    assert "definitely-wrong-token" not in caplog.text
    # A short SHA-256 correlation prefix of the presented token is logged instead.
    assert "invalid_token" in caplog.text


def test_correct_token_200_and_token_not_logged(caplog):
    caplog.set_level(logging.DEBUG, logger="api_server")
    c = TestClient(_flags_app())
    r = c.get(f"/flags/{SECRET}")
    assert r.status_code == 200
    # Success path must not log the token verbatim.
    assert SECRET not in caplog.text


def test_missing_token_403(caplog):
    caplog.set_level(logging.DEBUG, logger="api_server")
    c = TestClient(_flags_app())
    r = c.get("/flags")  # no token supplied -> None guarded, must 403 (not 500)
    assert r.status_code == 403
    assert SECRET not in caplog.text


def test_non_ascii_token_403_no_crash(caplog):
    # A non-ASCII presented token must be rejected as 403, not raise a 500 (hmac.compare_digest
    # over str would raise on non-ASCII; the fix compares UTF-8 bytes instead).
    caplog.set_level(logging.DEBUG, logger="api_server")
    c = TestClient(_flags_app())
    r = c.get("/flags/пароль")  # "пароль"
    assert r.status_code == 403
    assert SECRET not in caplog.text


# --------------------------------------------------------------------------- #
# Media digest check — the expected digest (signing-oracle output) must never leak.
# --------------------------------------------------------------------------- #
def test_invalid_media_digest_403_expected_not_logged(caplog, monkeypatch, tmp_path):
    # Keep the signing-key file inside a temp dir; do NOT mock verify_media_digest so the
    # real gate runs and the real log line is emitted.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    caplog.set_level(logging.DEBUG, logger="api_server")

    url = "chan/7/fidX"
    expected = url_signer.generate_media_digest(url)  # same cached key the endpoint uses

    c = TestClient(_media_app())
    r = c.get("/media/chan/7/fidX/bogusdigest")
    assert r.status_code == 403
    # The expected digest (a signature over the secret key) must NOT appear in the logs.
    assert expected not in caplog.text
    # The presented (attacker-supplied) digest is safe to log for correlation.
    assert "bogusdigest" in caplog.text
