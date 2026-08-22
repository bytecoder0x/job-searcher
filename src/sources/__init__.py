"""fetch_page dispatcher: routes `render` to the right parser (Python, 0 tokens)."""
from __future__ import annotations

from .a16z import parse_a16z
from .ats import _ATS_FETCHERS, parse_ats
from .consider import parse_consider
from .getro import parse_getro
from .html import MAX_TEXT_CHARS, _extract_links, _extract_text, _fit_records
from .http import _fetch_js, _fetch_static, close_browser
from .jobstash import parse_jobstash
from .remoteok import parse_remoteok

__all__ = [
    "fetch_page", "close_browser",
    "parse_getro", "parse_ats", "parse_a16z", "parse_remoteok",
    "parse_consider", "parse_jobstash",
]


async def fetch_page(url: str, render: str = "static",
                     days: int | None = None) -> dict:
    """Returns {'text': <capped to html.MAX_TEXT_CHARS>, 'links': [...][, 'items', 'error']}.
    Never raises — on error returns empty, so one bad resource doesn't sink
    the whole run.

    `days` is the freshness window. Reliably enforced only for Getro/Consider/
    ATS/a16z/RemoteOK/JobStash (structured timestamps) plus dedup across runs;
    static/JS boards have no reliable timestamps, so the date is passed
    through in `posted` and recency is handled by the matcher on a
    best-effort basis."""
    if render == "getro":
        return await parse_getro(url, days)

    if render == "consider":
        return await parse_consider(url, days)

    if render == "a16z":
        return await parse_a16z(url, days)

    if render == "remoteok":
        return await parse_remoteok(url, days)

    if render == "jobstash":
        return await parse_jobstash(url, days)

    if render in _ATS_FETCHERS:                 # greenhouse | lever | ashby | workable
        return await parse_ats(url, render, days)

    try:
        html = await (_fetch_js(url) if render == "js" else _fetch_static(url))
    except Exception as e:
        return {"text": "", "links": [], "error": str(e)}

    text, dropped = _fit_records(_extract_text(html, url), MAX_TEXT_CHARS)
    result: dict = {"text": text, "links": _extract_links(html, url)}

    if dropped:                                 # postings that didn't fit the budget — reported, not silent
        result["truncated_records"] = dropped

    return result
