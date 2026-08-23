"""Auto-detect how to read an arbitrary job-board URL — which `render` handler
(greenhouse/lever/ashby/workable/getro/static/js) actually yields postings — so
a user can /addsource any board without hand-configuring resources.yaml. All
probing is live fetch (0 tokens); the first handler that returns >0 wins."""
from __future__ import annotations

from urllib.parse import urlparse

from .sources import fetch_page

# ATS host → render family (the board token is the first path segment).
_ATS_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
}


def _ats_family(url: str) -> str | None:
    return _ATS_HOSTS.get(urlparse(url).netloc.lower())


def _record_count(page: dict) -> int:
    txt = page.get("text") or ""
    return len([l for l in txt.split("\n")
                if l.lstrip().startswith("- ") and " | http" in l])


def _yield(page: dict) -> int:
    items = page.get("items")
    return len(items) if items is not None else _record_count(page)


_NAME_SKIP_SEGS = {"jobs", "job", "careers", "career", "search", "positions"}


def _default_name(url: str) -> str:
    """A readable source name: the board token from the path (jobs.lever.co/acme
    → 'Acme') if present, else the host label (web3.career → 'Web3')."""
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s and s.lower() not in _NAME_SKIP_SEGS]
    base = segs[0] if segs else p.netloc.replace("www.", "").split(".")[0]
    base = base.replace("-", " ").replace("_", " ").strip()

    return (base[:1].upper() + base[1:]) if base else p.netloc


async def detect_source(url: str, category: str = "job",
                        name: str | None = None) -> dict | None:
    """Probe render handlers in order of specificity and return the first that
    yields postings as a resource dict (name/url/category/render/enabled/notes/
    detected_count), or None if none worked. Most-specific/structured-first —
    a known ATS host, then Getro (structured JSON), then a plain static fetch,
    then a full JS render (the genuinely expensive one) — not strictly
    cheapest-first: the Getro probe does a full static fetch (plus API POSTs),
    and a non-Getro board's static fetch is then deliberately re-done as the
    next probe."""
    url = url.strip()
    name = name or _default_name(url)
    order: list[str] = []
    fam = _ats_family(url)

    if fam:
        order.append(fam)

    order += ["getro", "static", "js"]

    tried: set[str] = set()

    for render in order:
        if render in tried:
            continue

        tried.add(render)

        try:
            page = await fetch_page(url, render=render, days=None)
        except Exception:
            continue

        if page.get("error"):
            continue

        n = _yield(page)

        if n > 0:
            return {
                "name": name, "url": url, "category": category, "render": render,
                "enabled": True, "detected_count": n,
                "notes": f"auto-detected render={render} ({n} postings) on add",
            }

    return None
