"""JobStash parser: public middleware JSON API (no browser, no LLM).

jobstash.xyz aggregates web3 postings across ~6000 organizations — a broad
feed like remoteok.py, not one company's board — so the same relevance-gate
concern applies (an unfiltered pull buries eng roles under everything else).
The HTML listing heuristic in html.py catches mostly filter/facet chrome on
this board (see resources.yaml history); the middleware API is clean.
"""
from __future__ import annotations

import time

import httpx

from .common import _epoch_to_date, _profile_query_terms, term_in
from .html import MAX_LINKS, MAX_TEXT_CHARS
from .http import _UA, get_with_retry

_JOBSTASH_API = "https://middleware.jobstash.xyz/jobs/list"
_JOBSTASH_JOB_URL = "https://jobstash.xyz/jobs/{shortUUID}"
_JOBSTASH_PAGE_SIZE = 100
_JOBSTASH_RAW_CAP = 500     # safety net while paginating an unwindowed board
_JOBSTASH_MAX_JOBS = 120    # final cap after relevance filtering, matches remoteok

# days → coarsest server-side bucket that fully covers the window (round UP;
# the exact cutoff is enforced client-side on `timestamp`, so over-fetch is
# safe — see parse_jobstash).
_BUCKETS = [(1, "today"), (7, "this-week"), (14, "past-2-weeks"),
           (31, "this-month"), (90, "past-3-months"), (180, "past-6-months")]


def _jobstash_bucket(days: int) -> str | None:
    for d, v in _BUCKETS:
        if days <= d:
            return v

    return None                 # >180d: no server bucket, page the whole board


def _jobstash_salary(job: dict) -> str | None:
    lo, hi, cur = job.get("minimumSalary"), job.get("maximumSalary"), job.get("salaryCurrency") or ""
    fmt = lambda v: f"{v / 1000:.0f}k"

    if lo and hi:
        return f"{fmt(lo)}–{fmt(hi)} {cur}".strip()

    if lo or hi:
        return f"{fmt(lo or hi)} {cur}".strip()

    if job.get("salary"):
        return f"{fmt(job['salary'])} {cur}".strip()

    return None


def _jobstash_remote(job: dict) -> bool | None:
    lt = (job.get("locationType") or "").upper()

    if lt == "REMOTE":
        return True

    if lt in ("ONSITE", "HYBRID"):
        return False

    return None


def _jobstash_item(job: dict) -> dict:
    """Maps one JobStash job straight into the parse-vacancy schema (no LLM —
    the API already returns structured fields). Uses the jobstash.xyz detail
    link (stable, always 200) rather than `url`, which is only populated for
    access=="public" jobs."""
    org = job.get("organization") or {}
    tags = [t.get("name") for t in (job.get("tags") or [])
            if isinstance(t, dict) and t.get("name")]
    short = job.get("shortUUID")

    return {
        "title": job.get("title") or "",
        "company": org.get("name") or None,
        "location": job.get("location") or org.get("location"),
        "remote": _jobstash_remote(job),
        "salary": _jobstash_salary(job),
        "url": _JOBSTASH_JOB_URL.format(shortUUID=short) if short else (job.get("url") or ""),
        "posted": _epoch_to_date(job["timestamp"] / 1000) if isinstance(job.get("timestamp"), (int, float)) else None,
        "tags": tags[:8],
    }


def _jobstash_relevant(job: dict, terms: list[str]) -> bool:
    """True if the job carries one of the profile's skill terms in its title,
    company or tags — the board mixes every discipline (ops/sales/eng/...),
    not just engineering. Mirrors remoteok._remoteok_relevant."""
    org = job.get("organization") or {}
    tags = job.get("tags")
    tag_names = " ".join(t.get("name") or "" for t in tags if isinstance(t, dict)) \
        if isinstance(tags, list) else ""
    hay = " ".join([job.get("title") or "", org.get("name") or "", tag_names]).lower()

    return any(term_in(t.lower(), hay) for t in terms)


async def _jobstash_collect(client: httpx.AsyncClient, params: dict,
                            cutoff_ms: float | None, terms: list[str]) -> list[dict]:
    """Pages the middleware API, keeping jobs within the freshness window and
    (if terms are given) matching a profile skill term. Does NOT early-stop on
    the first out-of-window row within a page — see module docstring on
    server ordering — it pages through the whole (bucket-narrowed) result set,
    capped by _JOBSTASH_RAW_CAP raw rows / _JOBSTASH_MAX_JOBS kept rows."""
    out: list[dict] = []
    page, fetched = 1, 0

    while fetched < _JOBSTASH_RAW_CAP and len(out) < _JOBSTASH_MAX_JOBS:
        params["page"] = page
        r = await get_with_retry(client, _JOBSTASH_API, params=params)
        data = r.json()
        batch = data.get("data") or []

        if not batch:
            break

        fetched += len(batch)

        for j in batch:
            if len(out) >= _JOBSTASH_MAX_JOBS:
                break

            ts = j.get("timestamp")

            if cutoff_ms is not None and isinstance(ts, (int, float)) and ts < cutoff_ms:
                continue

            if terms and not _jobstash_relevant(j, terms):
                continue

            out.append(j)

        if page * _JOBSTASH_PAGE_SIZE >= (data.get("total") or 0):
            break                            # last page

        page += 1

    return out


async def parse_jobstash(url: str, days: int | None = None) -> dict:
    """Narrows server-side by a `publicationDate` bucket when `days` is given
    (coarse, calendar-based), enforces the exact cutoff client-side on
    `timestamp` (server ordering is only roughly newest-first — see
    _BUCKETS), then filters by the profile's skill terms (same relevance
    concern as remoteok.parse_remoteok). Returns the same shape as
    fetch_page. Graceful — never raises."""
    cutoff_ms = None if days is None else (time.time() - days * 86400) * 1000
    # Wide term set (must_have + nice_to_have): a coarse anti-noise gate, like
    # remoteok — the profile-aware prefilter/matcher downstream judge fit.
    terms = _profile_query_terms(limit=20, include_nice=True)
    params: dict = {"limit": _JOBSTASH_PAGE_SIZE, "orderBy": "publicationDate", "order": "desc"}

    if days is not None:
        bucket = _jobstash_bucket(days)

        if bucket:
            params["publicationDate"] = bucket

    try:
        headers = {"User-Agent": _UA, "Accept": "application/json"}

        async with httpx.AsyncClient(headers=headers, timeout=25,
                                     follow_redirects=True) as client:
            jobs = await _jobstash_collect(client, params, cutoff_ms, terms)

        lines, links, items = [], [], []

        for j in jobs:
            item = _jobstash_item(j)
            items.append(item)
            parts = [item["title"], item["company"] or "", item["location"] or "",
                     item["posted"] or "", item["url"]]
            lines.append(" | ".join(p for p in parts if p))

            if item["url"]:
                links.append(item["url"])

        return {
            "text": "\n".join(lines)[:MAX_TEXT_CHARS],
            "links": links[:MAX_LINKS],
            "items": items,
        }
    except Exception as e:
        return {"text": "", "links": [], "error": str(e)}
