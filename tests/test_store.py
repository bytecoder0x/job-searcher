"""Characterization tests for src/store.py — dedup key, seen filter, scored export."""
from __future__ import annotations

import datetime

from src import store


def test_key_of_normalizes_case_and_punctuation():
    a = store.key_of({"title": "Backend Engineer!", "company": "Acme, Inc."})
    b = store.key_of({"title": "backend   engineer", "company": "acme inc"})
    assert a == b


def test_filter_unseen_excludes_seen_and_intra_batch_dupes_and_blanks(tmp_db, make_position):
    a = make_position(title="A", company="X")
    dup = make_position(title="A", company="X", url="https://other")
    blank = make_position(title="", company="")
    b = make_position(title="B", company="Y")
    out = store.filter_unseen([a, dup, blank, b])
    assert out == [a, b]   # intra-batch dup and blank key dropped


def test_mark_seen_then_filter_unseen_excludes(tmp_db, make_position):
    a = make_position(title="A", company="X")
    b = make_position(title="B", company="Y")
    store.mark_seen([a])
    out = store.filter_unseen([a, b])
    assert out == [b]


def test_purge_old_deletes_by_cutoff(tmp_db, make_position):
    a = make_position(title="Old", company="X")
    store.mark_seen([a])
    # Backdate first_seen well before the cutoff.
    conn = store._conn()
    old = (store._utcnow() - datetime.timedelta(days=40)).isoformat()
    conn.execute("UPDATE seen SET first_seen=? WHERE key=?", (old, store.key_of(a)))
    conn.commit()
    conn.close()
    deleted = store.purge_old(days=30)
    assert deleted == 1
    assert store.filter_unseen([a]) == [a]   # no longer "seen"


def test_save_scored_and_export_rows_roundtrip(tmp_db):
    jobs = [
        {"title": "A", "company": "X", "url": "u1", "score": 80, "category": "job", "source": "S"},
        {"title": "B", "company": "Y", "url": "u2", "score": 40, "category": "job", "source": "S"},
    ]
    store.save_scored(jobs, scan_date="2024-01-01")
    rows = store.export_rows(min_score=0, days=0)
    assert {r["title"] for r in rows} == {"A", "B"}
    rows_hi = store.export_rows(min_score=60, days=0)
    assert [r["title"] for r in rows_hi] == ["A"]


def test_export_rows_filters_by_days(tmp_db):
    old_job = {"title": "OldJob", "company": "X", "url": "u1", "score": 80}
    store.save_scored([old_job], scan_date="2000-01-01")
    rows = store.export_rows(min_score=0, days=30)
    assert rows == []
    rows_all = store.export_rows(min_score=0, days=0)
    assert len(rows_all) == 1
