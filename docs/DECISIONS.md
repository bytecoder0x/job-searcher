# Decision log (ADR)

> Why it was done this way. Add an entry on any significant architectural choice;
> do not rewrite old ones — mark them `deprecated` and add a new one.

## D1. Engine — Claude Agent SDK via subscription, not API key
We pay with the Claude Code subscription quota, not per API token. Do **not** set
`ANTHROPIC_API_KEY` — it overrides the subscription. Authorization is `claude login`.

## D2. Python orchestrates, the LLM only judges
All deterministic work (HTTP, rendering, parsing, dedup, filters) is pure Python,
0 tokens. The LLM receives only a compact structured result. The main token-savings
invariant. → see [ARCHITECTURE.md](ARCHITECTURE.md).

## D3. Each LLM step is an isolated `query()`
A fresh session per step → pages do not accumulate in history. This is the gain of
"sub-agents" without the risk of unpredictable agentic orchestration.
Consequence: prompt-cache does not apply between calls — do not rely on "auto-caching".

## D4. Model split — haiku for the rough work, sonnet for the final
extract (page parsing) with the cheap haiku; match (scoring/ranking) with the more
expensive sonnet on already-compact data. developer/tester — sonnet for quality.

## D5. Getro boards — via the public JSON-API, no LLM and no Playwright
`__NEXT_DATA__` → `network.id` → `api.getro.com`. Server-side filtering by the
profile's `must_have`; round-robin merge of terms so one broad query doesn't crowd
out eng roles. Cheaper and more reliable than browser rendering.

## D6. Dedup — SQLite by the normalized (title + company) pair
The same position from different boards is not shown or scored twice. Retention by
`dedup_retention_days`. → `src/store.py`.

## D7. Sources — config-driven in `resources.yaml`
Adding/disabling a board without code, via the `render` field. `developer` does not
hardcode URLs.

## D8. Privacy — everything stays on the machine that runs it
There is no service, no account and no remote state: the profile, the dedup store,
the digests and the CSV exports all live in `data/` next to the code. The only
outbound traffic is the board fetches and the LLM calls themselves.

## D9. Freshness window — default 1 day, max 7
`scan --days N`; unset means `default_window_days`. Clamp [1, `max_window_days`]. Reliable on Getro (`created_at`),
best-effort on static/JS (no structured dates). Getro cap 40→120 (all relevant ones
within the window). Config: `resources.yaml → meta`
(`default_window_days`, `max_window_days`).

## D10. Every change is verified by the cheapest sufficient real run
Green mocks prove nothing here: the failure modes are a board that changed its
markup, a render that silently returns a nav-only page, a model that answers in
prose instead of JSON. So the order is compile → unit tests → fetch-only against
the live board → and only then a full scan (`scan --limit-sources 1`, which spends
real tokens). A defect is written down as a repro, not as a description.

## D11. mark_seen only what the matcher evaluated (`capped`), not all of `fresh`
"Seen" means "the matcher made a judgment on it", not "we fetched it". After A2
introduced `_cap_for_matching`, `fresh` splits into `capped` (sent to
`_match_rank`) and the capped-out remainder (never scored). `run_search` calls
`store.mark_seen(capped)`, not `store.mark_seen(fresh)`.
- Rejected-by-score (in `capped`, scored below the digest threshold) **stay**
  marked seen — re-scoring the same failing JD every run would burn tokens for
  no benefit; that's the whole point of dedup.
- Capped-out (in `fresh` but not in `capped`) are **not** marked seen, so they
  get another chance next run. Re-fetch + re-prescore are pure Python (0
  tokens), so this costs nothing and stops a busy multi-source day from
  permanently burying a position it never actually saw.
- Rejected alternative: mark-all-of-`fresh` (the old behavior) — silently buries
  capped-out positions until the 30-day purge, even though no model ever judged
  them. Also rejected: mark-only-digest (only score ≥ threshold) — would re-send
  rejected-by-score positions to the matcher every run, defeating dedup's token
  savings. → `src/agent_runner.py` (since split into `pipeline.py`/`steps.py`).

