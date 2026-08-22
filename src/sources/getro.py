"""Getro/Consider board parser: HTML → network.id → JSON API → condensed listing.

Many jobs.<vc>.xyz sites are Getro networks: jobs are pulled from a public API,
not the initial HTML. Skips the browser and the LLM entirely.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .common import _profile_query_terms
from .html import MAX_LINKS, MAX_TEXT_CHARS
from .http import _UA, _fetch_static, request_with_retry

_GETRO_API = "https://api.getro.com/api/v2/collections/{nid}/search/jobs"
_GETRO_PER_PAGE = 20        # the API returns at most 20 positions per page
_GETRO_MAX_JOBS = 120       # safety cap so a large board can't paginate forever


def _getro_network_id(html: str) -> int | None:
    """Extracts network.id from a Getro board page's __NEXT_DATA__."""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S)

    if not m:
        return None

    try:
        data = json.loads(m.group(1))
        nid = data["props"]["pageProps"]["network"]["id"]

        return int(nid)
    except Exception:
        return None


def _getro_line(job: dict) -> str:
    """One line of the condensed listing: title | company | location | date | url."""
    org = job.get("organization") or {}
    company = org.get("name") or ""
    locs = job.get("locations") or job.get("searchable_locations") or []
    location = ", ".join(locs[:2]) if isinstance(locs, list) else str(locs)
    mode = job.get("work_mode") or ""
    ts = job.get("created_at")
    posted = ""

    if isinstance(ts, (int, float)):
        try:
            posted = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        except Exception:
            posted = ""

    parts = [job.get("title") or "", company, location, mode, posted,
             job.get("url") or ""]

    return " | ".join(p for p in parts if p)


def _getro_salary(job: dict) -> str | None:
    """Compensation range, when Getro provides structured amounts (in cents).
    Graceful: no amounts disclosed → None."""
    lo = job.get("compensation_amount_min_cents")
    hi = job.get("compensation_amount_max_cents")

    if not isinstance(lo, (int, float)) and not isinstance(hi, (int, float)):
        return None

    cur = job.get("compensation_currency") or ""
    fmt = lambda cents: f"{cents / 100_000:.0f}k"      # cents → thousands of currency units

    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        return f"{fmt(lo)}–{fmt(hi)} {cur}".strip()

    return f"{fmt(lo if isinstance(lo, (int, float)) else hi)} {cur}".strip()


def _getro_item(job: dict) -> dict:
    """Maps one Getro job straight into the parse-vacancy schema (no LLM —
    the API already returns structured fields)."""
    org = job.get("organization") or {}
    locs = job.get("locations") or job.get("searchable_locations") or []
    location = ", ".join(locs[:2]) if isinstance(locs, list) and locs else None
    mode = (job.get("work_mode") or "").lower()
    remote = True if "remote" in mode else None    # otherwise None — work_mode doesn't guarantee "not remote"
    posted = None
    ts = job.get("created_at")

    if isinstance(ts, (int, float)):
        try:
            posted = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        except Exception:
            posted = None

    tags = job.get("skills")
    tags = tags[:8] if isinstance(tags, list) else []

    return {
        "title": job.get("title") or "",
        "company": org.get("name") or None,
        "location": location,
        "remote": remote,
        "salary": _getro_salary(job),
        "url": job.get("url") or "",
        "posted": posted,
        "tags": tags,
    }


def _job_fresh(job: dict, cutoff: float | None) -> bool:
    """Whether the job falls within the freshness window by created_at (unix).
    Graceful: no cutoff or no structured date → treat as fresh."""
    if cutoff is None:
        return True

    ts = job.get("created_at")

    if not isinstance(ts, (int, float)):
        return True

    return ts >= cutoff


async def _getro_search(client: httpx.AsyncClient, api: str,
                        query: str, page: int = 0) -> list[dict]:
    """One request to the Getro API. Returns a list of positions (may be empty)."""
    r = await request_with_retry(client, "POST", api, json={
        "hitsPerPage": _GETRO_PER_PAGE, "page": page, "query": query})
    return (r.json().get("results") or {}).get("jobs") or []


async def _getro_collect_term(client: httpx.AsyncClient, api: str,
                              query: str, cutoff: float | None) -> list[dict]:
    """All positions for one query within the freshness window. Paginates
    while pages keep returning fresh positions (board returns newest first),
    capped by _GETRO_MAX_JOBS."""
    out: list[dict] = []
    page = 0

    while len(out) < _GETRO_MAX_JOBS:
        batch = await _getro_search(client, api, query, page)

        if not batch:
            break

        fresh = [j for j in batch if _job_fresh(j, cutoff)]
        out.extend(fresh)

        if not fresh:                       # everything after this is older than the window
            break

        if len(batch) < _GETRO_PER_PAGE:    # last page
            break

        page += 1

    return out[:_GETRO_MAX_JOBS]


def _merge_jobs(lists: list[list[dict]], cap: int) -> list[dict]:
    """Round-robin merge across terms (one position from each per round),
    deduped by url, so every skill contributes instead of one broad term
    filling up the whole list."""
    seen: set[str] = set()
    out: list[dict] = []
    i = 0

    while len(out) < cap:
        progressed = False

        for lst in lists:
            if i < len(lst):
                progressed = True
                job = lst[i]
                u = job.get("url")

                if u and u not in seen:
                    seen.add(u)
                    out.append(job)

                    if len(out) >= cap:
                        break

        if not progressed:
            break

        i += 1

    return out


async def parse_getro(url: str, days: int | None = None) -> dict:
    """Filters server-side by profile skills (must_have) to surface relevant
    eng roles instead of the latest trading/compliance postings. `days` is
    enforced here via Getro's structured created_at. Returns the same shape
    as fetch_page. Graceful — never raises."""
    cutoff = None if days is None else time.time() - days * 86400

    try:
        html = await _fetch_static(url)
        nid = _getro_network_id(html)

        if not nid:
            return {"text": "", "links": [], "error": "getro network id not found"}

        api = _GETRO_API.format(nid=nid)
        headers = {
            "User-Agent": _UA, "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": f"https://{urlparse(url).netloc}",
            "Referer": url,
        }
        terms = _profile_query_terms()

        async with httpx.AsyncClient(headers=headers, timeout=25) as client:
            jobs: list[dict] = []

            if terms:                                   # filter by skills
                lists = []

                for t in terms:
                    try:                                # all fresh positions for the term
                        lists.append(await _getro_collect_term(client, api, t, cutoff))
                    except Exception:
                        continue                        # one bad term shouldn't sink the rest

                jobs = _merge_jobs(lists, _GETRO_MAX_JOBS)

            # fallback: terms yielded nothing (small board or empty profile) —
            # take an unfiltered listing within the same window
            if len(jobs) < 5:
                seen = {j.get("url") for j in jobs}

                for j in await _getro_collect_term(client, api, "", cutoff):
                    u = j.get("url")

                    if u and u not in seen:
                        seen.add(u)
                        jobs.append(j)

                jobs = jobs[:_GETRO_MAX_JOBS]

        lines, links, items = [], [], []

        for j in jobs:
            lines.append(_getro_line(j))
            ju = j.get("url")

            if ju:
                links.append(ju)

            try:                                # one bad job shouldn't sink the whole run
                items.append(_getro_item(j))
            except Exception:
                continue

        return {
            "text": "\n".join(lines)[:MAX_TEXT_CHARS],
            "links": links[:MAX_LINKS],
            "items": items,
        }
    except Exception as e:
        return {"text": "", "links": [], "error": str(e)}
