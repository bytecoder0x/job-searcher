# Handover notes — start here

The state of the project in one page: what runs, what to check before believing a
change works, and which invariants look like cruft but are load-bearing. History
lives in [DECISIONS.md](DECISIONS.md) (ADRs); the live data flow in
[ARCHITECTURE.md](ARCHITECTURE.md).

## What it is

A command-line job-search agent. It scans configured job boards, extracts
postings, scores them against a candidate profile, and prints a short ranked
digest.

The bundled `resources.yaml` is a web3 board catalog; the checked-in example
profile is a plain backend engineer, because nothing downstream is field-specific.
`onboard` rebuilds the profile from a résumé, and the prefilter + matcher derive
the target field from that profile, never from hardcoded web3.

Python does everything deterministic (fetch, parse, window, dedup, filter) at
**0 tokens**; the LLM is called only for the two judgment steps. Each LLM call is
an isolated `query()` session with `allowed_tools=[]` and data inline — no context
accumulates, no cross-call cache.

## Run it

```
pip install -r requirements.txt
# auth: either `claude login` (subscription) OR set ANTHROPIC_API_KEY (API billing)
python -m src scan --days 3
python -m src --help          # every command
```

A scheduled run is a cron entry or a Windows scheduled task around the same
command; the tool keeps no scheduler of its own. Under `pythonw.exe` (no console)
the SDK's child processes would each flash a window — `src/nowindow.py` prevents
that, see the invariants below.

**Auth is per-deployment** (`config.auth_mode()`): `ANTHROPIC_API_KEY` set → API
billing; unset → the Claude Code subscription. `scan` prints it before it starts.

## Verify (before claiming anything works)

- `python -m pytest -q` → **307 passed, 5 deselected** (~7s). Must stay green.
- `python -m pytest -q -m network` → live-board canaries.
- `python -m compileall -q src tests` — not `py_compile src/*.py` (that glob skips
  `src/sources/`).
- `python -m src scan --limit-sources 1 --days 3` — the cheapest REAL end-to-end
  run (fetch → haiku extract → haiku prefilter → sonnet match). Run it from a copy
  of the tree, not the live checkout, or the scanned roles get marked seen in
  `data/seen.db` and drop out of the next digest.

Tests are characterization tests: they pin current behaviour. A failure after a
change means fix the code, not the test. **Never claim it works off green mocks —
verify end-to-end with a real run and show the numbers.**

## Pipeline

```
fetch(sources)  → extract(haiku, HTML boards only) → freshness-window → dedup
              → hard-filter → PREFILTER(haiku, coarse field gate)
              → MATCH(sonnet, scores every survivor, batched) → digest top-N
```

- **extract** (`steps._extract_items`, haiku) turns raw listing text into JSON
  positions. Getro/ATS/a16z/RemoteOK are structured JSON — parsed in Python, no
  haiku.
- **prefilter** (`steps._prefilter`, haiku) is a cheap coarse relevance gate that
  culls clearly off-field roles so sonnet isn't wasted on them. The field it
  gates against is **derived from the active profile** (`_profile_gate`), not a
  fixed domain. Graceful: a failed/unparseable chunk is kept whole.
- **match** (`steps._match_rank`, sonnet) scores every prefilter survivor against
  the **active profile** (`data/profile.active.yaml` if onboarded, else the seed
  `profile.yaml`) plus `.claude/rules/matching-rules.md`. Synonym/adjacency is the
  model's own knowledge (domain-neutral) — there is no fixed taxonomy dictionary.
- `matcher_input_cap` is a runaway-guard **ceiling**, not the normal path (the
  prefilter keeps volume sane); if it trips it's reported, never silent.

LLM step prompts are the bodies of `.claude/agents/{fetcher,prefilter,matcher}.md`
plus `.claude/skills/*` — read every run, so editing them needs no restart. The
frontmatter `model:` is read for the prefilter only; the other steps use
`config.EXTRACT_MODEL`/`MAIN_MODEL` (env-overridable), except the prefilter, which
reads its model from its agent frontmatter (single source of truth today).

## Modules

