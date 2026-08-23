"""Per-source yield ledger — detect a source silently half-breaking."""
import pytest

from src import config, store


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "_DB", tmp_path / "seen.db")
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    yield


def _seed(source, per_day):
    for day, n in per_day.items():
        store.record_yields({source: n}, scan_date=day)


def test_baseline_is_median_of_prior_scans():
    _seed("Board", {"2026-07-10": 40, "2026-07-11": 44, "2026-07-12": 38})
    assert store.yield_baseline("Board", today="2026-07-13") == 40.0


def test_baseline_none_without_enough_history():
    _seed("Board", {"2026-07-10": 40, "2026-07-11": 44})   # only 2 points
    assert store.yield_baseline("Board", today="2026-07-13") is None


def test_baseline_excludes_today():
    _seed("Board", {"2026-07-10": 40, "2026-07-11": 40, "2026-07-12": 40, "2026-07-13": 2})
    # today's low value must not drag its own baseline down
    assert store.yield_baseline("Board", today="2026-07-13") == 40.0


def test_record_yields_overwrites_same_day():
    store.record_yields({"Board": 10}, scan_date="2026-07-13")
    store.record_yields({"Board": 3}, scan_date="2026-07-13")
    _seed("Board", {"2026-07-10": 9, "2026-07-11": 9, "2026-07-12": 9})
    # only one row for 2026-07-13, and it's the latest (3)
    assert store.yield_baseline("Board", today="2026-07-14") == 9.0


def test_degraded_detection_via_pipeline_helper(monkeypatch):
    from src import pipeline
    _seed("Board", {"2026-07-10": 40, "2026-07-11": 40, "2026-07-12": 40})
    # a source that used to yield ~40 now yields 2 → degraded
    monkeypatch.setattr(store, "yield_baseline", lambda name, **k: 40.0)
    dg = pipeline._degraded_sources({"Board": 2})
    assert dg == [{"name": "Board", "yield": 2, "baseline": 40}]


def test_not_degraded_when_zero(monkeypatch):
    """Zero is no_data's job, not the degradation footer's."""
    from src import pipeline
    monkeypatch.setattr(store, "yield_baseline", lambda name, **k: 40.0)
    assert pipeline._degraded_sources({"Board": 0}) == []


def test_not_degraded_on_small_baseline(monkeypatch):
    from src import pipeline
    monkeypatch.setattr(store, "yield_baseline", lambda name, **k: 3.0)
    assert pipeline._degraded_sources({"Board": 1}) == []   # baseline too small to judge


def test_not_degraded_within_normal_variation(monkeypatch):
    from src import pipeline
    monkeypatch.setattr(store, "yield_baseline", lambda name, **k: 40.0)
    assert pipeline._degraded_sources({"Board": 30}) == []  # 30 of ~40 is fine
