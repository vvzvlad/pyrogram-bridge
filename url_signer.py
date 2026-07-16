#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation
# pylance: disable=reportMissingImports, reportMissingModuleSource

import hashlib
import hmac
import secrets
import os
import time

# --- v2 signing scheme -------------------------------------------------------
# New media URLs are signed with HMAC-SHA256 over a *scoped* message and hex-truncated
# to 128 bits (32 chars). The scope prefix gives domain separation so a digest can only
# ever authenticate a media URL (never some other value that happens to hash the same).
SCOPE_PREFIX = "media:v2:"
NEW_DIGEST_LEN = 32          # 128 bits of HMAC-SHA256, hex
LEGACY_DIGEST_LEN = 8        # legacy: 32 bits of HMAC-SHA1, hex

# HKDF (RFC 5869) parameters used to derive the actual signing key from the source secret.
_HKDF_INFO = b"pyrogram-bridge media url signing v2"
_HKDF_SALT = b""             # empty -> HKDF-Extract uses an all-zero salt block


def _hkdf_sha256(ikm: bytes, length: int = 32, salt: bytes = _HKDF_SALT, info: bytes = _HKDF_INFO) -> bytes:
    """Minimal RFC 5869 HKDF (extract + expand) on SHA-256.

    Derives a fixed-length pseudorandom key from arbitrary input keying material so the
    same source secret always yields the same signing key across restarts / hosts.
    """
    hash_len = hashlib.sha256().digest_size
    if not salt:
        salt = b"\x00" * hash_len
    # Extract
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    # Expand
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


