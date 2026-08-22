"""Characterization tests for src/sources/* — pure parsing/mapping helpers, NO network."""
from __future__ import annotations

import asyncio

import pytest
from selectolax.parser import HTMLParser

from src import config
from src.sources import a16z, ats, common, getro, html, remoteok


# ── html._looks_like_listing ──────────────────────────────────────────────
@pytest.mark.parametrize("path,expected", [
    ("/jobs/solidity-engineer-remote", True),
    ("/some-cool-job/12345", True),     # numeric-ID fallback → checks the segment before it
    ("/about", False),                   # nav segment
    ("/x", False),                        # no dashes
    ("/jobs/12345", False),               # numeric id, but segment before has no dashes
])
def test_looks_like_listing(path, expected):
    assert html._looks_like_listing(path) == expected


# ── html._extract_links ────────────────────────────────────────────────────
_LINKS_HTML = """
<html><body>
<a href="/jobs/solidity-engineer-remote">Solidity Engineer</a>
<a href="/careers">Careers</a>
<a href="/about">About</a>
<a href="/login">Login</a>
<a href="https://external.test/jobs/foo-bar-baz">External job</a>
</body></html>
"""


def test_extract_links_strong_before_weak_and_excludes_nav():
    links = html._extract_links(_LINKS_HTML, "https://example.test")
    assert links == ["https://example.test/jobs/solidity-engineer-remote",
                      "https://example.test/careers"]


def test_extract_links_weak_keyword_checks_path_not_host():
    """Host names like web3.career / jobs.lever.co must not make the weak
    keyword test true on their own — only the path/query is checked."""
    doc = '<a href="/random-page">Random</a>'
    assert html._extract_links(doc, "https://web3.career") == []


def test_extract_links_caps_at_MAX_LINKS():
    html_src = "".join(f'<a href="/jobs/job-number-{i}">J{i}</a>' for i in range(html.MAX_LINKS + 10))
    links = html._extract_links(html_src, "https://example.test")
    assert len(links) == html.MAX_LINKS


# ── html._extract_listing_text / _extract_text ────────────────────────────
_CARD_HTML = """
<html><body>
<nav><a href="/about">About</a> big navigation taxonomy text goes here</nav>
<div class="card"><a href="/jobs/solidity-engineer-remote">Solidity Engineer</a>
  <p>Acme Corp — Remote — Posted today</p></div>
</body></html>
"""


def test_extract_listing_text_recovers_job_cards():
    tree = HTMLParser(_CARD_HTML)
    text = html._extract_listing_text(tree, "https://example.test")
    assert "Solidity Engineer" in text
    assert "Acme Corp" in text


def test_extract_text_prefers_listing_when_large_enough():
    cards = "".join(
        f'<div class="card"><a href="/jobs/job-title-number-{i}">Role {i} Engineer</a>'
        f'<p>Company {i} Inc — Remote — Posted today — full-time</p></div>'
        for i in range(6)
    )
    html_src = f"<html><body><nav>short nav</nav>{cards}</body></html>"
    text = html._extract_text(html_src, "https://example.test")
    assert "Company 0 Inc" in text
    assert "Role 5 Engineer" in text


def test_extract_text_falls_back_to_flatten_for_plain_page():
    html_src = "<html><body><p>" + ("General company information. " * 20) + "</p></body></html>"
    text = html._extract_text(html_src, "https://example.test")
    assert "General company information." in text


# ── remoteok._remoteok_relevant / _remoteok_salary / _remoteok_item ──────
_REMOTEOK_TERMS = ["python", "rust"]


@pytest.mark.parametrize("job,expected", [
    ({"tags": ["python", "backend"], "position": "Backend Dev"}, True),
    ({"tags": ["backend"], "position": "Sales Manager", "company": "Foo"}, False),
    ({"tags": [], "position": "Rust Engineer"}, True),
    ({"tags": ["remote"], "position": "Marketing Lead"}, False),
])
def test_remoteok_relevant(job, expected):
    assert remoteok._remoteok_relevant(job, _REMOTEOK_TERMS) == expected


@pytest.mark.parametrize("job,expected", [
    ({"salary_min": 0, "salary_max": 0}, None),
    ({"salary_min": 80000, "salary_max": 120000}, "80k–120k"),
    ({"salary_min": 90000, "salary_max": 0}, "90k"),
])
def test_remoteok_salary(job, expected):
    assert remoteok._remoteok_salary(job) == expected


