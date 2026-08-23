"""Characterization tests for src/digest.py — pin CURRENT behavior."""
from __future__ import annotations

import pytest

from src import digest


# ── _empty_reason ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("page,render,expect_substr", [
    ({"error": "boom"}, "static", "fetch error: boom"),
    ({"text": "Access Denied"}, "static", "HTTP 403"),
    ({"text": "please complete the captcha challenge"}, "static", "anti-bot"),
    ({"text": "we use cookies here"}, "static", "cookie-consent"),
    ({"text": "{}"}, "getro", "source API returned 0 postings"),
    ({"text": "hi"}, "static", "no content returned"),
    ({"text": "x" * 500}, "static", "no job listings were parsed"),
])
def test_empty_reason(page, render, expect_substr):
    assert expect_substr in digest._empty_reason(page, render)


# ── _no_data_footer ───────────────────────────────────────────────────────
def test_no_data_footer_empty():
    assert digest._no_data_footer({"no_data": []}) == ""


def test_no_data_footer_lists_reasons():
    stats = {"sources": 3, "no_data": [{"name": "A", "reason": "r1"}, {"name": "B", "reason": "r2"}]}
    footer = digest._no_data_footer(stats)
    assert "2/3 sources" in footer
    assert "• A — r1" in footer
    assert "• B — r2" in footer


# ── _render_digest ────────────────────────────────────────────────────────
def test_render_digest_no_scored():
    text = digest._render_digest([], {"days": 1, "sources": 2, "no_data": []})
    assert text == "🔍 No new relevant positions."


def test_render_digest_no_scored_with_footer():
    stats = {"days": 1, "sources": 2, "no_data": [{"name": "A", "reason": "r"}]}
    text = digest._render_digest([], stats)
    assert text.startswith("🔍 No new relevant positions.")
    assert "• A — r" in text


def test_filtered_footer_explains_dedup_and_rule_drops():
    """A small 'relevant' count must be explained: most of raw→fresh is
    already-shown (dedup), not a broken scan."""
    footer = digest._filtered_footer({"already_seen": 720, "rule_filtered": 5})
    assert "720 already shown" in footer and "5 filtered by rules" in footer
    assert "/export" in footer


def test_filtered_footer_empty_when_nothing_skipped():
    assert digest._filtered_footer({}) == ""
    assert digest._filtered_footer({"already_seen": 0, "rule_filtered": 0}) == ""


def test_render_digest_shows_filtered_footer_on_empty():
    """The explanation is most needed exactly when 0 relevant are shown."""
    stats = {"days": 7, "sources": 35, "no_data": [], "already_seen": 720}
    text = digest._render_digest([], stats)
    assert "720 already shown" in text


def test_recall_footer_lists_shortfall():
    footer = digest._recall_footer({"shortfall": [{"name": "Web3 Career", "kept": 19, "records": 67}]})
    assert "Web3 Career — 19/67" in footer


def test_recall_footer_empty_when_no_shortfall():
    assert digest._recall_footer({}) == ""
    assert digest._recall_footer({"shortfall": []}) == ""


def test_render_digest_with_scored():
    scored = [{"score": 80, "title": "Eng", "company": "Acme", "location": "Remote",
               "salary": "100k", "posted": "today", "reason": "good fit", "url": "https://x/1"}]
    stats = {"days": 1, "sources": 2, "no_data": [], "threshold": 60, "top_n": 15}
    text = digest._render_digest(scored, stats)
    assert "1 relevant of 1 scored" in text
    assert "1. 80 | Eng @ Acme — Remote" in text
    assert "💰 100k" in text and "🗓 today" in text
    assert "✅ why: good fit" in text
    assert "🔗 https://x/1" in text