class KeyManager:
    SECRET_FILE = "data/media_digest.key"
    signing_key = None          # cached v2 (HKDF-derived) signing key, hex str
    _source_secret = None       # cached raw source secret feeding HKDF
    _legacy_key = None          # cached raw pre-v2 key (the on-disk key file)

    @classmethod
    def _read_or_create_key_file(cls) -> str:
        """Read the on-disk key file, or generate+persist a new one (pre-v2 behaviour).

        This is the last-resort source when neither MEDIA_SIGNING_SECRET nor the service
        token is set, so an operator who configured nothing keeps working exactly as before.
        """
        if os.path.exists(cls.SECRET_FILE):
            with open(cls.SECRET_FILE, 'r', encoding='utf-8') as f:
                return f.read().strip()
        key = secrets.token_hex(32)
        parent = os.path.dirname(cls.SECRET_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(cls.SECRET_FILE, 'w', encoding='utf-8') as f:
            f.write(key)
        return key

    @classmethod
    def _get_source_secret(cls) -> str:
        """Resolve the source secret for the v2 signing key.

        Precedence (documented in README / .env):
          1. MEDIA_SIGNING_SECRET env  -- survives any data/ volume recreation.
          2. the existing service token (config 'token', the same one api_server guards
             RSS with) -- also survives volume recreation if it is set in the env.
          3. the on-disk data/media_digest.key file (read, else generate+persist) -- the
             historical default, so an operator who set nothing keeps working as before.
        """
        if cls._source_secret is not None:
            return cls._source_secret

        secret = os.environ.get("MEDIA_SIGNING_SECRET")
        if secret:
            cls._source_secret = secret
            return cls._source_secret

        # Reuse the service token. Import get_settings LAZILY and defensively: a
        # signing-only import must never trigger config's sys.exit on missing TG_* env.
        token = ""
        try:
            from config import get_settings
            token = (get_settings() or {}).get("token", "") or ""
        except (Exception, SystemExit):
            token = ""
        if token:
            cls._source_secret = token
            return cls._source_secret

        cls._source_secret = cls._read_or_create_key_file()
        return cls._source_secret

    @classmethod
    def get_or_create_signing_key(cls) -> str:
        """Return the v2 signing key (HKDF-derived from the source secret), cached.

        If signing_key is already set (e.g. a test pins it, or a previous call derived it)
        it is returned verbatim -- callers/tests may inject a fixed key.
        """
        if cls.signing_key is not None:
            return cls.signing_key
        source = cls._get_source_secret()
        cls.signing_key = _hkdf_sha256(source.encode('utf-8'), length=16).hex()
        return cls.signing_key

    @classmethod
    def get_legacy_signing_key(cls) -> str | None:
        """Return the pre-v2 signing key that already-delivered URLs were signed with.

        That is the RAW on-disk key file only -- token/secret are new v2 inputs that never
        signed any old URL. Returns None when the file is absent (a fresh volume has no
        outstanding legacy URLs to verify).
        """
        if cls._legacy_key is not None:
            return cls._legacy_key
        if os.path.exists(cls.SECRET_FILE):
            with open(cls.SECRET_FILE, 'r', encoding='utf-8') as f:
                cls._legacy_key = f.read().strip()
                return cls._legacy_key
        return None


def media_url_expiry() -> int | None:
    """Absolute expiry (unix seconds) for a freshly generated media URL, or None.

    Controlled by MEDIA_URL_TTL_DAYS: unset / 0 / non-positive -> no expiry (default), so
    the URL shape is unchanged and legitimate late RSS fetches never break. When set, each
    feed regeneration mints a URL that expires TTL days from now.
    """
    raw = os.environ.get("MEDIA_URL_TTL_DAYS")
    if not raw or not raw.strip():
        return None
    try:
        days = int(raw)
    except ValueError:
        return None
    if days <= 0:
        return None
    return int(time.time()) + days * 86400


def _legacy_digests_allowed() -> bool:
    """Whether legacy (pre-v2 SHA-1/8-char) digests are still accepted.

    Default ENABLED so nothing breaks on deploy: media URLs already delivered to readers
    carry the old digest. Operators set MEDIA_ALLOW_LEGACY_DIGEST=false once every feed has
    been re-polled and all URLs regenerated with the v2 scheme.
    """
    raw = os.environ.get("MEDIA_ALLOW_LEGACY_DIGEST", "true").strip().lower()
    return raw not in ("false", "0", "no", "off", "disable", "disabled")


def _generate_legacy_digest(url: str) -> str | None:
    """Reproduce the pre-v2 digest: HMAC-SHA1 over the bare url, first 8 hex chars,
    using the RAW on-disk key. Returns None when no legacy key is available.
    """
    legacy_key = KeyManager.get_legacy_signing_key()
    if legacy_key is None:
        return None
    signature = hmac.new(legacy_key.encode('utf-8'), url.encode('utf-8'), hashlib.sha1)
    return signature.hexdigest()[:LEGACY_DIGEST_LEN]


def generate_media_digest(url: str, exp: int | None = None) -> str:
    """Generate the v2 media-URL digest: HMAC-SHA256 over the scoped message, 32 hex chars.

    The signed message is f"media:v2:{url}" (domain-separated). When exp is provided it is
    folded into the signed message so a tampered expiry fails verification.
    """
    signing_key = KeyManager.get_or_create_signing_key()
    message = f"{SCOPE_PREFIX}{url}"
    if exp is not None:
        message = f"{message}?exp={exp}"

    signature = hmac.new(signing_key.encode('utf-8'), message.encode('utf-8'), hashlib.sha256)
    return signature.hexdigest()[:NEW_DIGEST_LEN]


def verify_media_digest(url: str, digest: str | None, exp: int | None = None) -> bool:
    """Verify a media-URL digest.

    Accepts the v2 scheme (SHA-256 / 128-bit / scoped) OR -- for backward compatibility with
    URLs already delivered to readers -- the legacy SHA-1 / 8-char scheme, when the latter is
    enabled (MEDIA_ALLOW_LEGACY_DIGEST, default on). Both candidates are computed and then
    OR-ed (no early return between them) so the check does not leak which scheme matched.

    When exp is present the URL is a v2 (expiring) URL: only the v2 scheme is considered and
    the URL must not be past its expiry.
    """
    if not digest:
        return False

    new_expected = generate_media_digest(url, exp)
    new_ok = hmac.compare_digest(new_expected, digest)

    legacy_ok = False
    # Legacy URLs never carry an expiry, so only consider the legacy scheme when exp is absent.
    if exp is None and _legacy_digests_allowed():
        legacy_expected = _generate_legacy_digest(url)
        if legacy_expected is not None:
            legacy_ok = hmac.compare_digest(legacy_expected, digest)

    if not (new_ok or legacy_ok):
        return False

    # Freshness: an authenticated expiry must not be in the past.
    if exp is not None:
        try:
            if int(time.time()) > int(exp):
                return False
        except (TypeError, ValueError):
            return False

    return True
