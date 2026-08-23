"""The three LLM steps of the pipeline: extract (haiku), prefilter (haiku) and
match/rank (sonnet). Split out of pipeline.py to keep modules under the
~250-line house limit; run_search wires these together.
"""
from __future__ import annotations

import json
import logging
import re

import yaml

from . import config, llm
from .filters import _stamp_source, _validate_items
from .llm import _load_agent, _parse_int_array, _parse_json_array, _skill

log = logging.getLogger("job-searcher")


_EXTRACT_CHUNK = 20   # bound records per haiku extraction call. A long list made
                      # the cheap model lazily return ~a third and silently drop
                      # the rest (web3.career: 67 records → 19). Short lists are
                      # processed in full; we merge the chunks.


def _is_valid_empty_array(text: str) -> bool:
    """True when `text` contains a genuinely parseable JSON `[]` (a job-free
    page) — distinct from output that never formed a valid array at all.
    Mirrors only the first (strict) parse layer of llm._parse_json_array;
    used solely to keep the retry guard below from firing on a legitimate
    empty result."""
    stripped = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    a, b = stripped.find("["), stripped.rfind("]")

    if a == -1 or b == -1 or b <= a:
        return False

    try:
        return json.loads(stripped[a:b + 1]) == []
    except Exception:
        return False


async def _extract_call(system: str, resource: dict, page_text: str,
                        link_list: list[str]) -> list[dict]:
    """One haiku extraction call over page_text: lenient parse + one self-
    correction retry (an unparseable first answer used to lose the batch).
    A genuinely empty `[]` (no jobs on the page) does NOT count as
    unparseable — retrying that would double the haiku cost for nothing."""
    links = "\n".join(link_list)
    user = (
        f"Source: {resource['name']} (category={resource['category']}).\n"
        f"Extract EVERY individual position from the text below and return ONLY a "
        f"JSON array per the schema — one object per posting, do NOT summarize or "
        f"skip any. Use the candidate links for the url field.\n\n"
        f"CANDIDATE LINKS:\n{links}\n\nPAGE TEXT:\n{page_text}"
    )
    text, _ = await llm._run_agent(system, user, config.EXTRACT_MODEL)
    parsed = _parse_json_array(text)

    if not parsed and text.strip():
        if _is_valid_empty_array(text):
            log.info("extract: %s returned a valid empty array — no jobs found", resource["name"])
            return parsed

        log.info("extract: unparseable output for %s — retrying once", resource["name"])
        retry = (
            f"{user}\n\nYour previous answer could not be parsed as a JSON array. "
            f"It started with: {text.strip()[:200]!r}\n"
            f"Return ONLY a JSON array — no prose, no markdown fence. "
            f"If there are genuinely no positions, return exactly []."
        )
        text, _ = await llm._run_agent(system, retry, config.EXTRACT_MODEL)
        parsed = _parse_json_array(text)

    return parsed


