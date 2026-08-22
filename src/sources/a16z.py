"""a16z crypto portfolio-jobs parser: inline JSON in the static page (no browser, no LLM).

a16zcrypto.com/jobs/ inlines a `portfolioJobs` JS array (for an Alpine.js
widget) with the full listing across ~50 portfolio companies — a plain HTTP
fetch, mapped straight into the parse-vacancy schema.
"""
from __future__ import annotations

import json
import re
import time

from .common import _epoch_to_date, _iso_to_epoch, _profile_query_terms, term_in
from .html import MAX_LINKS, MAX_TEXT_CHARS
from .http import _fetch_static

_A16Z_MAX_JOBS = 120


def _a16z_extract_portfolio_jobs(html: str) -> list[dict] | None:
    """Pulls the inline `const portfolioJobs = [...]` array out of the page's
    <script>. Returns None if the page's structure changed and the variable is
    gone — caller degrades to an empty result rather than raising."""
    m = re.search(r"const portfolioJobs\s*=\s*(\[.*?\]);", html, re.S)

    if not m:
        return None

    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _a16z_salary(job: dict) -> str | None:
    """Compensation range, when disclosed (most a16z postings don't). Same
    shape as getro._getro_salary."""
    sal = job.get("salary")

    if not isinstance(sal, dict):
        return None

    lo, hi = sal.get("minValue"), sal.get("maxValue")

    if not isinstance(lo, (int, float)) and not isinstance(hi, (int, float)):
        return None

    cur = sal.get("currency") or ""
    period = sal.get("period") or ""
    suffix = f"/{period.lower()}" if period and period.lower() != "year" else ""
    fmt = lambda v: f"{v / 1000:.0f}k"

    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        return f"{fmt(lo)}–{fmt(hi)} {cur}{suffix}".strip()

    return f"{fmt(lo if isinstance(lo, (int, float)) else hi)} {cur}{suffix}".strip()


def _a16z_item(job: dict, company: str) -> dict:
    """Maps one a16z portfolio job straight into the parse-vacancy schema."""
    locs = job.get("locations") or []
    location = locs[0] if locs else None
    tags = job.get("skills")
    tags = tags[:8] if isinstance(tags, list) else []

    return {
        "title": job.get("title") or "",
        "company": job.get("companyName") or company or None,
        "location": location,
        "remote": bool(job.get("remote")) or None,
        "salary": _a16z_salary(job),
        "url": job.get("url") or "",
        "posted": _epoch_to_date(_iso_to_epoch(job.get("createdAt"))),
        "tags": tags,
    }


def _a16z_relevant(job: dict, term_set: set[str]) -> bool:
    hay = " ".join([job.get("title") or ""] + (job.get("skills") or [])).lower()
    return any(term_in(t, hay) for t in term_set)


async def parse_a16z(url: str, days: int | None = None) -> dict:
    """Filters by the freshness window and (same relevance concern as
    getro.parse_getro — a broad board otherwise buries eng roles under
    ops/trading/marketing) by the profile's must_have skills. Returns the
    same shape as fetch_page. Graceful — never raises."""
    cutoff = None if days is None else time.time() - days * 86400

    try:
        html = await _fetch_static(url)
        companies = _a16z_extract_portfolio_jobs(html)

        if not companies:
            return {"text": "", "links": [], "error": "portfolioJobs not found"}

        jobs = []

        for c in companies:
            company = c.get("company") or ""

            for j in c.get("jobs") or []:
                ts = _iso_to_epoch(j.get("createdAt"))

                if cutoff is not None and ts is not None and ts < cutoff:
                    continue

                jobs.append((j, company))

        jobs.sort(key=lambda pair: _iso_to_epoch(pair[0].get("createdAt")) or 0, reverse=True)
        terms = _profile_query_terms()

        if terms:
            term_set = {t.lower() for t in terms}
            relevant = [pair for pair in jobs if _a16z_relevant(pair[0], term_set)]

            if len(relevant) >= 5:               # else keep the unfiltered (freshness-sorted) list
                jobs = relevant

        jobs = jobs[:_A16Z_MAX_JOBS]
        lines, links, items = [], [], []

        for j, company in jobs:
            item = _a16z_item(j, company)
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