```
src/
  pipeline.py  run_search + _judged_items + _degraded_sources (orchestration)
  steps.py     _extract_items / _prefilter / _match_rank (the 3 LLM steps)
  llm.py       _run_agent (SDK call + retry), prompt/skill loaders, JSON parsers
  onboard.py   résumé (PDF/text) → profile (skill resume-to-profile)
  detect.py    render auto-detection for `sources --add` (ATS→getro→static→js)
  config.py    env/paths/yaml, active-profile + custom-source loaders, auth_mode
  overrides.py runtime overrides layered over the files (sources/meta/profile)
  filters.py   _within_window, _hard_filter, _validate_items, _stamp_source
  digest.py    plain-text render + silent-loss footers
  store.py     SQLite data/seen.db: seen, scored, source_yield, chat_log (30d)
  nowindow.py  CREATE_NO_WINDOW patch (apply BEFORE the SDK import — see invariants)
  progress.py  heartbeat + progress callback
  sources/     fetch_page dispatcher + http html common getro ats a16z remoteok
  __main__.py  the command line: argument tree + lazy dispatch per command
  scan.py      the scan command; commands.py = profile / sources / config
  cli.py       console plumbing (UTF-8, stderr notes, shutdown-noise filter)
```

## Generic / reconfigurable mode

Single-tenant (one active profile at a time), not concurrent multi-user.

- **Identity is config, not code.** Active profile = `data/profile.active.yaml`
  when onboarded, else the seed (`config.profile_base_path()` /
  `seed_profile_path()` / `save_active_profile()`). Overrides layer on top;
  `clear_profile_overrides()` drops only those.
- **Résumé → profile** (`onboard`, `src/onboard.py` + skill `resume-to-profile`):
  PDF (pypdf) or piped text → one main-model call → `_normalize_profile` coerces
  to the schema → printed preview → confirm → saved as the active profile.
  Domain-agnostic; verified live on a non-web3 (Go/backend) CV.
- **Dynamic sources** (`sources --add <url>`, `src/detect.py`): curated
  `resources.yaml` catalog + user sources in `data/sources.custom.yaml`, merged by
  `config.all_resources()` (custom wins on name). `detect_source` probes render
  handlers most-specific-first (ATS-by-host → getro → static → js) with a live
  fetch, keeping the first that yields postings. `sources --remove` drops a custom
  one; catalog ones use `sources --off`. `sources --list` marks added ones.

## Command surface

```
scan [--days N] [--categories ...] [--limit-sources N] [-q]
onboard <cv.pdf | ->   [-y]
profile [--set|--add|--remove FIELD VALUE] [--reset] [--fields]
sources [--list] [--add URL [--category C] [--name N]] [--remove NAME] [--on|--off NAME]
config  [--set threshold|top_n|window|cap VALUE] [--reset]
export  [--min-score N] [--days N]
```

`profile` and `config` edits are stored in `data/overrides.json` and layered over
the files at read time — the profile and `resources.yaml` are never rewritten, and
`config --reset` reverts everything. The digest goes to stdout, progress and
summaries to stderr.

## Load-bearing invariants — do NOT "fix" or "simplify" these

- **`src/nowindow.py` must stay, applied BEFORE the SDK import** (`__main__.py`
  and `scan.py` first, `llm.py` too). Under `pythonw.exe` (no console) the SDK
  spawns `claude.exe` per LLM call, and a console-less parent makes Windows
  allocate a NEW console window per child → a terminal-window storm (one chat
  message ≈ 3 windows, a full scan = hundreds). Measured A/B under `pythonw`:
  unpatched 3 windows/call, patched 0. Invisible under a shell-launched
  `python.exe` (children inherit the console) — reproduce only under `pythonw`,
  measuring **visible windows** (Win32 `EnumWindows`, `CASCADIA_*`/
  `ConsoleWindowClass`), never process counts.
- **Catalog vs feed — never put the freshness window back on catalogs.** Company
  ATS boards (`greenhouse|lever|ashby|workable`, `config.CATALOG_RENDERS`/
  `is_catalog`) list every currently-open role, not a dated feed, so a "last N
  days" window empties them (measured: Alchemy 17→0, Uniswap 10→0, etc. — boards
  reported broken were just windowed to nothing). Catalogs are fetched `days=None`
  and skipped by `_within_window`; **dedup** prevents repeats. Windowing is applied
  PER SOURCE in `pipeline.handle`, not once over the merged list — that's what lets
  feeds and catalogs coexist. Override per source with `catalog: true|false`.
- **HTML listing extraction (`sources/html.py`) heuristics are load-bearing.**
  `_is_posting_path` decides which links are real postings: it rejects category
  slugs (`-jobs`/`jobs`-token/`salar*` segments — web3.career's tag cloud once
  starved real cards to 0) and nav/filter directories mid-path (`search`,
  `employers`, `pages`) unless a posting segment follows, while accepting an id
  under a posting segment (`_looks_like_id`: all-digits, or ≥8-char alnum with a
  digit — Find Web3's Airtable `/job/reci3d…`, DOU's `/vacancies/<id>`).
  `_card_block` finds a card by climbing to the largest ancestor wrapping exactly
  ONE posting link (`_posting_anchors` counts DISTINCT posting URLs), not by a
  text-size ceiling. `_extract_listing_text` emits one `- {card text} | {url}`
  record with the URL bound to it — the extractor never guesses which link is
  which. Truncation is on record boundaries with a reported count. Verified live:
  Web3 Career 0→19, DOU 0→7, Crypto Careers 0→20, Find Web3 0→46.
