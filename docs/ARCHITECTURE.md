# job-searcher architecture (living document)

> Update this file when the data flow or component roles change. This is an
> on-demand reference — do not duplicate the invariants from
> [HANDOFF.md](HANDOFF.md) here.

The bundled `resources.yaml` is a web3 board catalog; the profile it searches by
is not tied to any field (the checked-in example is a backend engineer).
Reconfigurable via `onboard`. Single active profile at a time.

## Components and data flow

```
python -m src scan [--days N] [--categories ...]
   │
   ▼
src/__main__.py  ──dispatch──►  scan.run  ──►  pipeline.run_search(categories, days)
                              │
   ┌──────────────────────────┼───────────────────────────────────────┐
   │ For EACH source (Semaphore=4, in parallel):                       │
   │   fetch_page(url, render, days)   ← src/sources/, Python, 0 tok.   │
   │     • static → httpx + selectolax (fallback trafilatura)          │
   │     • js     → Playwright (wait + scroll + iframes)               │
   │     • getro/ats/a16z/remoteok → structured JSON API (pre-`items`) │
   │   HTML boards only → steps._extract_items()  ← haiku, JSON positions
   └──────────────────────────┬───────────────────────────────────────┘
                              ▼
   filters._within_window (feeds only, catalogs exempt)                ← 0 tok.
   store.filter_unseen  +  filters._hard_filter (profile)              ← 0 tok.
                              ▼
   steps._prefilter(fresh, profile)   ← haiku, coarse field gate       ← culls off-field
                              ▼
   steps._match_rank(kept, ACTIVE profile)  ← sonnet, scores every survivor
                              ▼
   store.save_scored + store.mark_seen(_judged_items(...)) + purge_old ← SQLite
                              ▼
   digest._render_digest (plain text + silent-loss footers) → stdout
```

A daily run is a cron entry or a scheduled task around the same command; the tool
keeps no scheduler of its own.

The active profile is `data/profile.active.yaml` when onboarded, else the seed
`profile.yaml`, with chat-driven overrides merged on top (`config.load_profile`).
The prefilter and matcher both derive their target field from that profile — no
domain is hardcoded.

## Files and roles

| File | Role |
|---|---|
| `resources.yaml` | curated source catalog (`render: static/js/getro/greenhouse/lever/ashby/workable`) + `meta` |
| `data/sources.custom.yaml` | boards added by `sources --add`; merged by `config.all_resources` (custom wins) |
| `profile.yaml` | bundled seed profile (web3) |
| `data/profile.active.yaml` | onboarding-generated active profile (overrides the seed when present) |
| `.claude/rules/matching-rules.md` | hard filter + scoring rubric (read by the matcher) |
| `.claude/agents/fetcher.md` | extract system prompt (haiku) |
| `.claude/agents/prefilter.md` | coarse relevance-gate system prompt (haiku); `model:` is its source of truth |
| `.claude/agents/matcher.md` | scoring/ranking system prompt (sonnet) |
| `.claude/agents/{developer,tester}.md` | dev + QA sub-agents (not on the live path) |
| `.claude/skills/*` | parse-vacancy (position schema), format-digest (output contract), resume-to-profile (loaded by `onboard`) |
| `src/pipeline.py` | `run_search` + `_judged_items` + `_degraded_sources` (orchestration) |
| `src/steps.py` | the three LLM steps: `_extract_items` / `_prefilter` / `_match_rank` |
| `src/llm.py` | isolated agent calls (SDK + retry), prompt/skill loaders, JSON parsers |
| `src/__main__.py` | the command line: argument tree, lazy dispatch per command |
| `src/commands.py` | the profile / sources / config commands |
| `src/scan.py` | the scan command: `run_search` + digest to stdout |
| `src/cli.py` | console plumbing: UTF-8 output, stderr notes, shutdown noise |
| `src/onboard.py` | résumé (PDF/text) → profile dict (skill `resume-to-profile`) |
| `src/detect.py` | render auto-detection for `sources --add` (probe handlers, first hit wins) |
| `src/config.py` | env/paths/yaml, active-profile + custom-source loaders, `auth_mode`, windows |
| `src/overrides.py` | runtime overrides layered over the files (sources/meta/profile edits) |
| `src/filters.py` | freshness window, hard filter, URL grounding, source stamping |
| `src/digest.py` | plain-text digest + silent-loss footers |
| `src/store.py` | SQLite `data/seen.db`: seen (dedup), scored, source_yield, chat_log |
| `src/sources/` | `fetch_page` dispatcher + per-family parsers (getro/ats/a16z/remoteok/html/http) |
| `src/nowindow.py` | CREATE_NO_WINDOW patch (must apply before the SDK import) |
| `src/progress.py` | progress callback + heartbeat ticker |

## LLM steps (three — this is where tokens are spent)

| Step | Model | Where | Input |
|---|---|---|---|
| extract | haiku | `steps._extract_items` | cleaned listing text + candidate links (HTML boards only) |
| prefilter | haiku | `steps._prefilter` | compact `{title, company, tags, category}` + one-line profile field gate |
| match | sonnet | `steps._match_rank` | compact JSON of deduped positions + active profile + matching-rules |

Structured boards (Getro/ATS/a16z/RemoteOK) skip extract — they return normalized
`items` from JSON. Everything else (HTTP, rendering, parsing, window, dedup, hard
filter, URL grounding) is deterministic Python, 0 tokens.

## Freshness window

`run_search(days)` clamps to `[1, max_window_days]` and passes it to `fetch_page`.
Getro/ATS enforce it structurally on `created_at` (reliable). For plain HTML,
`filters._within_window` only cuts explicit dates in `posted`; date-less postings
are kept (better than losing something relevant). **Catalogs** (company ATS boards,
`config.is_catalog`) are fetched `days=None` and skipped by the window entirely —
they list every open role, not a dated feed; dedup prevents repeats.

## How the LLM is called

Python drives the loop and calls the LLM in isolation for extract/prefilter/match:
`allowed_tools=[]`, all data inline, a fresh session per call. The
`.claude/agents/*.md` files provide only the *bodies of the system prompts* — the
model never decides what to fetch, so a bad page can waste one call, never a chain
of them.
