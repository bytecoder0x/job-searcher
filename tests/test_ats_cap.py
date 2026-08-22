"""ATS catalog cap must keep the most relevant roles, not the alphabetical head."""
from src.sources import ats


def _job(title, tags=None):
    return {"title": title, "tags": tags or [], "url": "https://x/" + title.replace(" ", "-")}


def test_relevance_ranks_skill_matches_first(monkeypatch):
    monkeypatch.setattr(ats, "_profile_terms", lambda: ["python", "rust", "kafka"])
    jobs = [_job("Accountant"), _job("Senior Python Engineer"), _job("Rust Backend, Kafka")]
    ranked = sorted(jobs, key=lambda j: ats._relevance(j, ats._profile_terms()), reverse=True)
    assert ranked[0]["title"] == "Rust Backend, Kafka"    # 2 hits
    assert ranked[-1]["title"] == "Accountant"            # 0 hits


def test_cap_keeps_relevant_and_reports_dropped(monkeypatch):
    monkeypatch.setattr(ats, "_profile_terms", lambda: ["python"])
    monkeypatch.setattr(ats, "_ATS_MAX_JOBS", 2)
    jobs = [_job("Accountant"), _job("Recruiter"), _job("Python Engineer")]
    kept, dropped = ats._cap_by_relevance(jobs)
    assert dropped == 1
    assert any("Python" in j["title"] for j in kept)      # the relevant role survived the cap


def test_cap_is_noop_under_the_limit(monkeypatch):
    monkeypatch.setattr(ats, "_ATS_MAX_JOBS", 120)
    jobs = [_job("A"), _job("B")]
    kept, dropped = ats._cap_by_relevance(jobs)
    assert dropped == 0 and kept == jobs


def test_cap_without_profile_terms_keeps_api_order(monkeypatch):
    monkeypatch.setattr(ats, "_profile_terms", lambda: [])
    monkeypatch.setattr(ats, "_ATS_MAX_JOBS", 2)
    jobs = [_job("First"), _job("Second"), _job("Third")]
    kept, dropped = ats._cap_by_relevance(jobs)
    assert dropped == 1
    assert [j["title"] for j in kept] == ["First", "Second"]   # unchanged order
