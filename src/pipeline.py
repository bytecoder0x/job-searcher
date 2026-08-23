"""Search orchestration: fetch → window → dedup/hard-filter → prefilter → match →
digest. Deterministic steps in Python (0 tokens); two isolated LLM calls
(extract=haiku, match=sonnet) on already-condensed data — this is how we save
tokens. Each query() call is a fresh session, so pages don't accumulate in history.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from . import config, store
from .digest import _empty_reason, _render_digest
from .filters import _hard_filter, _stamp_source, _within_window
from .progress import ProgressCB, _emit, _heartbeat
from .sources import fetch_page
# LLM-step helpers (extract/prefilter/match) live in steps.py; re-exported here
# so run_search and existing callers/tests can keep using pipeline.<name>.
from .steps import (  # noqa: F401
    _EXTRACT_CHUNK, _MATCH_CHUNK, _PREFILTER_CHUNK,
    _extract_call, _extract_items, _match_rank, _prefilter,
)

log = logging.getLogger("job-searcher")


def _judged_items(fresh: list[dict], kept: list[dict],
                  overflow: list[dict], scored: list[dict]) -> list[dict]:
    """Which fresh positions this run actually JUDGED — the only ones to mark
    seen (D11: seen = judged). Prefiltered-out items were judged by the
    prefilter; a kept item was judged only if the scorer returned a score for it (matched by dedup
    key). A failed or partial matcher chunk leaves its inputs UNSCORED — those
    must NOT be marked seen, or they vanish for 30 days without ever being
    scored; excluding them gives them another chance next run. Ceiling overflow
    was never looked at either → also excluded. Key drift (matcher rewrote a
    title) fails safe: the item looks unscored and simply retries."""
    kept_ids = {id(it) for it in kept}
    overflow_ids = {id(it) for it in overflow}
    scored_keys = {store.key_of(j) for j in scored}
    judged = []

    for it in fresh:
        if id(it) in overflow_ids:
            continue                            # never scored → free retry next run

        if id(it) in kept_ids and store.key_of(it) not in scored_keys:
            continue                            # reached the scorer but got no score → retry

        judged.append(it)                       # prefiltered-out (judged) or scored

    return judged


# A source is "degraded" when this run's yield fell to less than this fraction
# of its recent median, on a baseline big enough to be meaningful. Zero yield is
# NOT flagged here — it's already reported by no_data with a specific reason.
_DEGRADE_FRACTION = 0.4
_DEGRADE_MIN_BASELINE = 5


def _degraded_sources(yields: dict[str, int]) -> list[dict]:
    """Sources whose yield collapsed relative to their own history — the signal
    a board half-broke silently (still returns data, just far less). Pure
    Python, 0 tokens; skips sources with no reliable baseline."""
    out = []

    for name, count in yields.items():
        base = store.yield_baseline(name)

        if base and base >= _DEGRADE_MIN_BASELINE and 0 < count < _DEGRADE_FRACTION * base:
            out.append({"name": name, "yield": count, "baseline": round(base)})

    return out


@dataclass
class _Outcomes:
    """Per-source bookkeeping gathered while fetching. It exists so that nothing
    is lost silently: a board that yielded nothing says why, a cap that cut real
    postings is recorded, an extraction that came back far short is flagged, and
    the raw yield feeds the degradation ledger."""

    no_data: list[dict] = field(default_factory=list)      # [{name, reason}]
    truncated: list[dict] = field(default_factory=list)    # [{name, dropped, kind}]
    shortfall: list[dict] = field(default_factory=list)    # [{name, kept, records}]
    yields: dict[str, int] = field(default_factory=dict)   # raw yield per source


def _record_caps(resource: dict, page: dict, outcomes: _Outcomes) -> None:
    """A cap that actually dropped postings is reported — silent truncation is
    exactly the "looks healthy while losing jobs" failure mode."""
    if page.get("truncated_records"):
        outcomes.truncated.append({"name": resource["name"],
                                   "dropped": page["truncated_records"],
                                   "kind": "text budget"})

    if page.get("capped"):
        outcomes.truncated.append({"name": resource["name"],
                                   "dropped": page["capped"], "kind": "board cap"})


async def _positions_from(resource: dict, page: dict,
                          outcomes: _Outcomes) -> tuple[list[dict], bool]:
    """Positions out of a fetched page → (items, extract_errored). Structured
    boards arrive pre-parsed and cost nothing; only raw text goes to haiku."""
    items = page.get("items") or []            # Getro/ATS/a16z pre-structured

    if items:
        return _stamp_source(items, resource), False

    if not page.get("text") or page.get("error"):
        return [], False

    try:
        items = await _extract_items(resource, page)
    except Exception:
        log.warning("extract failed for %s", resource["name"], exc_info=True)

        return [], True

    # Report a NOTABLE extraction shortfall (the cheap model returned far fewer
    # positions than the page had records) — a dropped posting must be visible,
    # never silent. Small gaps are normal (nav/dupes/non-postings).
    recall = page.get("_extract_recall")

    if recall and recall["records"] >= 10 and recall["kept"] < 0.85 * recall["records"]:
        outcomes.shortfall.append({"name": resource["name"], "kept": recall["kept"],
                                   "records": recall["records"]})

    return items, False


def _no_data_reason(page: dict, render: str, catalog: bool, extract_error: bool,
                    windowed_out: int, days: int) -> str:
    """Why a source yielded nothing — the specific cause, never just "empty"."""
    if extract_error:
        return "extraction step errored"

    if windowed_out:
        return f"{windowed_out} posting(s) found but all older than the {days}-day window"

    return _empty_reason(page, render, catalog)


async def _fetch_source(resource: dict, days: int, outcomes: _Outcomes,
                        progress: dict) -> list[dict]:
    """One board end to end: fetch, parse, window, and record why it came back
    empty if it did."""
    render = resource.get("render", "static")
    catalog = config.is_catalog(resource)
    # Catalogs list every open role, so the freshness window is fetched as "no
    # window" AND skipped below; dedup still prevents repeats.
    page = await fetch_page(resource["url"], render, None if catalog else days)

    _record_caps(resource, page, outcomes)

    items, extract_error = await _positions_from(resource, page, outcomes)
    # Raw yield BEFORE window/dedup = the board's parser-health signal
    # (post-window/dedup naturally decays to ~0 on repeat runs, so it cannot
    # baseline degradation).
    outcomes.yields[resource["name"]] = len(items)
    # Per-source windowing: a feed gets cut by date here, a catalog never does.
    # Doing it per source is what lets the two kinds coexist.
    windowed_out = 0

    if items and not catalog:
        before = len(items)
        items = _within_window(items, days)
        windowed_out = before - len(items)

    if not items:
        outcomes.no_data.append({
            "name": resource["name"],
            "reason": _no_data_reason(page, render, catalog, extract_error,
                                      windowed_out, days),
        })

    progress["sources_done"] += 1
    progress["positions"] += len(items)

    return items


async def _gather_positions(resources: list[dict], days: int, outcomes: _Outcomes,
                            progress: dict) -> list[dict]:
    """Every board, four at a time. One failing board yields an empty list with
    a recorded reason instead of sinking the scan."""
    sem = asyncio.Semaphore(4)

    async def one(resource: dict) -> list[dict]:
        async with sem:
            return await _fetch_source(resource, days, outcomes, progress)

    batches = await asyncio.gather(*(one(r) for r in resources))

    return [item for batch in batches for item in batch]


def _initial_stats(resources: list[dict], days: int, all_items: list[dict],
                   unseen: list[dict], fresh: list[dict], outcomes: _Outcomes,
                   degraded: list[dict]) -> dict:
    """What the digest footers report. The scoring stage fills in the rest."""
    return {
        "sources": len(resources), "days": days, "raw": len(all_items),
        "fresh": len(fresh),
        "already_seen": len(all_items) - len(unseen),
        "rule_filtered": len(unseen) - len(fresh),
        "no_data": outcomes.no_data, "no_data_count": len(outcomes.no_data),
        "truncated": outcomes.truncated, "degraded": degraded,
        "shortfall": outcomes.shortfall,
        "prefiltered_out": 0, "ceiling_skipped": 0, "scored": 0,
        "threshold": config.score_threshold(),
        "top_n": config.resources_meta().get("digest_top_n", 15),
    }


def _log_source_problems(outcomes: _Outcomes, degraded: list[dict]) -> None:
    """The digest tells the user; the log tells whoever debugs the boards."""
    if outcomes.no_data:
        log.warning("sources with no data (%d): %s", len(outcomes.no_data),
                    [f"{d['name']}: {d['reason']}" for d in outcomes.no_data])

    if degraded:
        log.warning("sources degraded (%d): %s", len(degraded),
                    [f"{d['name']}: {d['yield']} vs ~{d['baseline']}" for d in degraded])

    if outcomes.truncated:
        log.warning("sources truncated (%d): %s", len(outcomes.truncated),
                    [f"{d['name']}: -{d['dropped']} ({d['kind']})"
                     for d in outcomes.truncated])


def _apply_ceiling(kept: list[dict], stats: dict) -> tuple[list[dict], list[dict]]:
    """matcher_input_cap is a runaway guard, not the normal path — the prefilter
    already keeps the volume sane. If it trips, say so; never truncate in
    silence. Returns (to score, overflow that was never looked at)."""
    ceiling = config.matcher_input_cap()

    if len(kept) <= ceiling:
        return kept, []

    log.warning("matcher input hit the safety ceiling: %d survivors > %d, "
                "scoring only the first %d", len(kept), ceiling, ceiling)
    stats["ceiling_skipped"] = len(kept) - ceiling

    return kept[:ceiling], kept[ceiling:]


async def _score(fresh: list[dict], profile: dict, stats: dict, progress: dict,
                 on_progress: ProgressCB | None) -> list[dict]:
    """Cheap haiku gate, then the sonnet scorer on every survivor, then persist.

    Seen is set for what this run JUDGED (D11): prefiltered-out plus genuinely
    scored. A failed matcher chunk leaves its inputs unscored — those, and
    anything the ceiling cut, stay unseen so they retry next run instead of
    disappearing for 30 days."""
    await _emit(on_progress, f"Prefiltering {len(fresh)}…")

    kept, prefiltered_out = await _prefilter(fresh, profile)
    stats["prefiltered_out"] = prefiltered_out
    kept, overflow = _apply_ceiling(kept, stats)
    progress["matching"] = len(kept)          # heartbeat now reports the scoring phase

    await _emit(on_progress, f"Scoring {len(kept)}…")

    scored = await _match_rank(kept, profile, progress)
    stats["scored"] = len(scored)

    store.save_scored(scored)                 # persisted for `export`
    store.mark_seen(_judged_items(fresh, kept, overflow, scored))
    store.purge_old(config.resources_meta().get("dedup_retention_days", 30))

    return scored


async def run_search(categories: list[str] | None = None,
                     limit_sources: int | None = None,
                     days: int | None = None,
                     on_progress: ProgressCB | None = None) -> tuple[str, dict]:
    profile = config.load_profile()
    cats = categories or profile.get("focus_categories") or ["job", "vc_board"]
    resources = config.enabled_resources(cats)

    if limit_sources:
        resources = resources[:limit_sources]

    # Freshness window: None → config default; always clamp to [1, max].
    if days is None:
        days = config.default_window_days()

    days = max(1, min(int(days), config.max_window_days()))

    # Live counters for the heartbeat ticker. Plain += is safe with no lock:
    # asyncio is single-threaded/cooperative, and the fetch step never awaits
    # between reading and updating `progress`, so no other task can interleave.
    progress = {"sources_done": 0, "positions": 0, "matching": None}
    outcomes = _Outcomes()

    heartbeat_task = asyncio.create_task(_heartbeat(on_progress, progress, len(resources)))

    try:
        all_items = await _gather_positions(resources, days, outcomes, progress)
        await _emit(on_progress, f"Fetched {len(all_items)} from {len(resources)} sources")

        # Degradation check BEFORE recording today (so today's low value can't
        # skew its own baseline): a source now yielding far below its recent
        # median — but not zero, which no_data already covers — has silently
        # half-broken (layout change, partial block). Then persist the ledger.
        degraded = _degraded_sources(outcomes.yields)
        store.record_yields(outcomes.yields)

        # Windowing already happened per source in the fetch step (catalogs
        # exempt). Split the two drops so the digest can explain a small
        # "relevant" count: most of raw→fresh is dedup, not a miss.
        unseen = store.filter_unseen(all_items)
        fresh = _hard_filter(unseen, profile)
        stats = _initial_stats(resources, days, all_items, unseen, fresh, outcomes, degraded)

        _log_source_problems(outcomes, degraded)

        if not fresh:
            return _render_digest([], stats), stats

        scored = await _score(fresh, profile, stats, progress, on_progress)

        return _render_digest(scored, stats), stats
    finally:
        heartbeat_task.cancel()

        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
