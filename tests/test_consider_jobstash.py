"""Characterization tests for src/sources/consider.py and jobstash.py — pure
parsing/mapping helpers, NO network (mirrors test_sources.py)."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from src.sources import consider, jobstash

# ── consider._consider_board_slug ──────────────────────────────────────────
_BOARD_HTML = '<script>window.serverInitialData = {"fixedBoard":"pantera-capital","other":1};</script>'


def test_consider_board_slug_extracted_from_html():
    assert consider._consider_board_slug(_BOARD_HTML) == "pantera-capital"


def test_consider_board_slug_none_when_absent():
    assert consider._consider_board_slug("<html>no data here</html>") is None


def test_consider_host_derives_scheme_and_netloc():
    assert consider._consider_host("https://jobs.panteracapital.com/jobs") == "https://jobs.panteracapital.com"


# ── consider._consider_salary ───────────────────────────────────────────────
def test_consider_salary_range():
    job = {"salary": {"minValue": 224000, "maxValue": 300000,
                      "currency": {"value": "USD"}, "period": {"value": "year"}}}
    assert consider._consider_salary(job) == "224k–300k USD"


def test_consider_salary_with_non_year_period():
    job = {"salary": {"minValue": 8000, "currency": {"value": "USD"}, "period": {"value": "month"}}}
    assert consider._consider_salary(job) == "8k USD/month"


def test_consider_salary_none_when_missing():
    assert consider._consider_salary({}) is None
    assert consider._consider_salary({"salary": "not a dict"}) is None


# ── consider._consider_item ─────────────────────────────────────────────────
def test_consider_item_maps_schema():
    job = {"title": "Backend Engineer", "companyName": "Acme", "locations": ["Remote", "US"],
           "remote": True, "skills": [{"label": "Rust"}, {"label": "Solidity"}],
           "url": "https://jobs.panteracapital.com/jobs/1",
           "timeStamp": "2026-08-08T10:34:14Z"}
    item = consider._consider_item(job)
    assert item["title"] == "Backend Engineer"
    assert item["company"] == "Acme"
    assert item["location"] == "Remote, US"
    assert item["remote"] is True
    assert item["posted"] == "2026-08-08"
    assert item["tags"] == ["Rust", "Solidity"]


def test_consider_item_hybrid_maps_remote_true():
    job = {"title": "X", "hybrid": True}
    assert consider._consider_item(job)["remote"] is True


def test_consider_item_neither_remote_nor_hybrid_is_none():
    job = {"title": "X"}
    assert consider._consider_item(job)["remote"] is None


# ── consider._consider_relevant / _job_fresh ────────────────────────────────
def test_consider_relevant_word_boundary():
    job = {"title": "DEX Engineer", "skills": [{"label": "Rust"}]}
    assert consider._consider_relevant(job, {"dex", "rust"}) is True


def test_consider_relevant_rejects_substring_false_positive():
    job = {"title": "Indexer Engineer", "skills": [{"label": "Trustless systems"}]}
    assert consider._consider_relevant(job, {"dex", "rust"}) is False


def test_consider_job_fresh_no_cutoff_always_true():
    assert consider._job_fresh({}, None) is True


def test_consider_job_fresh_respects_cutoff():
    now = time.time()
    # Relative to now — a hardcoded date silently rots past the cutoff window.
    fresh_iso = datetime.fromtimestamp(now - 86400, tz=timezone.utc).isoformat()
    fresh = {"timeStamp": fresh_iso}
    stale = {"timeStamp": "2020-01-01T00:00:00Z"}
    cutoff = now - 7 * 86400
    assert consider._job_fresh(fresh, cutoff) is True
    assert consider._job_fresh(stale, cutoff) is False


# ── consider pagination (fake client, zero network) ─────────────────────────
class _FakeConsiderResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200            # request_with_retry inspects this

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeConsiderClient:
    """Returns queued pages; each call echoes back a sequence cursor."""
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    async def post(self, url, **kw):
        self.calls += 1
        return _FakeConsiderResponse(self.pages.pop(0))


def test_consider_collect_stops_when_batch_short():
    pages = [{"jobs": [{"title": "A"}, {"title": "B"}], "meta": {"sequence": "s1"}}]
    client = _FakeConsiderClient(pages)
    out = asyncio.run(consider._consider_collect(client, "https://x/api", "slug", {}, None))
    assert len(out) == 2
    assert client.calls == 1


def test_consider_collect_paginates_via_sequence(monkeypatch):
    monkeypatch.setattr(consider, "_CONSIDER_PAGE_SIZE", 2)
    pages = [
        {"jobs": [{"title": "A"}, {"title": "B"}], "meta": {"sequence": "s1"}},
        {"jobs": [{"title": "C"}], "meta": {}},
    ]
    client = _FakeConsiderClient(pages)
    out = asyncio.run(consider._consider_collect(client, "https://x/api", "slug", {}, None))
    assert [j["title"] for j in out] == ["A", "B", "C"]
    assert client.calls == 2


def test_consider_collect_empty_batch_stops():
    client = _FakeConsiderClient([{"jobs": [], "meta": {}}])
    out = asyncio.run(consider._consider_collect(client, "https://x/api", "slug", {}, None))
    assert out == []


# ── jobstash._jobstash_bucket ────────────────────────────────────────────────
def test_jobstash_bucket_rounds_up():
    assert jobstash._jobstash_bucket(1) == "today"
    assert jobstash._jobstash_bucket(5) == "this-week"
    assert jobstash._jobstash_bucket(7) == "this-week"
    assert jobstash._jobstash_bucket(8) == "past-2-weeks"
    assert jobstash._jobstash_bucket(365) is None


# ── jobstash._jobstash_salary / _jobstash_remote ────────────────────────────
def test_jobstash_salary_range():
    job = {"minimumSalary": 160000, "maximumSalary": 210000, "salaryCurrency": "USD"}
    assert jobstash._jobstash_salary(job) == "160k–210k USD"


def test_jobstash_salary_single_value_fallback():
    assert jobstash._jobstash_salary({"salary": 90000, "salaryCurrency": "USD"}) == "90k USD"


def test_jobstash_salary_none_when_missing():
    assert jobstash._jobstash_salary({}) is None


def test_jobstash_remote_mapping():
    assert jobstash._jobstash_remote({"locationType": "REMOTE"}) is True
    assert jobstash._jobstash_remote({"locationType": "ONSITE"}) is False
    assert jobstash._jobstash_remote({"locationType": "HYBRID"}) is False
    assert jobstash._jobstash_remote({}) is None


# ── jobstash._jobstash_item ──────────────────────────────────────────────────
def test_jobstash_item_maps_schema_and_builds_stable_url():
    job = {"title": "Rust Engineer", "organization": {"name": "Acme", "location": "Remote"},
           "location": "Remote", "locationType": "REMOTE",
           "tags": [{"name": "Rust"}, {"name": "Solidity"}],
           "shortUUID": "abc123", "timestamp": 1704067200000}
    item = jobstash._jobstash_item(job)
    assert item["title"] == "Rust Engineer"
    assert item["company"] == "Acme"
    assert item["remote"] is True
    assert item["url"] == "https://jobstash.xyz/jobs/abc123"
    assert item["posted"] == "2024-01-01"
    assert item["tags"] == ["Rust", "Solidity"]


def test_jobstash_item_falls_back_to_url_field_without_shortuuid():
    job = {"title": "X", "url": "https://boards.greenhouse.io/acme/jobs/1"}
    assert jobstash._jobstash_item(job)["url"] == "https://boards.greenhouse.io/acme/jobs/1"


# ── jobstash._jobstash_relevant ──────────────────────────────────────────────
def test_jobstash_relevant_word_boundary():
    job = {"title": "DEX Engineer", "organization": {}, "tags": [{"name": "Rust"}]}
    assert jobstash._jobstash_relevant(job, ["dex", "rust"]) is True


def test_jobstash_relevant_rejects_substring_false_positive():
    job = {"title": "Indexer Engineer", "organization": {}, "tags": [{"name": "Trustless"}]}
    assert jobstash._jobstash_relevant(job, ["dex", "rust"]) is False


# ── jobstash._jobstash_collect (fake client, zero network) ──────────────────
class _FakeJobstashResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeJobstashClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    async def get(self, url, **kw):
        self.calls += 1
        return _FakeJobstashResponse(self.pages.pop(0))


def test_jobstash_collect_paginates_until_total_reached(monkeypatch):
    monkeypatch.setattr(jobstash, "_JOBSTASH_PAGE_SIZE", 1)
    pages = [
        {"data": [{"title": "A", "shortUUID": "a1", "organization": {}}], "total": 2},
        {"data": [{"title": "B", "shortUUID": "b1", "organization": {}}], "total": 2},
    ]
    client = _FakeJobstashClient(pages)
    params = {"limit": 1, "orderBy": "publicationDate", "order": "desc"}
    out = asyncio.run(jobstash._jobstash_collect(client, params, None, []))
    assert [j["title"] for j in out] == ["A", "B"]
    assert client.calls == 2


def test_jobstash_collect_stops_on_empty_page():
    client = _FakeJobstashClient([{"data": [], "total": 0}])
    out = asyncio.run(jobstash._jobstash_collect(
        client, {"limit": 100}, None, []))
    assert out == []


def test_jobstash_collect_drops_stale_and_irrelevant():
    pages = [{"data": [
        {"title": "Fresh Rust Engineer", "shortUUID": "a1", "organization": {}, "timestamp": 2_000_000_000_000},
        {"title": "Stale Rust Engineer", "shortUUID": "b1", "organization": {}, "timestamp": 1},
        {"title": "Sales Manager", "shortUUID": "c1", "organization": {}, "timestamp": 2_000_000_000_000},
    ], "total": 3}]
    client = _FakeJobstashClient(pages)
    out = asyncio.run(jobstash._jobstash_collect(
        client, {"limit": 100}, cutoff_ms=1_000_000_000_000, terms=["rust"]))
    assert [j["title"] for j in out] == ["Fresh Rust Engineer"]