- **Path-level URL grounding (`filters._validate_items`, `_norm_url`).** Extracted
  URLs are grounded against candidate links ∪ URLs harvested from the page text, at
  **host+path** level (query/trailing-slash tolerant) — a same-host invented PATH
  is a dead link and is dropped. Deterministic, 0 tokens; do not move grounding
  back to host-only. Parsing is lenient (strict → trailing-comma repair →
  per-object salvage) so one malformed object costs one position, not the source.
- **seen = JUDGED, not merely fetched (`pipeline._judged_items`, D11).**
  `mark_seen` gets ONLY prefiltered-out items (judged by the prefilter) + items the
  scorer actually returned a score for (matched by dedup key). A failed/partial
  matcher chunk, and ceiling overflow, leave their inputs UNSCORED → those stay
  UNSEEN so they retry next run instead of vanishing for 30 days. Key drift fails
  safe (looks unscored → retries). Do NOT go back to marking the whole `fresh` list.
- **Fetch-layer retry (`http.request_with_retry`/`get_with_retry`).** All
  structured parsers (ATS/Getro/RemoteOK) and the static HTML fetch retry TRANSIENT
  failures (network/timeout, 429/5xx) with 0.5/1/2s backoff — one blip used to drop
  a whole board. Hard 4xx (404) raise immediately.
- **Silent loss is always reported.** Digest footers surface every drop:
  `_no_data_footer` (0 yield + reason), `_truncated_footer` (a cap cut postings),
  `_degraded_footer` (yield <40% of a source's recent median, baseline ≥5 — a
  half-broken parser), `_recall_footer` (a board parsed <85% of ≥10 records),
  `_filtered_footer` ("N already shown · M filtered by rules", so a small relevant
  count is explained, not alarming). The yield ledger (`store.source_yield`) records
  RAW per-source yield pre window/dedup and needs ≥3 prior scans to baseline.
- **`_run_agent` uses `max_turns=None` on purpose + 3 retries.** With
  `allowed_tools=[]` the model answers in one turn; a turn cap only turned transient
  rate-limiting into hard "Reached maximum number of turns" errors (D13). Rate
  limiting is surfaced as a plain "rate-limited, try again" line, not a raw error.
- **`steps.py`/`pipeline.py` call `llm._run_agent(...)` via the module attribute**
  (`from . import llm`), never `from .llm import _run_agent` — so the test fixture
  can intercept every caller with one monkeypatch.
- The shared Playwright browser lives only in `sources/http.py`.

## Known issues / next steps

- **Board-config (not code):** Block.xyz (403 at the Fastly edge by IP — recheck
  from a VPS) and HackenProof (Cloudflare Turnstile) will only work off a VPS
  egress. Circle (Ashby) 404'd — find the new board token or leave disabled.
  Paradigm's JS blob is noisy — try `render: getro` or leave disabled.
- **Source catalog — next wave (2026-08-09 research).** Added & verified live:
  Variant + Polychain (getro), Jito Labs (lever `jito.wtf`), CoW DAO (ashby
  `cow-dao`), Web3 Vacancy + crypto.jobs (static). Deferred, need code:
  (a) **Pantera** (`jobs.panteracapital.com`, ~1000+ jobs) is on the newer
  **"Consider"** platform (virtualized list, no `__NEXT_DATA__`) — needs a new
  render handler, biggest single prize. (b) **JobStash** (`jobstash.xyz`) — the
  HTML heuristic catches ~90% facet/tag chrome; it likely has a JSON API worth a
  dedicated parser. Tokens valid but **0 open roles right now** (recheck later,
  no code): Flashbots (ashby `flashbots.net`), bloXroute (lever `blox-route`).
- **Pre-filter ATS by role** before the matcher (further token saving on the big
  company boards).
- **`/draft`** — a cover-letter/resume-bullets drafter over a scored position
  (`store.scored` already persists them); extends the agent from "find" to "apply".
- **TG/Discord/X sources** — many roles are posted in channels, not on boards.
  Needs a user client (Telethon) for TG/Discord history and the X API for Twitter;
  bigger effort.