def test_remoteok_item_maps_schema():
    job = {"position": "Backend Dev", "company": "Acme", "location": "Remote",
           "tags": ["python", "postgres"], "url": "https://remoteok.com/j/1", "epoch": 1704067200}
    item = remoteok._remoteok_item(job)
    assert item["title"] == "Backend Dev"
    assert item["remote"] is True
    assert item["posted"] == "2024-01-01"
    assert item["tags"] == ["python", "postgres"]


# ── a16z._a16z_salary / _a16z_item ─────────────────────────────────────────
@pytest.mark.parametrize("salary,expected", [
    (None, None),
    ({"minValue": 100000, "maxValue": 150000, "currency": "USD", "period": "year"}, "100k–150k USD"),
    ({"minValue": 100000, "maxValue": 150000, "currency": "USD", "period": "month"}, "100k–150k USD/month"),
    ({"minValue": 90000, "currency": "USD"}, "90k USD"),
])
def test_a16z_salary(salary, expected):
    job = {"salary": salary} if salary is not None else {}
    assert a16z._a16z_salary(job) == expected


def test_a16z_item_maps_schema():
    job = {"title": "Backend Engineer", "companyName": "Acme", "locations": ["Remote"],
           "remote": True, "skills": ["Python", "Postgres"], "url": "https://x/1",
           "createdAt": "2024-01-01T00:00:00Z"}
    item = a16z._a16z_item(job, "Acme")
    assert item["title"] == "Backend Engineer"
    assert item["company"] == "Acme"
    assert item["location"] == "Remote"
    assert item["posted"] == "2024-01-01"


# ── getro._getro_line / _getro_salary / _getro_item ────────────────────────
def test_getro_line_format():
    job = {"title": "Backend Engineer", "organization": {"name": "Acme"},
           "locations": ["Remote"], "work_mode": "remote", "created_at": 1704067200,
           "url": "https://x/1"}
    line = getro._getro_line(job)
    assert line == "Backend Engineer | Acme | Remote | remote | 2024-01-01 | https://x/1"


@pytest.mark.parametrize("job,expected", [
    ({}, None),
    ({"compensation_amount_min_cents": 10_000_000, "compensation_amount_max_cents": 15_000_000,
      "compensation_currency": "USD"}, "100k–150k USD"),
    ({"compensation_amount_min_cents": 9_000_000, "compensation_currency": "USD"}, "90k USD"),
])
def test_getro_salary(job, expected):
    assert getro._getro_salary(job) == expected


def test_getro_item_maps_schema():
    job = {"title": "Backend Dev", "organization": {"name": "Acme"}, "locations": ["Remote"],
           "work_mode": "Remote", "created_at": 1704067200, "skills": ["Python", "Postgres"],
           "url": "https://x/1"}
    item = getro._getro_item(job)
    assert item == {
        "title": "Backend Dev", "company": "Acme", "location": "Remote", "remote": True,
        "salary": None, "url": "https://x/1", "posted": "2024-01-01", "tags": ["Python", "Postgres"],
    }


# ── common._epoch_to_date ────────────────────────────────────────────────
@pytest.mark.parametrize("ts,expected", [
    (1704067200, "2024-01-01"),
    (None, None),
    ("not-a-number", None),      # non-numeric input degrades gracefully (TypeError)
])
def test_epoch_to_date_graceful_on_bad_input(ts, expected):
    assert common._epoch_to_date(ts) == expected


# ── common.term_in (word-boundary, shared by ats/a16z/remoteok relevance) ──
@pytest.mark.parametrize("term,haystack,expected", [
    ("dex", "we are hiring a dex trader", True),
    ("dex", "search our indexer service", False),   # 'dex' inside 'indexer'
    ("rust", "rust and solana engineer", True),
    ("rust", "a trustless bridge protocol", False),  # 'rust' inside 'trustless'
    ("amm", "amm liquidity pools", True),
    ("amm", "senior programmer role", False),        # 'amm' inside 'programmer'
])
def test_term_in_word_boundary(term, haystack, expected):
    assert common.term_in(term, haystack) == expected


