# flake8: noqa
# pylint: disable=missing-function-docstring, redefined-outer-name, line-too-long, protected-access
"""Unit tests for the v2 media-URL signing scheme (issue #57).

Covers: new-scheme round-trip, tamper rejection, scope/domain separation, legacy
(pre-v2 SHA-1/8-char) dual-verify with the MEDIA_ALLOW_LEGACY_DIGEST gate, deterministic
HKDF key derivation across "restarts", and the optional (default-off) expiry.
"""
import hashlib
import hmac

import pytest

import url_signer
from url_signer import (
    KeyManager,
    generate_media_digest,
    verify_media_digest,
    media_url_expiry,
    _generate_legacy_digest,
    NEW_DIGEST_LEN,
    LEGACY_DIGEST_LEN,
    SCOPE_PREFIX,
)


@pytest.fixture(autouse=True)
def _reset_keymanager(monkeypatch):
    """Reset all KeyManager caches + signing env before each test (process-lifetime state)."""
    for var in ("MEDIA_SIGNING_SECRET", "MEDIA_ALLOW_LEGACY_DIGEST", "MEDIA_URL_TTL_DAYS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(KeyManager, "signing_key", None)
    monkeypatch.setattr(KeyManager, "_source_secret", None)
    monkeypatch.setattr(KeyManager, "_legacy_key", None)
    yield


def _use_secret(monkeypatch, secret="unit-test-secret"):
    monkeypatch.setenv("MEDIA_SIGNING_SECRET", secret)
    KeyManager.signing_key = None
    KeyManager._source_secret = None


# --------------------------------------------------------------------------- #
# New v2 scheme
# --------------------------------------------------------------------------- #
def test_new_scheme_roundtrip(monkeypatch):
    _use_secret(monkeypatch)
    url = "durov/42/AgADabc"
    digest = generate_media_digest(url)
    assert len(digest) == NEW_DIGEST_LEN == 32
    assert all(c in "0123456789abcdef" for c in digest)
    assert verify_media_digest(url, digest) is True


def test_new_scheme_uses_sha256_and_scope(monkeypatch):
    _use_secret(monkeypatch)
    url = "durov/42/AgADabc"
    key = KeyManager.get_or_create_signing_key()
    expected = hmac.new(key.encode(), f"{SCOPE_PREFIX}{url}".encode(), hashlib.sha256).hexdigest()[:32]
    assert generate_media_digest(url) == expected
    # A bare-url (unscoped) SHA-256 digest must NOT verify -- proves domain separation is applied.
    unscoped = hmac.new(key.encode(), url.encode(), hashlib.sha256).hexdigest()[:32]
    assert verify_media_digest(url, unscoped) is False


def test_tamper_rejected(monkeypatch):
    _use_secret(monkeypatch)
    url = "durov/42/AgADabc"
    digest = generate_media_digest(url)
    tampered = ("0" if digest[0] != "0" else "1") + digest[1:]
    assert verify_media_digest(url, tampered) is False
    assert verify_media_digest(url, None) is False
    assert verify_media_digest(url, "") is False


def test_scope_binds_message(monkeypatch):
    """A digest generated for url A must never verify url B (message binding)."""
    _use_secret(monkeypatch)
    digest_a = generate_media_digest("chanA/1/uidA")
    assert verify_media_digest("chanA/1/uidA", digest_a) is True
    assert verify_media_digest("chanB/1/uidA", digest_a) is False
    assert verify_media_digest("chanA/2/uidA", digest_a) is False
    assert verify_media_digest("chanA/1/uidB", digest_a) is False


# --------------------------------------------------------------------------- #
# HKDF key derivation
# --------------------------------------------------------------------------- #
def test_hkdf_key_deterministic_across_restarts(monkeypatch):
    """Same MEDIA_SIGNING_SECRET -> same signing key after a simulated restart."""
    monkeypatch.setenv("MEDIA_SIGNING_SECRET", "fixed-secret-value")

    KeyManager.signing_key = None
    KeyManager._source_secret = None
    key1 = KeyManager.get_or_create_signing_key()

    # Simulate a process restart: wipe the in-memory caches, re-derive from the env secret.
    KeyManager.signing_key = None
    KeyManager._source_secret = None
    key2 = KeyManager.get_or_create_signing_key()

    assert key1 == key2
    assert len(key1) == 32  # 16 bytes hex

    # A different secret derives a different key.
    monkeypatch.setenv("MEDIA_SIGNING_SECRET", "other-secret-value")
    KeyManager.signing_key = None
    KeyManager._source_secret = None
    assert KeyManager.get_or_create_signing_key() != key1


def test_hkdf_matches_rfc5869_construction(monkeypatch):
    secret = b"abc123"
    okm = url_signer._hkdf_sha256(secret, length=16)
    # Reproduce extract+expand independently.
    prk = hmac.new(b"\x00" * 32, secret, hashlib.sha256).digest()
    t1 = hmac.new(prk, url_signer._HKDF_INFO + bytes([1]), hashlib.sha256).digest()
    assert okm == t1[:16]


# --------------------------------------------------------------------------- #
# Legacy dual-verify
# --------------------------------------------------------------------------- #
def _write_legacy_key(monkeypatch, tmp_path, key_material="legacy-file-key-0000"):
    key_file = tmp_path / "media_digest.key"
    key_file.write_text(key_material, encoding="utf-8")
    monkeypatch.setattr(KeyManager, "SECRET_FILE", str(key_file))
    KeyManager._legacy_key = None
    return key_material


def test_legacy_digest_still_verifies_when_allowed(monkeypatch, tmp_path):
    key_material = _write_legacy_key(monkeypatch, tmp_path)
    # New v2 key comes from a *different* source (secret), so only the legacy path can match.
    _use_secret(monkeypatch, "totally-different-secret")

    url = "durov/7/AgADlegacy"
    legacy_digest = hmac.new(key_material.encode(), url.encode(), hashlib.sha1).hexdigest()[:8]
    assert len(legacy_digest) == LEGACY_DIGEST_LEN == 8
    assert legacy_digest == _generate_legacy_digest(url)

    # Legacy allowed by default -> old URL still works.
    assert verify_media_digest(url, legacy_digest) is True


def test_legacy_digest_rejected_when_disabled(monkeypatch, tmp_path):
    key_material = _write_legacy_key(monkeypatch, tmp_path)
    _use_secret(monkeypatch, "totally-different-secret")
    monkeypatch.setenv("MEDIA_ALLOW_LEGACY_DIGEST", "false")

    url = "durov/7/AgADlegacy"
    legacy_digest = hmac.new(key_material.encode(), url.encode(), hashlib.sha1).hexdigest()[:8]
    assert verify_media_digest(url, legacy_digest) is False
    # v2 URLs keep working with legacy disabled.
    assert verify_media_digest(url, generate_media_digest(url)) is True


def test_default_migration_same_file_key(monkeypatch, tmp_path):
    """Pure-default deploy: nothing set, source = the on-disk file. Old (SHA-1) AND new
    (SHA-256) digests both derive from that one file, so migration is seamless."""
    key_material = _write_legacy_key(monkeypatch, tmp_path)
    # No MEDIA_SIGNING_SECRET; force token fallback empty so source == the file key.
    class _NoToken:
        @staticmethod
        def get_settings():
            return {"token": ""}
    monkeypatch.setitem(__import__("sys").modules, "config", _NoToken)
    KeyManager.signing_key = None
    KeyManager._source_secret = None

    url = "durov/9/AgADdefault"
    legacy_digest = hmac.new(key_material.encode(), url.encode(), hashlib.sha1).hexdigest()[:8]
    new_digest = generate_media_digest(url)
    assert verify_media_digest(url, legacy_digest) is True   # outstanding URL
    assert verify_media_digest(url, new_digest) is True      # freshly minted URL


# --------------------------------------------------------------------------- #
# Optional expiry (default OFF)
# --------------------------------------------------------------------------- #
def test_expiry_off_by_default(monkeypatch):
    _use_secret(monkeypatch)
    assert media_url_expiry() is None
    for bad in ("", "0", "-1", "notanint"):
        monkeypatch.setenv("MEDIA_URL_TTL_DAYS", bad)
        assert media_url_expiry() is None


def test_expiry_signed_and_enforced(monkeypatch):
    _use_secret(monkeypatch)
    monkeypatch.setenv("MEDIA_URL_TTL_DAYS", "7")
    exp = media_url_expiry()
    assert exp is not None and exp > 0

    url = "durov/11/AgADexp"
    digest = generate_media_digest(url, exp)

    # Correct exp verifies; tampered exp does not (exp is authenticated in the message).
    assert verify_media_digest(url, digest, exp) is True
    assert verify_media_digest(url, digest, exp + 999) is False

    # An already-expired timestamp is rejected even with a valid signature.
    past = 1_000_000
    past_digest = generate_media_digest(url, past)
    assert verify_media_digest(url, past_digest, past) is False


def test_expiry_url_shape_unchanged_when_off(monkeypatch):
    """With TTL unset, a v2 URL carries no exp and verifies without one -- shape identical."""
    _use_secret(monkeypatch)
    url = "durov/12/AgADnoexp"
    digest = generate_media_digest(url)  # exp=None
    assert verify_media_digest(url, digest, None) is True
