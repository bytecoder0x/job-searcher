"""Characterization tests for src/filters.py — pin CURRENT behavior."""
from __future__ import annotations

import datetime

import pytest

from src import filters

TODAY = datetime.date(2024, 9, 1)  # fixed reference date for _parse_posted


# ── _parse_posted ────────────────────────────────────────────────────────
@pytest.mark.parametrize("posted,expected", [
    ("2024-03-10", datetime.date(2024, 3, 10)),
    ("today", TODAY),
    ("now", TODAY),
    ("yesterday", TODAY - datetime.timedelta(days=1)),
    ("3h", TODAY),
    ("4d", TODAY - datetime.timedelta(days=4)),
    ("1w ago", TODAY - datetime.timedelta(weeks=1)),
    ("8w", TODAY - datetime.timedelta(days=56)),
    ("3mo", TODAY - datetime.timedelta(days=90)),
    ("3 months ago", TODAY - datetime.timedelta(days=90)),
    ("1y", TODAY - datetime.timedelta(days=365)),
    ("Jul 18", datetime.date(2024, 7, 18)),   # future-in-year → rolled back below
    ("18 Jun", datetime.date(2024, 6, 18)),
    ("", None),
    ("asdf", None),
])
def test_parse_posted(posted, expected):
    assert filters._parse_posted(posted, TODAY) == expected


def test_parse_posted_future_date_rolls_back_a_year():
    # "Dec 20" seen while today is Sep 1 is in the future this year → last year.
    assert filters._parse_posted("Dec 20", TODAY) == datetime.date(2023, 12, 20)


@pytest.mark.parametrize("posted,expected", [
    ("12 Jul 2024", datetime.date(2024, 7, 12)),
    ("Jul 12, 2024", datetime.date(2024, 7, 12)),
    ("12 Jul 2023", datetime.date(2023, 7, 12)),   # explicit year, not "today.year"
])
def test_parse_posted_explicit_year_is_trusted_not_inferred(posted, expected):
    assert filters._parse_posted(posted, TODAY) == expected


def test_parse_posted_explicit_year_skips_future_rollback():
    # Without the year, "Dec 20" this year would be future → rolled back a year
    # (see test above). With an explicit year, no such rollback should apply.
    assert filters._parse_posted("Dec 20, 2024", TODAY) == datetime.date(2024, 12, 20)


def test_parse_posted_month_year_without_day_does_not_misparse():
    # Guard: a bare 4-digit year adjacent to a month must not be split and
    # consumed as a 1-2 digit "day" (e.g. "Jul 2024" -> day=20 would be wrong).
    assert filters._parse_posted("Jul 2024", TODAY) is None


# ── _within_window ───────────────────────────────────────────────────────
def test_within_window_drops_stale_keeps_fresh_and_unknown():
    today = datetime.date.today()
    stale = {"title": "old", "posted": (today - datetime.timedelta(days=10)).isoformat()}
    fresh = {"title": "new", "posted": today.isoformat()}
    unknown = {"title": "no-date", "posted": "gibberish"}
    no_field = {"title": "missing"}
    kept = filters._within_window([stale, fresh, unknown, no_field], days=1)
    assert kept == [fresh, unknown, no_field]


# ── _hard_filter ──────────────────────────────────────────────────────────
def test_hard_filter(make_position):
    profile = {"exclude": {"keywords": ["intern"]}}
    ok = make_position(title="Backend Engineer")
    bad_kw = make_position(title="Backend Intern")
    no_title = make_position(title="")
    no_url = make_position(url="")
    kept = filters._hard_filter([ok, bad_kw, no_title, no_url], profile)
    assert kept == [ok]


def test_hard_filter_matches_whole_word_not_substring(make_position):
    """"intern" must drop the standalone word "Intern" but not "International"
    or "Internet Computer" (ICP/DFINITY is a real target board, not a match)."""
    profile = {"exclude": {"keywords": ["intern"]}}
    icp = make_position(title="Internet Computer Protocol Engineer")
    intl = make_position(title="International Solidity Engineer")
    plain_intern = make_position(title="Solidity Intern")
    kept = filters._hard_filter([icp, intl, plain_intern], profile)
    assert kept == [icp, intl]