# ── a16z._a16z_relevant / remoteok._remoteok_relevant use term_in (no substring FPs) ──
def test_a16z_relevant_rejects_substring_false_positive():
    job = {"title": "Indexer Engineer", "skills": ["Trustless systems"]}
    assert a16z._a16z_relevant(job, {"dex", "rust"}) is False


def test_a16z_relevant_matches_word_boundary():
    job = {"title": "DEX Engineer", "skills": ["Rust"]}
    assert a16z._a16z_relevant(job, {"dex", "rust"}) is True


def test_remoteok_relevant_rejects_substring_false_positive():
    job = {"tags": [], "position": "Trust and Safety Manager"}
    assert remoteok._remoteok_relevant(job, ["rust"]) is False


# ── common._profile_query_terms ─────────────────────────────────────────────
def test_profile_query_terms_splits_compound_skills(monkeypatch):
    # Fixed profile — the owner's live one changes with every /onboard.
    skills = ["Solidity", "Rust", "EVM smart contracts", "Foundry", "DeFi", "AMM / DEX"]
    monkeypatch.setattr(config, "load_profile", lambda: {"skills": {"must_have": skills}})
    terms = common._profile_query_terms(limit=5)
    assert terms == ["Solidity", "Rust", "EVM smart contracts", "Foundry", "DeFi"]
    terms10 = common._profile_query_terms(limit=10)
    assert "AMM" in terms10 and "DEX" in terms10
    assert "AMM / DEX" not in terms10


# ── ATS item mappers (fake httpx client — zero network) ──────────────────
class _FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status               # get_with_retry inspects this

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, data):
        self._data = data

    async def get(self, url, **kw):
        return _FakeResponse(self._data)


def test_ats_greenhouse_maps_schema():
    data = {"jobs": [{"title": "Backend Dev", "location": {"name": "Remote"},
                      "updated_at": "2024-01-01T00:00:00Z",
                      "absolute_url": "https://boards.greenhouse.io/acme/jobs/1"}]}
    out = asyncio.run(ats._ats_greenhouse(_FakeClient(data), "acme-co", cutoff=None))
    assert out == [{"title": "Backend Dev", "company": "Acme Co", "location": "Remote",
                    "remote": True, "salary": None,
                    "url": "https://boards.greenhouse.io/acme/jobs/1",
                    "posted": "2024-01-01", "tags": []}]


def test_ats_lever_maps_schema():
    data = [{"text": "Rust Engineer",
             "categories": {"location": "Remote", "workplaceType": "remote", "team": "Protocol"},
             "salaryRange": {"min": 100000, "max": 150000, "currency": "USD"},
             "createdAt": 1704067200000, "hostedUrl": "https://jobs.lever.co/acme/1"}]
    out = asyncio.run(ats._ats_lever(_FakeClient(data), "acme", cutoff=None))
    assert out[0]["title"] == "Rust Engineer"
    assert out[0]["remote"] is True
    assert out[0]["salary"] == "100k–150k USD"
    assert out[0]["tags"] == ["Protocol"]
    assert out[0]["posted"] == "2024-01-01"


def test_ats_ashby_maps_schema():
    data = {"organizationName": "Acme",
            "jobs": [{"title": "Platform Dev", "publishedAt": "2024-01-01T00:00:00Z",
                     "compensation": {"compensationTierSummary": "$100k-$150k"},
                     "department": "Eng", "team": "Core", "location": "Remote",
                     "isRemote": True, "jobUrl": "https://jobs.ashbyhq.com/acme/1"}]}
    out = asyncio.run(ats._ats_ashby(_FakeClient(data), "acme", cutoff=None))
    assert out[0]["company"] == "Acme"
    assert out[0]["salary"] == "$100k-$150k"
    assert out[0]["tags"] == ["Eng", "Core"]
    assert out[0]["remote"] is True


def test_ats_workable_maps_schema():
    data = {"name": "Acme Inc",
            "jobs": [{"title": "Infra Dev", "published_on": "2024-01-01", "city": "Remote",
                     "state": "", "country": "", "telecommuting": True,
                     "shortlink": "https://apply.workable.com/acme/j/1", "department": "Engineering"}]}
    out = asyncio.run(ats._ats_workable(_FakeClient(data), "acme", cutoff=None))
    assert out[0]["company"] == "Acme Inc"
    assert out[0]["location"] == "Remote"
    assert out[0]["remote"] is True
    assert out[0]["tags"] == ["Engineering"]