async def _extract_items(resource: dict, page: dict) -> list[dict]:
    """Cheap-model extraction, CHUNKED so no single call gets a list long enough
    to trigger lazy truncation. Bound records ("- card | url", one per posting)
    are split into _EXTRACT_CHUNK batches and merged; a prose/flatten page (no
    bound records) goes in one call. Output is validated in Python
    (_validate_items): free, catches invented URLs / off-schema fields. The
    record-vs-kept recall is stashed on `page` so the caller reports any
    shortfall — a dropped posting is never silent.

    Model resolution note: this step (and _match_rank) run on the hot path
    with config.EXTRACT_MODEL/MAIN_MODEL (env-overridable), NOT the frontmatter
    in .claude/agents/*.md — frontmatter model is only consulted in fully
    the prompt body. _prefilter below is the one exception; see its docstring."""
    _, body = _load_agent("fetcher")
    # Schema only: extract runs on haiku per source, so keep the prompt minimal.
    # Synonym normalization is done by matcher (one call).
    system = f"{body}\n\nPOSITION SCHEMA:\n{_skill('parse-vacancy')}"
    link_list = page.get("links", [])
    text = page.get("text") or ""
    record_lines = [ln for ln in text.split("\n")
                    if ln.lstrip().startswith("- ") and " | http" in ln]

    if len(record_lines) > _EXTRACT_CHUNK:
        parsed: list[dict] = []

        for i in range(0, len(record_lines), _EXTRACT_CHUNK):
            chunk = "\n".join(record_lines[i:i + _EXTRACT_CHUNK])
            parsed += await _extract_call(system, resource, chunk, link_list)
    else:
        parsed = await _extract_call(system, resource, text, link_list)

    # Ground URLs against every URL seen on the page (candidate links are capped
    # at MAX_LINKS, but the page text carries one per record), so path-level
    # validation drops invented links without false-dropping real overflow ones.
    known = list(link_list) + re.findall(r"https?://[^\s|)>\]]+", text)
    vstats: dict = {}
    validated = _validate_items(parsed, known, vstats)
    drop = vstats.get("dropped") or {}

    if any(drop.values()):
        log.info("extract %s: kept %d, dropped %s", resource["name"],
                 vstats.get("kept", 0), {k: v for k, v in drop.items() if v})

    if record_lines:                            # record-structured page → track recall
        page["_extract_recall"] = {"records": len(record_lines), "kept": len(validated)}
        log.info("extract %s: recall %d/%d records",
                 resource["name"], len(validated), len(record_lines))

    return _stamp_source(validated, resource)


_PREFILTER_CHUNK = 80  # positions per haiku prefilter call


def _profile_gate(profile: dict) -> str:
    """One-line field summary (role, top must-have skills, seniority) injected
    into the prefilter prompt so the coarse gate keeps roles in or adjacent to
    THIS candidate's field instead of a hardcoded domain. Missing pieces are
    simply omitted — a sparse profile just yields a looser gate."""
    ident = profile.get("identity") or {}
    role = ident.get("role") or "the candidate's stated field"
    skills = (profile.get("skills") or {}).get("must_have") or []
    parts = [f"role: {role}"]

    if skills:
        parts.append("core skills: " + ", ".join(str(s) for s in skills[:10]))

    if ident.get("seniority"):
        parts.append(f"seniority: {ident['seniority']}")

    return "; ".join(parts)


async def _prefilter(items: list[dict], profile: dict) -> tuple[list[dict], int]:
    """Cheap coarse relevance gate (haiku) between hard_filter and the
    expensive matcher: culls clearly-off-field roles so sonnet only scores
    positions plausibly in the candidate's field (built from `profile`).
    Chunked (~_PREFILTER_CHUNK/call). GRACEFUL BY DESIGN: a chunk whose call
    fails or whose output doesn't parse is KEPT WHOLE — the prefilter may only
    cull obvious junk, never silently drop a vacancy on an infra hiccup.

    Model resolution note: unlike _extract_items/_match_rank, this step
    resolves its model from the .claude/agents/prefilter.md frontmatter (via
    _load_agent) rather than a config.*_MODEL constant — intentional, not
    drift: the prefilter has no env override today, so the agent file is its
    single source of truth. Change here if that split should ever be unified."""
    if not items:
        return [], 0

    model, body = _load_agent("prefilter")
    gate = _profile_gate(profile)
    kept: list[dict] = []

    for start in range(0, len(items), _PREFILTER_CHUNK):
        chunk = items[start:start + _PREFILTER_CHUNK]
        payload = [
            {"i": i, "title": it.get("title"), "company": it.get("company"),
             "tags": (it.get("tags") or [])[:6], "category": it.get("category")}
            for i, it in enumerate(chunk)
        ]
        user = (
            f"Candidate field — {gate}.\n\n"
            f"Positions (index, title, company, tags, category):\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n\n"
            f"Return ONLY a JSON array of the indices to KEEP."
        )
        keep_idx, call_ok = None, False

        try:
            text, _ = await llm._run_agent(body, user, model)
            call_ok = True
            keep_idx = _parse_int_array(text)
        except Exception:
            log.warning("prefilter chunk agent call failed — keeping it whole (%d items)",
                        len(chunk), exc_info=True)

        if keep_idx is None:
            if call_ok:  # call succeeded but the output didn't parse as an index array
                log.warning("prefilter chunk unparseable output — keeping it whole (%d items)", len(chunk))

            keep_idx = list(range(len(chunk)))

        kept.extend(chunk[i] for i in keep_idx if 0 <= i < len(chunk))

    dropped = len(items) - len(kept)
    log.info("prefilter: kept %d/%d (dropped %d clearly off-field)", len(kept), len(items), dropped)

    return kept, dropped


