# flake8: noqa
# pylint: disable=missing-function-docstring, redefined-outer-name, line-too-long
"""Stage-0 golden oracle (render-pipeline refactor epic, issue #27/#34).

Regenerates the RSS + HTML feeds for the frozen recorded corpus and asserts
byte-equality against the committed goldens after the spec's declared normalization
(strip volatile <lastBuildDate>). Any other byte change = a render regression, and
this test must catch it. (The stage-0 merged-flags sort normalization was removed in
stage 2: flag order is now deterministic first-seen order — §3.8.)

Guardrails: this stage only ADDS a loader + goldens + this test. No render/pipeline
production code is touched; the goldens freeze CURRENT behavior including known bugs
(fixed in later stages, each referencing a §3 registry item).

Regenerate goldens with:  python -m tests.golden_replay
"""
import pytest

from tests import golden_replay as gr


@pytest.fixture
def golden_env(monkeypatch):
    """Apply the determinism pins (signing key, time_based_merge, DB no-op). TZ=UTC is
    pinned process-wide in conftest."""
    gr.pin_environment(monkeypatch)
    return monkeypatch


def _read_golden(channel, kind):
    with open(gr.golden_path(channel, kind), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("channel", gr.CORPUS_CHANNELS)
def test_rss_golden(channel, golden_env):
    gr.patch_tg_cache(golden_env, channel)
    actual = gr.capture_rss(channel)
    expected = _read_golden(channel, "rss")
    assert gr.normalize_rss(actual) == gr.normalize_rss(expected), \
        f"RSS feed for {channel} diverged from the golden (render regression)"


@pytest.mark.parametrize("channel", gr.CORPUS_CHANNELS)
def test_html_golden(channel, golden_env):
    gr.patch_tg_cache(golden_env, channel)
    actual = gr.capture_html(channel)
    expected = _read_golden(channel, "html")
    assert gr.normalize_html(actual) == gr.normalize_html(expected), \
        f"HTML feed for {channel} diverged from the golden (render regression)"


def test_all_goldens_present_and_nonempty():
    """The corpus and its goldens must stay in lockstep — a missing/empty golden would
    silently pass the parametrized tests only if a channel were also dropped."""
    import os
    for channel in gr.CORPUS_CHANNELS:
        for kind in ("rss", "html"):
            path = gr.golden_path(channel, kind)
            assert os.path.exists(path), f"missing golden: {path}"
            assert os.path.getsize(path) > 0, f"empty golden: {path}"


def test_normalization_preserves_flag_order():
    """Stage 2 (§3.8): merged-post flags are now emitted in deterministic first-seen
    order, so normalize_html no longer reorders the flags div — a real flag reordering
    must reach the byte comparison instead of being masked."""
    a = '<div class="message-flags"> 🏷 video 🏷 fwd 🏷 link </div>'
    b = '<div class="message-flags"> 🏷 link 🏷 fwd 🏷 video </div>'
    assert gr.normalize_html(a) != gr.normalize_html(b)
    assert gr.normalize_html(a) == a


def test_normalization_strips_lastbuilddate():
    """<lastBuildDate> is the one volatile RSS field (feedgen now() in the constructor)."""
    a = "<x><lastBuildDate>Mon, 06 Jul 2026 07:32:04 +0000</lastBuildDate><y/></x>"
    b = "<x><lastBuildDate>Mon, 06 Jul 2026 09:15:59 +0000</lastBuildDate><y/></x>"
    assert gr.normalize_rss(a) == gr.normalize_rss(b)