## D12. `src/tools.py` split into `src/sources/` (one concern per module)
The 1000+ line `tools.py` became hard to hold in context. Split, no behavior
change (112 characterization tests unchanged in count/assertions):
`sources/http.py` (fetch + shared Playwright browser), `sources/html.py`
(text/link extraction), `sources/common.py` (profile query terms, date
helpers shared by parsers), `sources/{getro,ats,a16z,remoteok}.py` (one
board-family parser each), `sources/__init__.py` (fetch_page dispatcher).
Every module stays under ~250 lines. (`src/mcp_server.py`, which held the
agentic-mode wrapper, was later deleted: nothing ever imported it.)

## D13. `src/agent_runner.py` split into `llm.py` / `chat.py` / `filters.py` /
`digest.py` / `progress.py` / `pipeline.py`
The 662-line file mixed six concerns (SDK call+retry, chat routing, freeform-date
parsing, digest rendering, progress/heartbeat, orchestration) and was getting hard
to hold in context. Pure move + docstring trim, no behavior change (112
characterization tests unchanged in count/assertions, split across
`tests/test_{filters,digest,llm,progress,chat,pipeline}.py`):
- `llm.py` — `_run_agent` (SDK call + retry), `_load_agent`, `_skill`,
  `_parse_json_array/_object/_int_array`.
- `chat.py` — `_ask`, `_route_message`, `_valid_edit_ops`, `_SEARCH_HINT`/`_EDIT_HINT`.
- `filters.py` — `_parse_posted`, `_within_window`, `_hard_filter`, `_stamp_source`.
- `digest.py` — `_empty_reason`, `_no_data_footer`, `_render_digest`.
- `progress.py` — `ProgressCB`, `HEARTBEAT_SECONDS`, `_emit`, `_heartbeat`.
- `pipeline.py` — `run_search` + the LLM steps (`_extract_items`, the coarse
  gate later renamed `_prefilter` — see D18, `_match_rank`).

`chat.py` and `pipeline.py` call the SDK step as `llm._run_agent(...)` (via
`from . import llm`, not `from .llm import _run_agent`) so tests can monkeypatch
`llm._run_agent` once and have it intercept every caller — a direct `from .llm
import _run_agent` would bind the original function at import time and the patch
would silently miss.

Rationale for the essay `_run_agent` used to carry (max_turns=None): with
`allowed_tools=[]` the model has no tools to loop on, so it emits its answer in
one turn regardless of the cap. A turn cap gained nothing and actively hurt —
under subscription rate limiting the SDK retries a turn, each retry burns a
"turn", and a low cap turned transient throttling into a hard 'Reached maximum
number of turns' error. Removing the cap lets those retries resolve instead of
failing; the small retry-with-backoff loop around the whole call is what
actually handles the transient case.

## D14. One command line, `python -m src <command>`
The tool is driven from the terminal: `src/__main__.py` owns the whole argument
tree and dispatches to a module per command (`scan`, `onboard`, `profile`,
`sources`, `config`, `export`). Two consequences worth keeping: the argument tree
lives in one file, so `--help` is complete and there is no second place to update;
and each command module is imported only when that command runs, so `sources
--list` does not pay for loading the agent SDK. Console plumbing that every command
needs — UTF-8 stdout, notes on stderr, the Windows shutdown-noise filter — sits in
`src/cli.py`, not copied per command. The digest goes to stdout and everything else
to stderr, so `scan -q > digest.txt` is a clean file.

## D15. `src/config.py` split into `config.py` + `overrides.py`
Pure move + docstring trim, no behavior change (same 112 tests; `tests/conftest.py`'s
`tmp_overrides` fixture now patches `overrides.OVERRIDES_PATH`, not
`config.OVERRIDES_PATH` — the module that owns the path is the one tests must
patch). `overrides.py` owns the runtime-overrides layer:
`OVERRIDES_PATH`, `load/save/update/clear_overrides`, `PROFILE_FIELDS`,
`FIELD_LABELS`, `apply_profile_edits` (conflict rules: `set` overwrites; `add
X` un-removes and adds X case-insensitively deduped; `remove X` un-adds and
removes X), `profile_overrides_summary`, and the private merge helpers
(`_apply_profile_overrides`, `_dotted_parent`, `_list_at`, `_ci_index`).
`config.py` keeps env/paths/yaml loading and the accessors
(`enabled_resources`, `resources_meta`, `score_threshold`,
`load_profile`, ...) and re-exports the overrides helpers so existing call
sites (commands, tests) keep working unchanged. To avoid a
cycle (`overrides.py` needs `DATA_DIR` for `OVERRIDES_PATH`; `config.py`
imports `overrides.py` to re-export it), `overrides.py` computes its own
`BASE_DIR`/`DATA_DIR` independently instead of importing them from `config`
— cheap duplication (2 lines), no shared mutable state, and `save_overrides`
only creates the one directory it needs instead of calling the full
`config.ensure_dirs()`.

