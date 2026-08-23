"""Deterministic (0-token) filtering: freeform-date parsing, freshness window,
hard keyword/field filter, and source/category stamping."""
from __future__ import annotations

import datetime
import re
from urllib.parse import urlparse

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
# relative age like "8w", "4d ago", "2 months ago" — longer units first so
# "month" wins over "mo"/"m" in the alternation.
_REL_AGE = re.compile(
    r"(\d+)\s*(minute|min|hour|hr|day|week|month|mon|mo|year|yr|d|w|h|y)s?\b", re.I)


def _parse_posted(posted: str, today: datetime.date) -> datetime.date | None:
    """Best-effort parse of a board's freeform `posted` string (ISO, relative
    age, today/yesterday, month-name dates) into a date. None only when nothing
    recognizable is found — callers keep those (can't prove staleness)."""
    s = posted.strip().lower()

    if not s:
        return None

    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)

    if m:
        try:
            return datetime.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None

    if s in ("today", "now", "just now", "just posted", "new"):
        return today

    if s.startswith("yesterday"):
        return today - datetime.timedelta(days=1)

    m = _REL_AGE.search(s)

    if m:
        n, unit = int(m[1]), m[2]

        if unit in ("minute", "min", "hour", "hr", "h"):
            return today                        # same day → fresh

        if unit in ("day", "d"):
            return today - datetime.timedelta(days=n)

        if unit in ("week", "w"):
            return today - datetime.timedelta(weeks=n)

        if unit in ("month", "mon", "mo"):
            return today - datetime.timedelta(days=30 * n)

        if unit in ("year", "yr", "y"):
            return today - datetime.timedelta(days=365 * n)

    # month-name date, either order, with an optional trailing 4-digit year:
    # "Jun 18" / "18 Jun" / "Jun 18, 2024" / "12 Jul 2024". `(?!\d)` on the day
    # stops a 4-digit year from being swallowed as a 1-2 digit day.
    mn = re.search(r"([a-z]{3,9})\s+(\d{1,2})(?!\d)(?:\D{0,4}(\d{4})\b)?", s)
    dm = re.search(r"(\d{1,2})(?!\d)\s+([a-z]{3,9})(?:\D{0,4}(\d{4})\b)?", s)
    mon = day = year = None

    if mn and mn[1][:3] in _MONTHS:
        mon, day = _MONTHS[mn[1][:3]], int(mn[2])
        year = int(mn[3]) if mn[3] else None
    elif dm and dm[2][:3] in _MONTHS:
        mon, day = _MONTHS[dm[2][:3]], int(dm[1])
        year = int(dm[3]) if dm[3] else None

    if mon:
        try:
            d = datetime.date(year or today.year, mon, day)
        except ValueError:
            return None

        # Only roll back an inferred (yearless) date; an explicit year is trusted as-is.
        if year is None and d > today:          # e.g. "Dec 20" seen in July → last year
            d = datetime.date(today.year - 1, mon, day)

        return d

    return None


def _within_window(items: list[dict], days: int) -> list[dict]:
    """Freshness-window filter on extracted positions. Drops anything whose
    `posted` parses to a date older than the window; keeps fresh ones and those
    with no recognizable date (better to keep than to lose a relevant one).
    Getro/ATS already filter server-side by structured timestamps — this covers
    the freeform-date boards (8w, Jun 18, 4d ago) that would otherwise slip
    through and stale up a 'last N days' digest."""
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days)
    kept = []

    for it in items:
        d = _parse_posted(str(it.get("posted") or ""), today)

        if d is not None and d < cutoff:
            continue                            # parseable and stale → drop

        kept.append(it)

    return kept