_MATCH_CHUNK = 40  # positions per sonnet scoring call


async def _match_rank(items: list[dict], profile: dict,
                      progress: dict | None = None) -> list[dict]:
    """Scores EVERY item passed in (no top-K input cap — the prefilter already
    culled the junk), sequentially in chunks of _MATCH_CHUNK (polite to rate
    limits; _run_agent retries within a call). Returns EVERY scored position,
    sorted best→worst, with nothing cut: the owner asked to see the full
    ranking, so the threshold/top-N split is presentation and lives in
    _render_digest. Persisting all of them also makes /export a complete list."""
    _, body = _load_agent("matcher")
    # Serialize the ACTIVE profile passed in (onboarded or seed + chat edits),
    # NOT the bundled seed file — this is what drives scoring for any candidate.
    profile_txt = yaml.safe_dump(profile, allow_unicode=True, sort_keys=False)
    rules_txt = config.read_text(".claude/rules/matching-rules.md")
    top_n = config.resources_meta().get("digest_top_n", 15)
    threshold = config.score_threshold()
    # Domain-neutral synonym guidance (no fixed dictionary): let the model use
    # its own knowledge so this generalises to any field, not just one domain.
    system = (
        f"{body}\n\nWhen counting skill matches, treat well-known synonyms and "
        f"adjacent/equivalent skills, tools and role titles as matches using "
        f"your own knowledge — but only on a real signal in the posting; never "
        f"credit a skill inferred from the domain alone."
    )
    scored: list[dict] = []
    total_batches = (len(items) + _MATCH_CHUNK - 1) // _MATCH_CHUNK

    for n, start in enumerate(range(0, len(items), _MATCH_CHUNK), 1):
        if progress is not None:            # so the heartbeat can show movement
            progress["batch"] = (n, total_batches)

        chunk = items[start:start + _MATCH_CHUNK]
        user = (
            f"Candidate profile (YAML):\n{profile_txt}\n\n"
            f"Matching rules (matching-rules.md):\n{rules_txt}\n\n"
            f"Score EVERY position in this batch and return ALL of them — do not "
            f"drop the weak ones and do not limit to top-N. The owner wants the "
            f"full ranking, best to worst; Python merges the batches, sorts "
            f"globally and decides what to show (threshold {threshold}, "
            f"top-{top_n} shown in full).\n\n"
            f"Positions to score (JSON, already deduped):\n"
            f"{json.dumps(chunk, ensure_ascii=False)}\n\n"
            f"Return ONLY a JSON array of the scored positions per your output contract."
        )

        try:
            text, _ = await llm._run_agent(system, user, config.MAIN_MODEL)
            scored.extend(_parse_json_array(text))
        except Exception:
            # A failed chunk is lost (no way to re-score it for free), but it
            # must never crash the whole run — log loudly and move on.
            log.warning("matcher chunk failed (%d positions skipped)", len(chunk), exc_info=True)

    scored.sort(key=lambda j: j.get("score") or 0, reverse=True)

    return scored
