"""Storage for deduplicating already-shown positions (SQLite).

Dedup key is a normalized "title + company" pair, so the same job posting
from different boards isn't shown twice or re-scored (saves tokens).
"""
from __future__ import annotations

import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from . import config

_DB = config.DATA_DIR / "seen.db"


def _utcnow() -> datetime:
    """Naive UTC timestamp, replacing the 3.12-deprecated datetime.utcnow().
    We strip tzinfo so the stored ISO strings stay byte-identical to the old
    format — existing seen.db/scored rows compare correctly against new ones."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _conn() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(_DB)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seen (
               key        TEXT PRIMARY KEY,
               title      TEXT,
               company    TEXT,
               url        TEXT,
               source     TEXT,
               first_seen TEXT
           )"""
    )
    # Scored results of the matching step — kept so they can be exported to CSV
    # later (the digest is only text). One row per (job, scan_date).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scored (
               key       TEXT,
               title     TEXT,
               company   TEXT,
               url       TEXT,
               location  TEXT,
               salary    TEXT,
               score     INTEGER,
               reason    TEXT,
               category  TEXT,
               source    TEXT,
               scan_date TEXT,
               PRIMARY KEY (key, scan_date)
           )"""
    )
    # Per-source raw yield per scan — the run-over-run record that makes a
    # SILENT partial degradation (a board that quietly drops 40 postings → 2,
    # still 200-OK) detectable. One row per (source, scan_date).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS source_yield (
               source    TEXT,
               scan_date TEXT,
               yield_count INTEGER,
               PRIMARY KEY (source, scan_date)
           )"""
    )

    return conn


def _norm(s: str | None) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9а-яіїєґ ]+", " ", s)

    return re.sub(r"\s+", " ", s).strip()


def key_of(item: dict) -> str:
    return f"{_norm(item.get('title'))}|{_norm(item.get('company'))}"


def filter_unseen(items: list[dict]) -> list[dict]:
    """Returns only positions not yet shown (does not mark them as seen)."""
    if not items:
        return []

    conn = _conn()

    try:
        seen = {row[0] for row in conn.execute("SELECT key FROM seen")}
    finally:
        conn.close()

    out, batch_keys = [], set()

    for it in items:
        k = key_of(it)

        if k in seen or k in batch_keys or k == "|":
            continue

        batch_keys.add(k)
        out.append(it)

    return out


def mark_seen(items: list[dict]) -> None:
    if not items:
        return

    now = _utcnow().isoformat()
    conn = _conn()

    try:
        conn.executemany(
            "INSERT OR IGNORE INTO seen(key,title,company,url,source,first_seen)"
            " VALUES (?,?,?,?,?,?)",
            [
                (key_of(it), it.get("title"), it.get("company"),
                 it.get("url"), it.get("source"), now)
                for it in items
            ],
        )
        conn.commit()
    finally:
        conn.close()


def purge_old(days: int) -> int:
    cutoff = (_utcnow() - timedelta(days=days)).isoformat()
    cutoff_date = cutoff[:10]
    conn = _conn()

    try:
        cur = conn.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff,))
        # Keep scored + yield history bounded by the same retention window.
        conn.execute("DELETE FROM scored WHERE scan_date < ?", (cutoff_date,))
        conn.execute("DELETE FROM source_yield WHERE scan_date < ?", (cutoff_date,))
        conn.commit()

        return cur.rowcount
    finally:
        conn.close()


# ── Per-source yield ledger (silent-degradation detection) ────────────────

def record_yields(counts: dict[str, int], scan_date: str | None = None) -> None:
    """Persists each source's raw yield for this scan (INSERT OR REPLACE by
    source+date, so a same-day re-run overwrites)."""
    if not counts:
        return

    day = scan_date or _utcnow().date().isoformat()
    conn = _conn()

    try:
        conn.executemany(
            "INSERT OR REPLACE INTO source_yield(source,scan_date,yield_count)"
            " VALUES (?,?,?)",
            [(name, day, int(n)) for name, n in counts.items()],
        )
        conn.commit()
    finally:
        conn.close()


def yield_baseline(source: str, lookback: int = 10,
                   today: str | None = None) -> float | None:
    """Median of a source's yield over its last `lookback` PRIOR scans (today
    excluded so a freshly-recorded low value can't mask its own drop). None when
    there's too little history (< 3 points) to judge a baseline reliably."""
    day = today or _utcnow().date().isoformat()
    conn = _conn()

    try:
        rows = conn.execute(
            "SELECT yield_count FROM source_yield WHERE source=? AND scan_date < ?"
            " ORDER BY scan_date DESC LIMIT ?",
            (source, day, lookback),
        ).fetchall()
    finally:
        conn.close()

    vals = [r[0] for r in rows if r[0] is not None]

    if len(vals) < 3:
        return None

    return float(statistics.median(vals))


# ── Scored results (for CSV export) ──────────────────────────────────────

def save_scored(jobs: list[dict], scan_date: str | None = None) -> None:
    """Persists the matcher's scored positions for a later CSV export.
    scan_date defaults to today (UTC, YYYY-MM-DD). Re-running the same day
    overwrites that day's row for a given job (INSERT OR REPLACE by key+date)."""
    if not jobs:
        return

    day = scan_date or _utcnow().date().isoformat()
    conn = _conn()

    try:
        conn.executemany(
            "INSERT OR REPLACE INTO scored"
            "(key,title,company,url,location,salary,score,reason,category,source,scan_date)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (key_of(j), j.get("title"), j.get("company"), j.get("url"),
                 j.get("location") or j.get("remote"), j.get("salary"),
                 _as_int(j.get("score")), j.get("reason"),
                 j.get("category"), j.get("source"), day)
                for j in jobs
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _as_int(v: object) -> int | None:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def export_rows(min_score: int = 0, days: int = 0) -> list[dict]:
    """Scored rows for CSV export, filtered by min_score and (optionally) a
    freshness window in days (0 = all history). Newest & highest score first."""
    conn = _conn()

    try:
        sql = "SELECT title,company,url,location,salary,score,reason,category,source,scan_date FROM scored WHERE score >= ?"
        params: list[object] = [min_score]

        if days > 0:
            cutoff = (_utcnow() - timedelta(days=days)).date().isoformat()
            sql += " AND scan_date >= ?"
            params.append(cutoff)

        sql += " ORDER BY scan_date DESC, score DESC"
        cols = ["title", "company", "url", "location", "salary", "score",
                "reason", "category", "source", "scan_date"]

        return [dict(zip(cols, row)) for row in conn.execute(sql, params)]
    finally:
        conn.close()
