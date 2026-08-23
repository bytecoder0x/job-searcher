# Job Searcher

A command-line job-search agent. It scans the boards you configure, drops what
you have already seen, scores the rest against your profile with an LLM, and
prints a ranked digest.

The bundled board catalog is web3, but nothing in the pipeline is: the example
profile is a plain backend engineer, and the prefilter and the scorer derive the
target field from whatever profile is active. Point it at your own CV and it
searches your field.

## How it works

```
python -m src scan
      |
      v
  1. fetch        HTTP or a headless browser per board      Python, 0 tokens
  2. extract      page text -> positions as JSON            haiku
  3. window       freshness cut, dedup, keyword filter      Python, 0 tokens
  4. prefilter    coarse "is this even my field" gate       haiku
  5. score        rank against the profile and the rubric   sonnet
      |
      v
  ranked digest on stdout
```

The savings are in step 3. Fetching, parsing, deduplicating and filtering are
plain Python and cost nothing; the cheap model only turns pages into JSON; the
expensive model only ever sees positions that survived everything else. Every
LLM call is a separate session with no tools and the data inline, so nothing
accumulates between calls.

Structured boards (Greenhouse, Lever, Ashby, Workable, Getro, a16z, RemoteOK,
JobStash) are parsed straight from their JSON and skip the extraction model
entirely.

## Setup

Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium        # only needed for JS-rendered boards

claude login                       # or set ANTHROPIC_API_KEY in .env
cp profile.example.yaml profile.yaml   # or: python -m src onboard cv.pdf
python -m src scan --days 3
```

Authentication is per deployment (see `.env.example`): with no
`ANTHROPIC_API_KEY` the LLM calls draw on your Claude Code subscription; set the
key and they are billed per token to it instead.

## Commands

```bash
python -m src scan                       # the profile's categories, default window
python -m src scan --days 7 -c job bounty
python -m src scan --limit-sources 1     # smoke test: one board only
python -m src scan -q > digest.txt       # digest only; progress goes to stderr

python -m src onboard cv.pdf             # build the profile from a resume
python -m src profile                    # what the search currently runs with
python -m src profile --add must_have_skill Go
python -m src profile --set min_salary 90000
python -m src profile --fields           # what can be edited

python -m src sources                    # how many boards are enabled
python -m src sources --list
python -m src sources --add https://jobs.ashbyhq.com/acme
python -m src sources --off "Web3 Career"

python -m src config                     # score threshold, digest size, window
python -m src config --set threshold 70
python -m src export --min-score 70      # scored positions to CSV
```

Edits made through `profile` and `config` are stored as overrides in
`data/overrides.json` and layered on top of the files — neither `profile.yaml`
nor `resources.yaml` is ever rewritten.

For a daily run, point cron or Task Scheduler at `python -m src scan`; the tool
keeps no scheduler of its own.

## Profile

The profile is what the whole pipeline derives from. It resolves most specific
first:

```
data/profile.active.yaml   written by `onboard` from your CV
profile.yaml               your own copy, kept out of version control
profile.example.yaml       the checked-in template
```

`onboard` accepts a PDF or plain text (`-` reads stdin) and asks before saving.

## Sources

`resources.yaml` ships **59 boards, 56 of them enabled**, grouped by category.
`python -m src sources --list` prints the live state; `--on/--off` toggles one.

| Category | Boards | What is in it |
|---|---:|---|
| `job` | 32 | Crypto-native boards (Web3 Career, Crypto Jobs List, Cryptocurrency Jobs, JobStash, Find Web3, Remote3, Sail Onchain, crypto.jobs, BeInCrypto, Web3 Vacancy, Crypto Careers, RemoteOK), two regional ones (Djinni, DOU), and company career pages: Coinbase, Ripple, Fireblocks, Gemini, Consensys, Uniswap Labs, Alchemy, Ledger, Blockdaemon, Phantom, OpenSea, Magic Eden, Anchorage Digital, Jito Labs, CoW DAO |
| `vc_board` | 13 | Fund portfolio boards — a16z crypto, Dragonfly, Polychain, Pantera, Variant, Electric Capital, Blockchain Capital, Block — and ecosystem boards for Solana, Avalanche, Ethereum, Midnight |
| `bounty` | 7 | Immunefi, HackenProof, Superteam Earn, Scribble DAO, Wizz HQ, First Dollar, Rova |
| `contest` | 4 | Audit contests: Code4rena, Sherlock, Cantina, CodeHawks |
| `hackathon` | 2 | ETHGlobal, DoraHacks |
| `grant` | 1 | Gitcoin |

A scan only visits the categories in your profile's `focus_categories`, so the
default run touches `job` boards, not bounties or hackathons.

**How they are read.** 28 of the 59 come from structured JSON APIs — Greenhouse,
Lever, Ashby, Workable, Getro, Consider, JobStash, a16z, RemoteOK — and never
reach a model at all. The remaining 31 are HTML or JS pages: Python extracts the
text and the candidate links, and only that compact result goes to haiku.

**Disabled in the catalog** (kept with the reason, not deleted): Wellfound needs
a login; Circle's Ashby board 404s after a move; Paradigm renders a JS blob that
never parses into cards.

**Adding your own.** `sources --add <url>` probes the board live —
Greenhouse/Lever/Ashby/Workable by host, then Getro, then plain HTTP, then a full
JS render — and keeps the first handler that actually returns postings. Boards
added that way live in `data/sources.custom.yaml` and win over a catalog entry of
the same name.

## Structure

```
resources.yaml       the source catalog
profile.example.yaml the seed profile template
.claude/agents/      the prompts the pipeline loads (fetcher, prefilter, matcher)
.claude/rules/       matching-rules.md - hard filter and scoring rubric
.claude/skills/      position schema, digest contract, résumé → profile
docs/                architecture, decisions (ADR), handover notes
src/
  __main__.py        the command line: argument tree and dispatch
  scan.py            the scan command; commands.py holds profile/sources/config
  pipeline.py        run_search orchestration; steps.py the three LLM steps
  sources/           fetch_page and one parser per board family
  config.py          env, YAML, profile resolution, auth mode
  store.py           SQLite: dedup, scored history, per-source yield
data/                runtime state (store, exports, active profile)
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q       # no network, no LLM calls, no API key needed
```

Tests that hit the real network are marked `network` and deselected by default
(`pytest.ini`); run them with `python -m pytest -m network`.

## Notes

- Without `playwright install chromium`, JS-rendered boards fall back to plain
  HTTP and return fewer positions, or none.
- Boards that require a login (Wellfound) are disabled in the catalog.
- Positions are remembered in `data/seen.db` and never scored twice, so a second
  scan on the same day is cheap and mostly empty. That is intended.
- Everything the tool collects stays in `data/`, next to the code.