def _hard_filter(items: list[dict], profile: dict) -> list[dict]:
    """Cheap Python-only filter before the expensive matching step: drops
    exclude.keyword hits (whole-word, so "intern" doesn't match "International"
    or "Internet Computer") and malformed entries missing a title/URL.
    Category (which resources got fetched) and dedup (the store) are enforced
    upstream, before this runs. Seniority and work-format are deliberately
    NOT hard-filtered here — a heuristic pre-model drop risks a false negative
    (e.g. seniority only stated in the JD body); the matcher judges those with
    full context (see matching-rules.md §1-2)."""
    excl = (profile.get("exclude") or {}).get("keywords") or []
    excl = [w.lower() for w in excl]
    kept = []

    for it in items:
        blob = " ".join(str(it.get(k, "")) for k in ("title", "company", "tags")).lower()

        if any(re.search(rf"\b{re.escape(w)}\b", blob) for w in excl):
            continue

        if not it.get("title") or not it.get("url"):
            continue

        kept.append(it)

    return kept


# parse-vacancy schema keys — anything else the model invents is dropped.
_SCHEMA_KEYS = ("title", "company", "location", "remote", "salary", "url",
                "posted", "tags", "source", "category")
_MAX_TAGS = 8


def _norm_url(u: str) -> str:
    """host+path, lowercased host, no trailing slash, query/fragment dropped —
    so grounding tolerates a query-string or trailing-slash difference between a
    candidate link and the model's copy of it, but not a different path."""
    p = urlparse(u)
    return f"{p.netloc.lower()}{p.path.rstrip('/')}"


def _validate_items(items: list[dict], links: list[str],
                    stats: dict | None = None) -> list[dict]:
    """Deterministic post-extraction validation (0 tokens).

    A cheap model will occasionally invent a field, emit a non-list `tags`, or
    hallucinate a URL that was never on the page — none of which the prompt can
    fully prevent. Enforcing the contract in Python is free and reliable: drop
    entries without a usable title/URL, keep only schema keys, coerce types, and
    require the URL itself (host+path, see _norm_url) to be one we actually saw
    in the candidate links — a same-host but invented PATH is still a dead link,
    worse than no position. `links` is the full set of URLs seen on the page
    (candidate links ∪ URLs embedded in the page text), so path-grounding does
    not false-drop a real posting that fell past the candidate-link cap.
    Optional `stats` is filled with kept/dropped-by-reason counts for logging
    (return type unchanged, so callers/tests that ignore it are unaffected)."""
    known = {_norm_url(u) for u in links if u}
    known.discard("")
    out, seen = [], set()
    drop = {"no_title_or_url": 0, "off_listing_url": 0, "duplicate": 0}

    for it in items:
        if not isinstance(it, dict):
            drop["no_title_or_url"] += 1
            continue

        title = str(it.get("title") or "").strip()
        url = str(it.get("url") or "").strip()

        if not title or not url.lower().startswith(("http://", "https://")):
            drop["no_title_or_url"] += 1
            continue

        # Enforce grounding only when we had candidates to compare against;
        # structured sources (Getro/ATS) legitimately arrive with no link list.
        if known and _norm_url(url) not in known:
            drop["off_listing_url"] += 1
            continue

        key = (title.lower(), str(it.get("company") or "").strip().lower())

        if key in seen:
            drop["duplicate"] += 1
            continue

        seen.add(key)
        clean = {k: it.get(k) for k in _SCHEMA_KEYS if k in it}
        clean["title"], clean["url"] = title, url
        tags = clean.get("tags")
        clean["tags"] = [str(t) for t in tags][:_MAX_TAGS] if isinstance(tags, list) else []

        if not isinstance(clean.get("remote"), bool):
            clean["remote"] = None

        out.append(clean)

    if stats is not None:
        stats["kept"] = len(out)
        stats["dropped"] = drop

    return out


def _stamp_source(items: list[dict], resource: dict) -> list[dict]:
    """Sets source/category on items that don't already have them (no overwrite)."""
    for it in items:
        it.setdefault("source", resource["name"])
        it.setdefault("category", resource["category"])

    return items
