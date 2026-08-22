"""RemoteOK parser: tag-scoped public JSON API (no browser, no LLM).

remoteok.com's listing is fetched client-side (static HTML ships only nav
chrome); /api?tags=<tag> exposes the same data, and the board's own
`/remote-<tag>-jobs` slug maps straight onto it.

The `tags=` param IS applied server-side, but many listings are boosted/
cross-posted with dozens of unrelated tags purely to surface on every
tag-filtered feed, so a single broad tag also catches generic sales/ops/
support postings unrelated to the profile. We additionally require one of the
profile's own skill terms to appear (in tags, title or company) before
keeping a job.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import httpx

from .common import _epoch_to_date, _profile_query_terms, term_in
from .html import MAX_LINKS, MAX_TEXT_CHARS
from .http import _UA, get_with_retry

_REMOTEOK_API = "https://remoteok.com/api"
_REMOTEOK_MAX_JOBS = 120


def _remoteok_tag_from_url(url: str) -> str | None:
    """RemoteOK tag listing pages are named /remote-<tag>-jobs (e.g.
    /remote-<slug>-jobs); the same tag is the API's `tags` query param.
    Graceful — None (caller falls back to the profile's top term) if the path
    doesn't match."""
    path = urlparse(url).path.strip("/")
    m = re.fullmatch(r"remote-(.+)-jobs", path)

    return m.group(1) if m else None


def _remoteok_salary(job: dict) -> str | None:
    """Compensation range, when disclosed (min/max are 0 for most postings)."""
    lo, hi = job.get("salary_min"), job.get("salary_max")

    if not lo and not hi:
        return None

    fmt = lambda v: f"{int(v) / 1000:.0f}k"

    return f"{fmt(lo)}–{fmt(hi)}" if lo and hi else fmt(lo or hi)


def _remoteok_relevant(job: dict, terms: list[str]) -> bool:
    """True if the job carries one of the profile's skill terms in its tags,
    title or company — not just the (broad) tag used to query the API (see
    module docstring)."""
    tags = job.get("tags")
    hay = " ".join([
        job.get("position") or "", job.get("company") or "",
        " ".join(tags) if isinstance(tags, list) else "",
    ]).lower()

    return any(term_in(t.lower(), hay) for t in terms)


def _remoteok_item(job: dict) -> dict:
    """Maps one RemoteOK API job straight into the parse-vacancy schema."""
    tags = job.get("tags")
    tags = tags[:8] if isinstance(tags, list) else []

    return {
        "title": job.get("position") or "",
        "company": job.get("company") or None,
        "location": job.get("location") or None,
        "remote": True,               # RemoteOK lists remote-only positions
        "salary": _remoteok_salary(job),
        "url": job.get("url") or job.get("apply_url") or "",
        "posted": _epoch_to_date(job.get("epoch")),
        "tags": tags,
    }


async def parse_remoteok(url: str, days: int | None = None) -> dict:
    """The first array element is a legal/metadata banner, not a job —
    skipped. Drops tag-stuffed/off-topic postings (see _remoteok_relevant),
    filters by the freshness window (`epoch`) and caps the result. Returns the
    same shape as fetch_page. Graceful — never raises."""
    cutoff = None if days is None else time.time() - days * 86400
    # Wide term set (must_have + nice_to_have): this is a coarse anti-tag-stuff
    # gate, so recall matters more than precision — the profile-aware prefilter
    # and matcher downstream do the fine relevance judging.
    terms = _profile_query_terms(limit=20, include_nice=True)
    tag = _remoteok_tag_from_url(url) or (terms[0] if terms else None)

    try:
        headers = {"User-Agent": _UA, "Accept": "application/json"}
        params = {"tags": tag} if tag else None

        async with httpx.AsyncClient(headers=headers, timeout=25,
                                     follow_redirects=True) as client:
            r = await get_with_retry(client, _REMOTEOK_API, params=params)
            jobs = r.json()

        if isinstance(jobs, list) and jobs and "legal" in jobs[0]:
            jobs = jobs[1:]                   # drop the metadata banner
        else:
            jobs = jobs if isinstance(jobs, list) else []

        lines, links, items = [], [], []

        for j in jobs:
            if len(items) >= _REMOTEOK_MAX_JOBS:
                break

            if not _remoteok_relevant(j, terms):
                continue

            ts = j.get("epoch")

            if cutoff is not None and isinstance(ts, (int, float)) and ts < cutoff:
                continue

            item = _remoteok_item(j)
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