## D16. Generic / reconfigurable mode — identity is config, not code
The app started hardcoded to one owner's web3 search. It now **ships
web3-cataloged** (`resources.yaml`; the example profile is field-neutral) but is
**reconfigurable to any field**: the active profile is `data/profile.active.yaml`
(else the seed), and user-added sources live in `data/sources.custom.yaml`. Single-tenant (one active profile at a time), not
concurrent multi-user. Rejected a full multi-user rewrite as over-scoped for a
personal agent — a per-deployment single tenant covers "anyone runs their own copy".

## D17. Auth is per-deployment (`config.auth_mode()`), not fixed to the subscription
Supersedes the "**never set `ANTHROPIC_API_KEY`**" rule from D1 (which assumed a
single owner on a subscription). `ANTHROPIC_API_KEY` set → per-token API billing
(so anyone can run on their own key); unset → the Claude Code subscription via
`claude login`. The SDK/CLI honours the env var itself; we only surface which mode
is active (logged at startup, documented in `.env.example`). D1's model split still
holds; only the "must not set the key" absolute is relaxed.

## D18. `triage` → `prefilter` rename, and it is profile-driven
The coarse relevance gate between hard-filter and the matcher was renamed
`_triage` → `_prefilter` (and split into `steps.py`, D19). More than a rename: it
no longer asks a hardcoded "is this technical/web3?" — it gates against a one-line
summary of the **active profile's** field (`_profile_gate`: role + top must-have
skills + seniority), so it culls off-field roles for *whatever* domain the profile
describes. Graceful by design: a chunk whose call fails or won't parse is kept
whole (a coarse gate may only cull obvious junk, never silently drop a vacancy).

## D19. Domain-agnostic matcher; `web3-taxonomy` skill deleted; `steps.py` split
The matcher scores against the **active profile** passed in (onboarded or seed +
chat edits), not the bundled seed file. The `web3-taxonomy` skill (a fixed web3
synonym dictionary) was **deleted**: instead the matcher is told to treat
well-known synonyms / adjacent skills as matches using its own knowledge, but only
on a real signal in the posting — this generalises to any field without a
per-domain dictionary to maintain. The three LLM steps (`_extract_items`,
`_prefilter`, `_match_rank`) were moved out of `pipeline.py` into `steps.py` to
keep both files under the ~250-line house limit; `pipeline.py` re-exports them so
existing callers/tests keep using `pipeline.<name>`. Pure move + behaviour-neutral;
tests unchanged.

## D20. Active profile from a résumé (`onboard`), and dynamic sources (`sources --add`)
Onboarding builds the active profile from a CV: `src/onboard.py` extracts text
(PDF via pypdf, else decoded) → one main-model call with the `resume-to-profile`
skill → `_normalize_profile` coerces it to the schema (safe defaults so a partial
answer can't produce a broken profile) → printed preview → confirm → saved as
`data/profile.active.yaml` (overrides cleared). Verified live on a non-web3
(Go/backend) CV. `sources --add <url>` uses `src/detect.py` to probe render handlers
most-specific-first (ATS-by-host → getro → static → js) with a live fetch, keeping
the first that yields postings — so a user adds any board without hand-editing
`resources.yaml`. Custom sources merge via `config.all_resources()` (custom wins on
name).

## Open decisions (need a choice)
- **Getro: "all relevant within the window" vs "literally all".** Currently a search
  by `must_have` is kept (we don't pull HR/trading), which is an assumption rather
  than a confirmed requirement. If literally all are needed — remove the skill-search
  in `parse_getro`.
