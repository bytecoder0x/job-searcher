"""Search orchestration: fetch → window → dedup/hard-filter → prefilter → match →
digest. Deterministic steps in Python (0 tokens); two isolated LLM calls
(extract=haiku, match=sonnet) on already-condensed data — this is how we save
tokens. Each query() call is a fresh session, so pages don't accumulate in history.
"""
from __future__ import annotations

import asyncio
import logging

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

    sem = asyncio.Semaphore(4)
    # Live counters for the heartbeat ticker. Plain += is safe with no lock:
    # asyncio is single-threaded/cooperative, and `handle` never awaits
    # between reading and updating `progress`, so no other task can interleave.
    progress = {"sources_done": 0, "positions": 0, "matching": None}
    # Per-source outcome, for the "report failures with a REASON, don't skip
    # silently" requirement — appended from `handle`, same no-lock reasoning as
    # `progress` above (no await between the check and the append).
    no_data: list[dict] = []   # [{name, reason}] for sources that yielded 0 positions
    truncated: list[dict] = []  # [{name, dropped, kind}] where a cap cut real postings
    shortfall: list[dict] = []  # [{name, kept, records}] where haiku parsed far fewer
    yields: dict[str, int] = {}  # per-source RAW yield (pre window/dedup) for the ledger

    async def handle(r: dict) -> list[dict]:
        async with sem:
            render = r.get("render", "static")
            catalog = config.is_catalog(r)
            # Catalogs list every open role, so the freshness window is fetched
            # as "no window" AND skipped below; dedup still prevents repeats.
            page = await fetch_page(r["url"], render, None if catalog else days)

            # Surface a cap that actually dropped postings — silent truncation
            # is exactly the "looks healthy while losing jobs" failure mode.
            if page.get("truncated_records"):
                truncated.append({"name": r["name"], "dropped": page["truncated_records"],
                                  "kind": "text budget"})

            if page.get("capped"):
                truncated.append({"name": r["name"], "dropped": page["capped"],
                                  "kind": "board cap"})

            items = page.get("items") or []   # Getro/ATS/a16z pre-structured
            extract_error = False

            if items:
                items = _stamp_source(items, r)   # no haiku needed
            elif page.get("text") and not page.get("error"):
                try:
                    items = await _extract_items(r, page)
                except Exception:
                    log.warning("extract failed for %s", r["name"], exc_info=True)
                    items, extract_error = [], True

                # Report a NOTABLE extraction shortfall (the cheap model returned
                # far fewer positions than the page had records) — a dropped
                # posting must be visible, never silent. Small gaps are normal
                # (nav/dupes/non-postings), so only flag a real loss.
                rec = page.get("_extract_recall")

                if rec and rec["records"] >= 10 and rec["kept"] < 0.85 * rec["records"]:
                    shortfall.append({"name": r["name"], "kept": rec["kept"],
                                      "records": rec["records"]})

            # Raw yield BEFORE window/dedup = the board's parser-health signal
            # (post-window/dedup naturally decays to ~0 on repeat runs, so it
            # can't baseline degradation). Recorded per source for the ledger.
            yields[r["name"]] = len(items)
            # Per-source windowing: a feed gets cut by date here, a catalog never
            # does. Doing it per source (instead of once over the merged list)
            # is what lets the two kinds coexist.
            windowed_out = 0

            if items and not catalog:
                before = len(items)
                items = _within_window(items, days)
                windowed_out = before - len(items)

            if not items:
                if extract_error:
                    reason = "extraction step errored"
                elif windowed_out:
                    reason = (f"{windowed_out} posting(s) found but all older than "
                              f"the {days}-day window")
                else:
                    reason = _empty_reason(page, render, catalog)

                no_data.append({"name": r["name"], "reason": reason})

            progress["sources_done"] += 1
            progress["positions"] += len(items)

            return items

    heartbeat_task = asyncio.create_task(_heartbeat(on_progress, progress, len(resources)))

    try:
        batches = await asyncio.gather(*(handle(r) for r in resources))
        all_items = [it for b in batches for it in b]
        await _emit(on_progress, f"Fetched {len(all_items)} from {len(resources)} sources")

        # Degradation check BEFORE recording today (so today's low value can't
        # skew its own baseline): a source now yielding far below its recent
        # median — but not zero, which no_data already covers — has silently
        # half-broken (layout change, partial block). Then persist the ledger.
        degraded = _degraded_sources(yields)
        store.record_yields(yields)

        # Windowing already happened per source in `handle` (catalogs exempt).
        # Split the two drops so the digest can explain a small "relevant" count:
        # most of raw→fresh is dedup (already shown in earlier runs), not a miss.
        unseen = store.filter_unseen(all_items)
        fresh = _hard_filter(unseen, profile)
        stats = {
            "sources": len(resources), "days": days, "raw": len(all_items), "fresh": len(fresh),
            "already_seen": len(all_items) - len(unseen),
            "rule_filtered": len(unseen) - len(fresh),
            "no_data": no_data, "no_data_count": len(no_data),
            "truncated": truncated, "degraded": degraded, "shortfall": shortfall,
            "prefiltered_out": 0, "ceiling_skipped": 0, "scored": 0,
            "threshold": config.score_threshold(),
            "top_n": config.resources_meta().get("digest_top_n", 15),
        }

        if no_data:
            log.warning("sources with no data (%d): %s", len(no_data),
                        [f"{d['name']}: {d['reason']}" for d in no_data])

        if degraded:
            log.warning("sources degraded (%d): %s", len(degraded),
                        [f"{d['name']}: {d['yield']} vs ~{d['baseline']}" for d in degraded])

        if truncated:
            log.warning("sources truncated (%d): %s", len(truncated),
                        [f"{d['name']}: -{d['dropped']} ({d['kind']})" for d in truncated])

        if not fresh:
            return _render_digest([], stats), stats

        # Every fresh, unseen position is analyzed — no silent cap. Cheap haiku
        # prefilter first (culls the obvious off-field noise), then the
        # expensive sonnet scorer sees every survivor.
        await _emit(on_progress, f"Prefiltering {len(fresh)}…")
        kept, prefiltered_out = await _prefilter(fresh, profile)
        stats["prefiltered_out"] = prefiltered_out

        # matcher_input_cap is a runaway-guard CEILING, not the normal path —
        # the prefilter already keeps the volume sane. If it ever trips, that's
        # reported (never a silent truncation); raise it at runtime with
        # /set cap <n> if a genuinely huge batch needs to go through.
        ceiling = config.matcher_input_cap()
        overflow: list[dict] = []

        if len(kept) > ceiling:
            log.warning("matcher input hit the safety ceiling: %d survivors > %d, "
                        "scoring only the first %d", len(kept), ceiling, ceiling)
            stats["ceiling_skipped"] = len(kept) - ceiling
            overflow = kept[ceiling:]
            kept = kept[:ceiling]

        progress["matching"] = len(kept)   # heartbeat now reports the scoring phase
        await _emit(on_progress, f"Scoring {len(kept)}…")

        scored = await _match_rank(kept, profile, progress)
        stats["scored"] = len(scored)
        store.save_scored(scored)               # persist for CSV export (/export)
        # Mark seen ONLY what this run actually judged (D11): prefiltered-out +
        # genuinely-scored. A failed/partial matcher chunk leaves its inputs
        # unscored — those (and ceiling overflow) stay unseen so they retry next
        # run instead of being lost for 30 days. See _judged_items.
        store.mark_seen(_judged_items(fresh, kept, overflow, scored))
        store.purge_old(config.resources_meta().get("dedup_retention_days", 30))

        return _render_digest(scored, stats), stats
    finally:
        heartbeat_task.cancel()

        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
