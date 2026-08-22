"""ATS public JSON APIs: Greenhouse / Lever / Ashby / Workable (no browser, no LLM).

Configure with `render: greenhouse | lever | ashby | workable` and the
standard board URL; the board token is taken from the URL (first path
segment).
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

from .common import _epoch_to_date, _iso_to_epoch, term_in
from .html import MAX_LINKS, MAX_TEXT_CHARS
from .http import _UA, get_with_retry

# ATS boards are catalogs (finite, dedup-protected, and the sonnet stage is
# separately bounded by config.matcher_input_cap). A low cap cut the API's
# alphabetical tail — real senior roles on big boards like Coinbase/Ripple — so
# it's set generously; relevance-ranking below decides WHICH survive if a board
# ever exceeds it, and the trim is reported (never silent).
_ATS_MAX_JOBS = 250


def _profile_terms() -> list[str]:
    """Lowercased skill tokens from profile.yaml (must_have + nice_to_have),
    split on '/' like Getro's term builder. Empty on any error — the caller
    then keeps API order (the old behaviour)."""
    try:
        from .. import config
        skills = config.load_profile().get("skills") or {}
    except Exception:
        return []

    out: set[str] = set()

    for group in ("must_have", "nice_to_have"):
        for entry in skills.get(group) or []:
            for part in str(entry).split("/"):
                t = part.strip().lower()

                if t:
                    out.add(t)

    return list(out)


def _relevance(job: dict, terms: list[str]) -> int:
    """Cheap 0-token relevance score: how many profile skill tokens appear in
    the title/tags/location. Used only to decide WHICH jobs survive the cap on a
    big catalog — so a 300-role board keeps its profile-matching roles
    instead of whatever sorts first alphabetically. Word-boundary match, so
    short tokens don't false-positive (`dex` inside `indexer`, `rust` inside
    `trustless`)."""
    blob = " ".join(str(x) for x in (
        job.get("title") or "", job.get("location") or "",
        *(job.get("tags") or []))).lower()
    return sum(1 for t in terms if term_in(t, blob))


def _cap_by_relevance(jobs: list[dict]) -> tuple[list[dict], int]:
    """Keep the _ATS_MAX_JOBS most profile-relevant jobs, returning
    (kept, dropped). A company ATS board is a CATALOG with no freshness window,
    so a blind `[:120]` in the API's (alphabetical) order permanently loses the
    tail — on Coinbase/Ripple that's real senior engineering roles. Ranking by
    skill overlap first means the cap trims the least-relevant, not the
    alphabetically-last."""
    if len(jobs) <= _ATS_MAX_JOBS:
        return jobs, 0

    terms = _profile_terms()

    if terms:
        # stable sort: equal-relevance jobs keep their API order
        jobs = sorted(jobs, key=lambda j: _relevance(j, terms), reverse=True)

    return jobs[:_ATS_MAX_JOBS], len(jobs) - _ATS_MAX_JOBS


def _ats_token(url: str) -> str | None:
    """Board/company token from a standard ATS board URL (first path segment).
    e.g. jobs.lever.co/<co>, boards.greenhouse.io/<co>, jobs.ashbyhq.com/<co>."""
    segs = [s for s in urlparse(url).path.split("/") if s]
    return segs[0] if segs else None


def _amount_k(v: object) -> str | None:
    try:
        return f"{int(v) / 1000:.0f}k"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def _ats_greenhouse(client: httpx.AsyncClient, token: str,
                          cutoff: float | None) -> list[dict]:
    api = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false"
    r = await get_with_retry(client, api)
    company = token.replace("-", " ").title()
    out = []

    for j in r.json().get("jobs") or []:
        ts = _iso_to_epoch(j.get("updated_at"))

        if cutoff is not None and ts is not None and ts < cutoff:
            continue

        loc = (j.get("location") or {}).get("name")
        out.append({
            "title": j.get("title") or "",
            "company": company,
            "location": loc,
            "remote": True if loc and "remote" in loc.lower() else None,
            "salary": None,                     # Greenhouse rarely exposes pay
            "url": j.get("absolute_url") or "",
            "posted": _epoch_to_date(ts),
            "tags": [],
        })

    return out


async def _ats_lever(client: httpx.AsyncClient, token: str,
                     cutoff: float | None) -> list[dict]:
    api = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = await get_with_retry(client, api)
    company = token.replace("-", " ").title()
    out = []

    for j in r.json() or []:
        raw = j.get("createdAt")
        ts = raw / 1000 if isinstance(raw, (int, float)) else None

        if cutoff is not None and ts is not None and ts < cutoff:
            continue

        cats = j.get("categories") or {}
        loc = cats.get("location")
        wt = (cats.get("workplaceType") or "").lower()
        sr = j.get("salaryRange") or {}
        lo, hi = _amount_k(sr.get("min")), _amount_k(sr.get("max"))
        cur = sr.get("currency") or ""
        salary = f"{lo}–{hi} {cur}".strip() if lo and hi else ((lo or hi) and f"{lo or hi} {cur}".strip())
        out.append({
            "title": j.get("text") or "",
            "company": company,
            "location": loc,
            "remote": True if wt == "remote" or (loc and "remote" in loc.lower()) else None,
            "salary": salary or None,
            "url": j.get("hostedUrl") or "",
            "posted": _epoch_to_date(ts),
            "tags": [cats.get("team")] if cats.get("team") else [],
        })

    return out


async def _ats_ashby(client: httpx.AsyncClient, token: str,
                     cutoff: float | None) -> list[dict]:
    api = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    r = await get_with_retry(client, api)
    data = r.json()
    company = data.get("organizationName") or token.replace("-", " ").title()
    out = []

    for j in data.get("jobs") or []:
        ts = _iso_to_epoch(j.get("publishedAt") or j.get("publishedDate"))

        if cutoff is not None and ts is not None and ts < cutoff:
            continue

        comp = j.get("compensation") or {}
        salary = comp.get("compensationTierSummary") or j.get("compensationTierSummary")
        tags = [t for t in (j.get("department"), j.get("team")) if t]
        out.append({
            "title": j.get("title") or "",
            "company": company,
            "location": j.get("location"),
            "remote": bool(j.get("isRemote")) or None,
            "salary": salary if isinstance(salary, str) and salary else None,
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "posted": _epoch_to_date(ts),
            "tags": tags,
        })

    return out


async def _ats_workable(client: httpx.AsyncClient, token: str,
                        cutoff: float | None) -> list[dict]:
    api = f"https://apply.workable.com/api/v1/widget/accounts/{token}"
    r = await get_with_retry(client, api)
    data = r.json()
    company = data.get("name") or token.replace("-", " ").title()
    out = []

    for j in data.get("jobs") or []:
        ts = _iso_to_epoch(j.get("published_on") or j.get("created_at"))

        if cutoff is not None and ts is not None and ts < cutoff:
            continue

        loc = ", ".join(p for p in (j.get("city"), j.get("state"), j.get("country")) if p)
        out.append({
            "title": j.get("title") or "",
            "company": company,
            "location": loc or None,
            "remote": bool(j.get("telecommuting")) or None,
            "salary": None,                     # the public widget doesn't expose pay
            "url": j.get("shortlink") or j.get("url") or "",
            "posted": _epoch_to_date(ts),
            "tags": [j.get("department")] if j.get("department") else [],
        })

    return out


_ATS_FETCHERS = {
    "greenhouse": _ats_greenhouse,
    "lever": _ats_lever,
    "ashby": _ats_ashby,
    "workable": _ats_workable,
}


async def parse_ats(url: str, family: str, days: int | None = None) -> dict:
    """Hits the public JSON API and maps straight into the parse-vacancy
    schema (no browser, no LLM). Returns the same shape as fetch_page.
    Graceful — never raises."""
    cutoff = None if days is None else time.time() - days * 86400
    token = _ats_token(url)

    if not token:
        return {"text": "", "links": [], "error": f"{family}: no token in url"}

    fetcher = _ATS_FETCHERS.get(family)

    if not fetcher:
        return {"text": "", "links": [], "error": f"unknown ATS family: {family}"}

    try:
        headers = {"User-Agent": _UA, "Accept": "application/json"}

        async with httpx.AsyncClient(headers=headers, timeout=25,
                                     follow_redirects=True) as client:
            jobs, capped = _cap_by_relevance(await fetcher(client, token, cutoff))

        lines, links, items = [], [], []

        for j in jobs:
            parts = [j.get("title") or "", j.get("company") or "",
                     j.get("location") or "", j.get("posted") or "",
                     j.get("url") or ""]
            lines.append(" | ".join(p for p in parts if p))

            if j.get("url"):
                links.append(j["url"])

            items.append(j)

        result = {
            "text": "\n".join(lines)[:MAX_TEXT_CHARS],
            "links": links[:MAX_LINKS],
            "items": items,
        }

        if capped:                              # least-relevant roles trimmed at the cap — reported
            result["capped"] = capped

        return result
    except Exception as e:
        return {"text": "", "links": [], "error": str(e)}
