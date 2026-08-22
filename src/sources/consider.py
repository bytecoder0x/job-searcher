"""Consider board parser: HTML → fixedBoard slug → search-jobs JSON API.

Consider (consider.com) is Getro's newer platform — a virtualized job list
loaded client-side, so `render: js` catches only the first screenful and
classic getro.py's __NEXT_DATA__ lookup doesn't apply (no numeric network id,
just a string board slug embedded as `window.serverInitialData.fixedBoard`).
Skips the browser and the LLM entirely, same as getro.py.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import httpx

from .common import _epoch_to_date, _iso_to_epoch, _profile_query_terms, term_in
from .html import MAX_LINKS, MAX_TEXT_CHARS
from .http import _UA, _fetch_static, request_with_retry

_CONSIDER_PAGE_SIZE = 100
_CONSIDER_RAW_CAP = 300     # safety net while paginating a board with no date window
_CONSIDER_MAX_JOBS = 120    # final cap after relevance filtering, matches getro/a16z


def _consider_host(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _consider_board_slug(html: str) -> str | None:
    """Pulls the board slug out of `window.serverInitialData.fixedBoard`."""
    m = re.search(r'"fixedBoard"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else None


def _consider_salary(job: dict) -> str | None:
    """Compensation range, when disclosed. Same shape as a16z._a16z_salary,
    but currency/period are nested one level deeper ({"value": ...})."""
    sal = job.get("salary")

    if not isinstance(sal, dict):
        return None

    lo, hi = sal.get("minValue"), sal.get("maxValue")

    if not isinstance(lo, (int, float)) and not isinstance(hi, (int, float)):
        return None

    cur = (sal.get("currency") or {}).get("value") or ""
    period = (sal.get("period") or {}).get("value") or ""
    suffix = f"/{period.lower()}" if period and period.lower() != "year" else ""
    fmt = lambda v: f"{v / 1000:.0f}k"

    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        return f"{fmt(lo)}–{fmt(hi)} {cur}{suffix}".strip()

    return f"{fmt(lo if isinstance(lo, (int, float)) else hi)} {cur}{suffix}".strip()


def _consider_item(job: dict) -> dict:
    """Maps one Consider job straight into the parse-vacancy schema (no LLM —
    the API already returns structured fields)."""
    locs = job.get("locations") or []
    location = ", ".join(locs[:2]) if isinstance(locs, list) and locs else None
    remote = True if job.get("remote") else (True if job.get("hybrid") else None)
    tags = [s.get("label") for s in (job.get("skills") or [])
            if isinstance(s, dict) and s.get("label")]

    return {
        "title": job.get("title") or "",
        "company": job.get("companyName") or None,
        "location": location,
        "remote": remote,
        "salary": _consider_salary(job),
        "url": job.get("url") or job.get("applyUrl") or "",
        "posted": _epoch_to_date(_iso_to_epoch(job.get("timeStamp"))),
        "tags": tags[:8],
    }


def _consider_relevant(job: dict, term_set: set[str]) -> bool:
    hay = " ".join([job.get("title") or ""] +
                   [s.get("label") or "" for s in (job.get("skills") or [])
                    if isinstance(s, dict)]).lower()
    return any(term_in(t, hay) for t in term_set)


def _job_fresh(job: dict, cutoff: float | None) -> bool:
    """Whether the job falls within the freshness window by timeStamp (ISO).
    Graceful: no cutoff or no structured date → treat as fresh."""
    if cutoff is None:
        return True

    ts = _iso_to_epoch(job.get("timeStamp"))

    return ts is None or ts >= cutoff


async def _consider_page(client: httpx.AsyncClient, api: str, slug: str,
                         query: dict, sequence: str | None) -> tuple[list[dict], str | None]:
    """One request to the search-jobs API. Returns (jobs, next sequence cursor)."""
    meta: dict = {"size": _CONSIDER_PAGE_SIZE}

    if sequence:
        meta["sequence"] = sequence

    body = {"meta": meta, "board": {"id": slug, "isParent": True},
            "query": query, "grouped": False}
    r = await request_with_retry(client, "POST", api, json=body)
    data = r.json()

    return data.get("jobs") or [], (data.get("meta") or {}).get("sequence")


async def _consider_collect(client: httpx.AsyncClient, api: str, slug: str,
                            query: dict, cutoff: float | None) -> list[dict]:
    """All positions within the freshness window, cursor-paginated (board
    returns newest first), capped by _CONSIDER_RAW_CAP."""
    out: list[dict] = []
    sequence = None

    while len(out) < _CONSIDER_RAW_CAP:
        batch, sequence = await _consider_page(client, api, slug, query, sequence)

        if not batch:
            break

        fresh = [j for j in batch if _job_fresh(j, cutoff)]
        out.extend(fresh)

        if not fresh or not sequence or len(batch) < _CONSIDER_PAGE_SIZE:
            break

    return out[:_CONSIDER_RAW_CAP]


async def parse_consider(url: str, days: int | None = None) -> dict:
    """Filters server-side by `postedSince` (ISO-8601 duration) when `days` is
    given, then narrows further by the profile's must_have skills (same
    relevance concern as getro.parse_getro — the board mixes eng/ops/trading
    roles across ~1000 portfolio companies). Returns the same shape as
    fetch_page. Graceful — never raises."""
    cutoff = None if days is None else time.time() - days * 86400

    try:
        host = _consider_host(url)
        html = await _fetch_static(f"{host}/jobs")
        slug = _consider_board_slug(html)

        if not slug:
            return {"text": "", "links": [], "error": "consider board slug not found"}

        api = f"{host}/api-boards/search-jobs"
        query = {"postedSince": f"P{days}D"} if days else {}
        headers = {"User-Agent": _UA, "Accept": "application/json",
                  "Content-Type": "application/json"}

        async with httpx.AsyncClient(headers=headers, timeout=25) as client:
            jobs = await _consider_collect(client, api, slug, query, cutoff)

        terms = _profile_query_terms()

        if terms:
            term_set = {t.lower() for t in terms}
            relevant = [j for j in jobs if _consider_relevant(j, term_set)]

            if len(relevant) >= 5:          # else keep the unfiltered (freshness) list
                jobs = relevant

        jobs = jobs[:_CONSIDER_MAX_JOBS]
        lines, links, items = [], [], []

        for j in jobs:
            item = _consider_item(j)
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