# ── _validate_items (deterministic post-extraction guard) ──────────────────
def test_validate_items_drops_hallucinated_urls():
    """A cheap model sometimes invents a link that was never on the page; a dead
    link in the digest is worse than a missing position."""
    links = ["https://board.com/jobs/a", "https://board.com/jobs/b"]
    items = [
        {"title": "Real", "url": "https://board.com/jobs/a"},
        {"title": "Invented", "url": "https://totally-other.com/jobs/x"},
    ]
    out = filters._validate_items(items, links)
    assert [i["title"] for i in out] == ["Real"]


def test_validate_items_requires_title_and_http_url():
    links = ["https://board.com/jobs/a"]
    items = [
        {"title": "", "url": "https://board.com/jobs/a"},
        {"title": "No url", "url": ""},
        {"title": "Bad scheme", "url": "javascript:alert(1)"},
        {"title": "Good", "url": "https://board.com/jobs/a"},
    ]
    assert [i["title"] for i in filters._validate_items(items, links)] == ["Good"]


def test_validate_items_strips_offschema_fields_and_coerces_types():
    links = ["https://board.com/jobs/a"]
    items = [{"title": "T", "url": "https://board.com/jobs/a", "tags": "Solidity",
              "remote": "yes", "invented_field": "nope"}]
    out = filters._validate_items(items, links)[0]
    assert "invented_field" not in out
    assert out["tags"] == []          # non-list tags coerced away
    assert out["remote"] is None      # non-bool coerced to null


def test_validate_items_caps_tags_and_dedupes():
    links = ["https://board.com/jobs/a"]
    items = [
        {"title": "T", "company": "C", "url": "https://board.com/jobs/a",
         "tags": [f"t{i}" for i in range(20)]},
        {"title": "t", "company": "c", "url": "https://board.com/jobs/a"},  # same pair
    ]
    out = filters._validate_items(items, links)
    assert len(out) == 1 and len(out[0]["tags"]) == 8


def test_validate_items_allows_any_host_when_no_candidate_links():
    """Structured sources (Getro/ATS) arrive with no candidate link list."""
    items = [{"title": "T", "url": "https://anything.io/j/1"}]
    assert len(filters._validate_items(items, [])) == 1


def test_validate_items_drops_same_host_invented_path():
    """Wave 3: grounding is now path-level, not just host — a fabricated path on
    a real board (the sneaky hallucination) is a dead link and must be dropped."""
    links = ["https://board.com/jobs/real-role-123"]
    items = [
        {"title": "Real", "url": "https://board.com/jobs/real-role-123"},
        {"title": "Invented", "url": "https://board.com/jobs/made-up-999"},  # same host!
    ]
    assert [i["title"] for i in filters._validate_items(items, links)] == ["Real"]


def test_validate_items_grounding_ignores_query_and_trailing_slash():
    """A candidate link and the model's copy may differ only by ?query or a
    trailing slash — that must still count as the same posting, not an invention."""
    links = ["https://board.com/jobs/a/"]
    items = [{"title": "T", "url": "https://board.com/jobs/a?utm=x"}]
    assert len(filters._validate_items(items, links)) == 1


def test_validate_items_reports_drop_counts_in_stats():
    """Wave 3: drops are counted (not silent) when a stats dict is passed."""
    links = ["https://board.com/jobs/a"]
    items = [
        {"title": "Good", "url": "https://board.com/jobs/a"},
        {"title": "Invented", "url": "https://board.com/jobs/nope"},
        {"title": "", "url": "https://board.com/jobs/a"},
    ]
    stats: dict = {}
    filters._validate_items(items, links, stats)
    assert stats["kept"] == 1
    assert stats["dropped"]["off_listing_url"] == 1
    assert stats["dropped"]["no_title_or_url"] == 1


# ── _parse_json_array leniency (Wave 3) ────────────────────────────────────
def test_parse_json_array_recovers_from_trailing_comma():
    from src.llm import _parse_json_array
    text = '[{"title": "A"}, {"title": "B"},]'   # trailing comma → strict json fails
    out = _parse_json_array(text)
    assert [o["title"] for o in out] == ["A", "B"]


def test_parse_json_array_salvages_good_objects_around_a_broken_one():
    from src.llm import _parse_json_array
    # middle object has an unescaped quote → whole-array parse fails; the two
    # good ones must survive instead of losing the entire source.
    text = '[{"title": "A"}, {"title": "B" broken}, {"title": "C"}]'
    out = _parse_json_array(text)
    assert [o["title"] for o in out] == ["A", "C"]


def test_parse_json_array_still_empty_on_pure_prose():
    from src.llm import _parse_json_array
    assert _parse_json_array("Sorry, I could not find any positions.") == []
