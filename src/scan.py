"""The `scan` command: fetch the configured boards, score what is new against
the active profile, print the digest. Arguments come from src/__main__.py."""
from __future__ import annotations

import asyncio
import logging

from .nowindow import apply as _no_window

# Before anything pulls in claude-agent-sdk: its CLI children must not each open
# a console window (see src/nowindow.py).
_no_window()

from . import cli, config              # noqa: E402  (must follow the patch above)
from .pipeline import run_search       # noqa: E402

log = logging.getLogger("job-searcher")

# Scalar stats worth a one-line summary; the list-valued keys (no_data,
# truncated, …) are already spelled out in the digest footers.
_SUMMARY_KEYS = ("sources", "days", "raw", "already_seen", "rule_filtered",
                 "fresh", "prefiltered_out", "ceiling_skipped", "scored")


async def _progress(text: str) -> None:
    cli.note(text)


def _summary(stats: dict) -> str:
    return " ".join(f"{k}={stats[k]}" for k in _SUMMARY_KEYS if k in stats)


async def _run(args) -> int:
    profile = config.load_profile()
    cats = args.categories or profile.get("focus_categories") or ["job", "vc_board"]

    if not args.quiet:
        cli.note(f"profile : {profile.get('identity', {}).get('role', '—')}")
        cli.note(f"auth    : {config.auth_mode()}")
        cli.note(f"scanning: {', '.join(cats)}")

    try:
        digest, stats = await run_search(categories=cats,
                                         limit_sources=args.limit_sources,
                                         days=args.days,
                                         on_progress=None if args.quiet else _progress)
    except Exception as e:              # a failed scan reports, it does not traceback
        if args.verbose:
            log.exception("Scan failed")

        cli.note(f"scan failed: {e}" + ("" if args.verbose else "  (-v for the traceback)"))

        return 1

    print(digest)

    if not args.quiet:
        cli.note(f"\n{_summary(stats)}")

    return 0


def run(args) -> int:
    """`python -m src scan` — fetch, score and print. Returns the exit code."""
    return asyncio.run(_run(args))