def test_render_digest_numbers_from_one_and_ranks_best_first():
    scored = [
        {"score": 62, "title": "Third", "company": "C", "url": "https://x/3"},
        {"score": 91, "title": "First", "company": "A", "url": "https://x/1"},
        {"score": 75, "title": "Second", "company": "B", "url": "https://x/2"},
    ]
    stats = {"days": 7, "sources": 5, "no_data": [], "threshold": 60, "top_n": 15}
    text = digest._render_digest(scored, stats)
    assert "1. 91 | First @ A" in text
    assert "2. 75 | Second @ B" in text
    assert "3. 62 | Third @ C" in text
    assert text.index("1. 91") < text.index("2. 75") < text.index("3. 62")


def test_render_digest_hides_weak_matches_and_says_so():
    """Sub-threshold roles (sales/PM/support noise) must NOT be dumped into the
    message — they stay in the store for /export."""
    scored = [
        {"score": 80, "title": "Strong", "company": "Acme", "url": "https://x/1"},
        {"score": 30, "title": "Weak", "company": "Beta", "url": "https://x/2"},
        {"score": 55, "title": "Mid", "company": "Gamma", "url": "https://x/3"},
    ]
    stats = {"days": 7, "sources": 5, "no_data": [], "threshold": 60, "top_n": 15}
    text = digest._render_digest(scored, stats)
    assert "1. 80 | Strong @ Acme" in text
    assert "Weak" not in text and "Mid" not in text
    assert "2 weaker, below threshold" in text


def test_render_digest_top_n_cap_distinguishes_hidden_relevant_from_weak():
    """When more positions clear the threshold than top_n allows, the header
    must count ALL relevant matches (not just the shown slice), and the hidden
    line must tell apart capped-but-relevant from genuinely below-threshold."""
    scored = [{"score": 70 + i, "title": f"R{i}", "company": "C", "url": f"https://x/{i}"}
              for i in range(5)]                                   # 5 relevant
    scored.append({"score": 30, "title": "Weak", "company": "C", "url": "https://x/weak"})
    stats = {"days": 7, "sources": 5, "no_data": [], "threshold": 60, "top_n": 3}
    text = digest._render_digest(scored, stats)
    assert "5 relevant of 6 scored" in text          # NOT "3 relevant" (len(shown))
    assert "2 more above the top-3 cap" in text
    assert "1 weaker, below threshold" in text


def test_render_digest_all_below_threshold_reads_as_empty():
    scored = [{"score": 40, "title": "Meh", "company": "C", "url": "https://x/1"}]
    stats = {"days": 7, "sources": 5, "no_data": [], "threshold": 60, "top_n": 15}
    assert digest._render_digest(scored, stats).startswith("🔍 No new relevant positions.")


# ── truncation footer (silent-loss visibility) ────────────────────────────
def test_truncated_footer_lists_dropped_sources():
    stats = {"days": 7, "sources": 36, "no_data": [], "threshold": 60, "top_n": 15,
             "truncated": [{"name": "Coinbase (GH)", "dropped": 180, "kind": "board cap"},
                           {"name": "Web3 Career", "dropped": 9, "kind": "text budget"}]}
    scored = [{"score": 80, "title": "Eng", "company": "Acme", "url": "https://x/1"}]
    text = digest._render_digest(scored, stats)
    assert "Truncated" in text
    assert "Coinbase (GH) — 180 dropped (board cap)" in text
    assert "Web3 Career — 9 dropped (text budget)" in text


def test_no_truncated_footer_when_nothing_dropped():
    stats = {"days": 7, "sources": 36, "no_data": [], "threshold": 60, "top_n": 15, "truncated": []}
    scored = [{"score": 80, "title": "Eng", "company": "Acme", "url": "https://x/1"}]
    assert "Truncated" not in digest._render_digest(scored, stats)


# ── degraded footer (silent half-break visibility) ─────────────────────────
def test_degraded_footer_lists_sources():
    stats = {"days": 7, "sources": 36, "no_data": [], "threshold": 60, "top_n": 15,
             "degraded": [{"name": "Web3 Career", "yield": 3, "baseline": 40}]}
    scored = [{"score": 80, "title": "Eng", "company": "Acme", "url": "https://x/1"}]
    text = digest._render_digest(scored, stats)
    assert "Yield dropped" in text
    assert "Web3 Career — 3 now vs ~40 usual" in text
