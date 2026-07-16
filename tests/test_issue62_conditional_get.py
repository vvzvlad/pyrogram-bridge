"""
Issue #62 — conditional GET on the feed endpoint.

The RSS/HTML feed responses must carry validators (ETag + Last-Modified) and honor
If-None-Match / If-Modified-Since with a bodyless 304, so a reader's poll of an
unchanged feed no longer forces a full regenerate+resend on the wire.

These tests drive api_server.get_rss_feed through a TestClient with generate_channel_*
mocked out, so they exercise the header/304 logic without touching Telegram.
"""

import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import api_server


TOKEN = api_server.Config["token"]  # "test_token" in mock_config


def _rss_body(*, title: str, pub_date: datetime, last_build: datetime) -> str:
    """A minimal RSS 2.0 body with a volatile <lastBuildDate> and one dated entry."""
    def rfc822(dt: datetime) -> str:
        # e.g. "Wed, 01 Jan 2020 00:00:00 +0000"
        return dt.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        "<title>chan</title>"
        f"<lastBuildDate>{rfc822(last_build)}</lastBuildDate>"
        "<item>"
        f"<title>{title}</title>"
        f"<pubDate>{rfc822(pub_date)}</pubDate>"
        "</item>"
        "</channel></rss>"
    )


class _FakeClient:
    """Stands in for api_server.client (only .client is touched by the handler)."""
    def __init__(self):
        self.client = object()


@pytest.fixture
def feed(monkeypatch):
    """Install a fake TG client + a mutable feed body, yield (set_body, TestClient)."""
    monkeypatch.setattr(api_server, "client", _FakeClient())

    holder = {"body": _rss_body(
        title="post-1",
        pub_date=datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        last_build=datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc),
    )}

    async def _fake_rss(channel, **kwargs):
        return holder["body"]

    async def _fake_html(channel, **kwargs):
        # HTML feed carries no per-entry pubDate.
        return "<div>post-1</div>"

    monkeypatch.setattr(api_server, "generate_channel_rss", _fake_rss)
    monkeypatch.setattr(api_server, "generate_channel_html", _fake_html)

    def _set_body(body):
        holder["body"] = body

    return _set_body, TestClient(api_server.app)


def _url(output_type="rss"):
    return f"/rss/somechannel/{TOKEN}?output_type={output_type}"


# --------------------------------------------------------------------------- #
# 200 + validators
# --------------------------------------------------------------------------- #
def test_first_get_returns_200_with_validators(feed):
    _set_body, cl = feed
    r = cl.get(_url())
    assert r.status_code == 200
    assert r.content  # body present on first fetch

    etag = r.headers.get("etag")
    assert etag is not None
    # strong ETag = quoted sha256 hex
    assert re.fullmatch(r'"[0-9a-f]{64}"', etag), etag

    # Last-Modified reflects the freshest entry's pubDate.
    assert r.headers.get("last-modified") == "Thu, 02 Jan 2020 03:04:05 GMT"

    cc = r.headers.get("cache-control", "")
    assert "private" in cc
    assert "max-age" in cc


# --------------------------------------------------------------------------- #
# If-None-Match → 304 + no body
# --------------------------------------------------------------------------- #
def test_if_none_match_returns_304_no_body(feed):
    _set_body, cl = feed
    first = cl.get(_url())
    etag = first.headers["etag"]

    second = cl.get(_url(), headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""  # 304 must not carry a body
    # validators are still echoed on the 304
    assert second.headers.get("etag") == etag
    assert "private" in second.headers.get("cache-control", "")


def test_if_none_match_star_returns_304(feed):
    _set_body, cl = feed
    r = cl.get(_url(), headers={"If-None-Match": "*"})
    assert r.status_code == 304
    assert r.content == b""


def test_if_none_match_weak_validator_matches(feed):
    _set_body, cl = feed
    etag = cl.get(_url()).headers["etag"]
    r = cl.get(_url(), headers={"If-None-Match": f"W/{etag}"})
    assert r.status_code == 304


def test_if_none_match_in_list_matches(feed):
    _set_body, cl = feed
    etag = cl.get(_url()).headers["etag"]
    r = cl.get(_url(), headers={"If-None-Match": f'"deadbeef", {etag}'})
    assert r.status_code == 304


# --------------------------------------------------------------------------- #
# Changed feed → 200 + new validator
# --------------------------------------------------------------------------- #
def test_changed_feed_new_etag_and_200(feed):
    set_body, cl = feed
    first = cl.get(_url())
    old_etag = first.headers["etag"]

    set_body(_rss_body(
        title="post-2-new-content",
        pub_date=datetime(2021, 6, 6, 6, 6, 6, tzinfo=timezone.utc),
        last_build=datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc),
    ))

    # Stale validator → full 200 with a fresh ETag.
    r = cl.get(_url(), headers={"If-None-Match": old_etag})
    assert r.status_code == 200
    assert r.content
    new_etag = r.headers["etag"]
    assert new_etag != old_etag
    assert r.headers.get("last-modified") == "Sun, 06 Jun 2021 06:06:06 GMT"


def test_etag_stable_when_only_lastbuilddate_changes(feed):
    """The ETag is a content signature: a differing <lastBuildDate> alone must NOT bust it."""
    set_body, cl = feed
    etag1 = cl.get(_url()).headers["etag"]

    # Same content, different build stamp (as feedgen emits on every serialize).
    set_body(_rss_body(
        title="post-1",
        pub_date=datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        last_build=datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
    ))
    etag2 = cl.get(_url()).headers["etag"]
    assert etag1 == etag2

    # And a conditional GET across that build-stamp change still yields 304.
    r = cl.get(_url(), headers={"If-None-Match": etag1})
    assert r.status_code == 304


# --------------------------------------------------------------------------- #
# If-Modified-Since
# --------------------------------------------------------------------------- #
def test_if_modified_since_not_modified_returns_304(feed):
    _set_body, cl = feed
    # Client copy is newer than the freshest entry (2020-01-02) → 304.
    r = cl.get(_url(), headers={"If-Modified-Since": "Wed, 01 Jan 2025 00:00:00 GMT"})
    assert r.status_code == 304
    assert r.content == b""


def test_if_modified_since_stale_returns_200(feed):
    _set_body, cl = feed
    # Client copy predates the freshest entry → full 200.
    r = cl.get(_url(), headers={"If-Modified-Since": "Wed, 01 Jan 2000 00:00:00 GMT"})
    assert r.status_code == 200
    assert r.content


# --------------------------------------------------------------------------- #
# HTML output: ETag drives conditional GET even without dates
# --------------------------------------------------------------------------- #
def test_html_feed_has_etag_and_304(feed):
    _set_body, cl = feed
    first = cl.get(_url("html"))
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert re.fullmatch(r'"[0-9a-f]{64}"', etag)
    # HTML has no per-entry pubDate → no Last-Modified, ETag still works.
    assert "last-modified" not in {k.lower() for k in first.headers}

    second = cl.get(_url("html"), headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
