"""CSV export of scored positions.

The matching step persists every scored position to SQLite (store.scored); this
module turns that into a CSV file on demand. No network, no LLM — pure Python,
0 tokens. Exposed as `python -m src export`.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone

from . import cli, config, store

# Column order in the CSV (header row).
EXPORT_FIELDS = [
    "Score", "Role", "Company", "Location", "Salary",
    "Category", "Source", "Reason", "Application URL", "Scan Date",
]

# A cell starting with any of these is a formula in Excel/Sheets — a scraped
# title/company/reason could contain one by accident (or maliciously).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """Neutralises CSV/formula-injection: prefix a leading =+-@/tab/CR with a
    single quote so spreadsheet apps render the cell as text, not a formula."""
    s = str(value) if value is not None else ""
    return f"'{s}" if s.startswith(_FORMULA_PREFIXES) else s


def _row(j: dict) -> dict:
    return {
        "Score": j.get("score", ""),
        "Role": _csv_safe(j.get("title", "")),
        "Company": _csv_safe(j.get("company", "")),
        "Location": j.get("location", ""),
        "Salary": _csv_safe(j.get("salary", "")),
        "Category": j.get("category", ""),
        "Source": j.get("source", ""),
        "Reason": _csv_safe(j.get("reason", "")),
        "Application URL": j.get("url", ""),
        "Scan Date": j.get("scan_date", ""),
    }


def export_csv(min_score: int = 0, days: int = 0) -> tuple[str | None, int]:
    """Writes a CSV of scored positions (filtered by min_score and an optional
    freshness window in days; 0 = all history). Returns (path, count). Path is
    None when there is nothing to export."""
    rows = store.export_rows(min_score=min_score, days=days)

    if not rows:
        return None, 0

    config.ensure_dirs()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    suffix = ""

    if min_score:
        suffix += f"_min{min_score}"

    if days:
        suffix += f"_last{days}d"

    out_path = config.EXPORT_DIR / f"jobs_{date_str}{suffix}.csv"

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPORT_FIELDS)
        writer.writeheader()

        for j in rows:
            writer.writerow(_row(j))

    return str(out_path), len(rows)


def run(args) -> int:
    """`python -m src export` — write the scored positions to a CSV file."""
    path, count = export_csv(min_score=args.min_score, days=args.days)

    if not path:
        cli.note("nothing scored yet - run python -m src scan first")

        return 1

    print(path)                     # path alone on stdout, so it can be piped
    cli.note(f"{count} positions")

    return 0
