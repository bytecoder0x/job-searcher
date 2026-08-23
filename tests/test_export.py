"""Characterization tests for src/export.py — the CSV writer and its command
(`python -m src export`). No network, no LLM."""
from __future__ import annotations

import csv

from src import export, store
from src.__main__ import main


def test_export_csv_nothing_to_export_returns_none_zero(tmp_db, tmp_export_dir):
    path, count = export.export_csv()
    assert (path, count) == (None, 0)


def test_export_csv_writes_header_and_rows(tmp_db, tmp_export_dir):
    jobs = [{"title": "A", "company": "X", "url": "u1", "score": 80, "category": "job",
             "source": "S", "location": "Remote", "salary": "100k", "reason": "fit"}]
    store.save_scored(jobs, scan_date="2024-01-01")
    path, count = export.export_csv()
    assert count == 1

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    assert rows[0]["Role"] == "A"
    assert rows[0]["Company"] == "X"
    assert rows[0]["Score"] == "80"


# ── the export command ─────────────────────────────────────────────────────
def test_cli_reports_an_empty_store_without_writing(tmp_db, tmp_export_dir, capsys):
    assert main(["export"]) == 1

    out = capsys.readouterr()

    assert out.out == ""                        # nothing to pipe onward
    assert "python -m src scan" in out.err


def test_cli_prints_only_the_path_on_stdout(tmp_db, tmp_export_dir, capsys):
    store.save_scored([{"title": "A", "company": "X", "url": "u1", "score": 80,
                        "category": "job", "source": "S", "location": "Remote",
                        "salary": "", "reason": "fit"}], scan_date="2024-01-01")

    assert main(["export", "--min-score", "50"]) == 0

    out = capsys.readouterr()

    assert out.out.strip().endswith(".csv")
    assert "1 positions" in out.err


def test_cli_min_score_filters(tmp_db, tmp_export_dir, capsys):
    store.save_scored([{"title": "A", "company": "X", "url": "u1", "score": 40,
                        "category": "job", "source": "S", "location": "",
                        "salary": "", "reason": ""}], scan_date="2024-01-01")

    assert main(["export", "--min-score", "90"]) == 1        # filtered out, nothing written

    capsys.readouterr()
