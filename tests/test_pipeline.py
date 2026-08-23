"""Characterization tests for src/pipeline.py — pin CURRENT behavior."""
from __future__ import annotations

import asyncio

from src import config, llm, pipeline


# ── _prefilter ──────────────────────────────────────────────────────────────
# The gate is coarse: keep/drop is driven by the (faked) agent reply, so any
# minimal profile suffices — it only shapes the injected prompt, not the logic.
_PROFILE = {"identity": {"role": "Engineer"}, "skills": {"must_have": ["Python"]}}


def test_prefilter_keeps_only_returned_indices(monkeypatch, fake_agent, make_position):
    items = [make_position(title=f"T{i}") for i in range(5)]
    agent = fake_agent(responses=["[0, 2, 4]"])
    monkeypatch.setattr(llm, "_run_agent", agent)
    kept, dropped = asyncio.run(pipeline._prefilter(items, _PROFILE))
    assert kept == [items[0], items[2], items[4]]
    assert dropped == 2


def test_prefilter_unparseable_output_keeps_chunk_whole(monkeypatch, fake_agent, make_position):
    items = [make_position(title=f"T{i}") for i in range(3)]
    agent = fake_agent(responses=["not a list"])
    monkeypatch.setattr(llm, "_run_agent", agent)
    kept, dropped = asyncio.run(pipeline._prefilter(items, _PROFILE))
    assert kept == items
    assert dropped == 0


def test_prefilter_raising_call_keeps_chunk_whole(monkeypatch, fake_agent, make_position):
    items = [make_position(title=f"T{i}") for i in range(3)]
    agent = fake_agent(raises=RuntimeError("boom"))
    monkeypatch.setattr(llm, "_run_agent", agent)
    kept, dropped = asyncio.run(pipeline._prefilter(items, _PROFILE))
    assert kept == items
    assert dropped == 0


def test_prefilter_chunks_at_PREFILTER_CHUNK(monkeypatch, fake_agent, make_position):
    items = [make_position(title=f"T{i}") for i in range(pipeline._PREFILTER_CHUNK + 1)]
    agent = fake_agent(responses=["[0, 1]", "[0]"])
    monkeypatch.setattr(llm, "_run_agent", agent)
    kept, dropped = asyncio.run(pipeline._prefilter(items, _PROFILE))
    assert len(agent.calls) == 2
    assert len(kept) == 3
    assert dropped == len(items) - 3


def test_prefilter_injects_candidate_field(monkeypatch, fake_agent, make_position):
    """The candidate's role/skills reach the prompt so the gate is profile-aware,
    not hardcoded to one domain."""
    items = [make_position(title="T0")]
    profile = {"identity": {"role": "Data Scientist", "seniority": "senior"},
               "skills": {"must_have": ["TensorFlow", "PyTorch"]}}
    agent = fake_agent(responses=["[0]"])
    monkeypatch.setattr(llm, "_run_agent", agent)
    asyncio.run(pipeline._prefilter(items, profile))
    prompt = agent.calls[0]
    assert "Data Scientist" in prompt and "TensorFlow" in prompt and "senior" in prompt


# ── _match_rank ───────────────────────────────────────────────────────────
def test_match_rank_batches_and_returns_full_ranking(monkeypatch, fake_agent, make_position, tmp_overrides):
    """Batches the input, merges every batch, and returns the COMPLETE ranking
    best→worst — no threshold filter and no top-N cut here; both are
    presentation and belong to _render_digest (the owner wants to see weak
    matches too, with their score)."""
    config.update_override("meta", "digest_top_n", 3)
    config.update_override("meta", "score_threshold", 50)
    items = [make_position(title=f"T{i}") for i in range(pipeline._MATCH_CHUNK + 1)]
    chunk1 = '[{"score": 90, "title": "A"}, {"score": 70, "title": "B"}, {"score": 40, "title": "C"}]'
    chunk2 = '[{"score": 85, "title": "D"}, {"score": 55, "title": "E"}]'
    agent = fake_agent(responses=[chunk1, chunk2])
    monkeypatch.setattr(llm, "_run_agent", agent)
    profile = config.load_profile()
    scored = asyncio.run(pipeline._match_rank(items, profile))
    assert len(agent.calls) == 2   # 41 items / 40-per-chunk → 2 calls
    assert [j["score"] for j in scored] == [90, 85, 70, 55, 40]


def test_match_rank_serializes_the_passed_profile(monkeypatch, fake_agent, make_position):
    """Scoring must use the ACTIVE profile passed in — not the bundled seed
    profile.yaml. Pins the fix for the old bug that re-read the seed file and so
    ignored an onboarded profile entirely."""
    items = [make_position(title="T0")]
    profile = {"identity": {"role": "Senior Data Scientist"},
               "skills": {"must_have": ["TensorFlow"]}}
    agent = fake_agent(responses=["[]"])
    monkeypatch.setattr(llm, "_run_agent", agent)
    asyncio.run(pipeline._match_rank(items, profile))
    prompt = agent.calls[0]
    assert "Senior Data Scientist" in prompt and "TensorFlow" in prompt
    seed_name = config._read_yaml_dict(config.seed_profile_path())["identity"]["name"]

    assert seed_name not in prompt   # the seed profile's owner must NOT leak in


# ── _judged_items (Wave 4: seen = judged, not merely submitted) ─────────────
def test_judged_items_excludes_unscored_kept(make_position):
    """A kept item the scorer never returned (failed/partial chunk) must NOT be
    marked seen — else it vanishes for 30 days without ever being scored."""
    fresh = [make_position(title=f"T{i}", company="C") for i in range(5)]
    kept = fresh[:3]                                  # T0,T1,T2 reached the scorer
    overflow: list[dict] = []
    scored = [{"title": "T0", "company": "C", "score": 80},
              {"title": "T2", "company": "C", "score": 60}]  # T1's chunk failed
    judged = pipeline._judged_items(fresh, kept, overflow, scored)
    # T0,T2 scored + T3,T4 prefiltered-out (judged); T1 excluded → retries
    assert {j["title"] for j in judged} == {"T0", "T2", "T3", "T4"}


def test_judged_items_excludes_ceiling_overflow(make_position):
    fresh = [make_position(title=f"T{i}", company="C") for i in range(4)]
    kept, overflow = fresh[:2], fresh[2:]             # overflow never looked at
    scored = [{"title": "T0", "company": "C"}, {"title": "T1", "company": "C"}]
    judged = pipeline._judged_items(fresh, kept, overflow, scored)
    assert {j["title"] for j in judged} == {"T0", "T1"}


def test_judged_items_all_scored_marks_all(make_position):
    fresh = [make_position(title=f"T{i}", company="C") for i in range(3)]
    scored = [{"title": f"T{i}", "company": "C"} for i in range(3)]
    judged = pipeline._judged_items(fresh, list(fresh), [], scored)
    assert len(judged) == 3


# ── _extract_items chunking (cheap model under-returns on long lists) ────────
def _records(n):
    urls = [f"https://board.com/jobs/{i}" for i in range(n)]
    text = "\n".join(f"- Job {i} Company | {urls[i]}" for i in range(n))

    return urls, text


def test_extract_items_chunks_long_record_lists(monkeypatch, fake_agent):
    """25 bound records must be split into 20+5 across TWO haiku calls (a single
    call is what let the model lazily drop most of a long list), and merged."""
    import json
    urls, text = _records(25)
    page = {"text": text, "links": urls}
    resp1 = json.dumps([{"title": f"Job {i}", "company": "Company", "url": urls[i]}
                        for i in range(20)])
    resp2 = json.dumps([{"title": f"Job {i}", "company": "Company", "url": urls[i]}
                        for i in range(20, 25)])
    agent = fake_agent(responses=[resp1, resp2])
    monkeypatch.setattr(llm, "_run_agent", agent)
    items = asyncio.run(pipeline._extract_items({"name": "B", "category": "job"}, page))
    assert len(agent.calls) == 2                       # chunked, not one giant call
    assert len(items) == 25                            # every posting recovered
    assert page["_extract_recall"] == {"records": 25, "kept": 25}


def test_extract_items_single_call_for_short_list(monkeypatch, fake_agent):
    import json
    urls, text = _records(3)
    page = {"text": text, "links": urls}
    resp = json.dumps([{"title": f"Job {i}", "company": "Company", "url": urls[i]}
                       for i in range(3)])
    agent = fake_agent(responses=[resp])
    monkeypatch.setattr(llm, "_run_agent", agent)
    items = asyncio.run(pipeline._extract_items({"name": "B", "category": "job"}, page))
    assert len(agent.calls) == 1                       # short list → one call
    assert len(items) == 3


def test_extract_call_valid_empty_array_does_not_retry(monkeypatch, fake_agent):
    """A genuine `[]` (job-free page) must NOT trigger the unparseable-output
    retry — that used to double the haiku cost on a perfectly valid answer."""
    agent = fake_agent(responses=["[]"])
    monkeypatch.setattr(llm, "_run_agent", agent)
    items = asyncio.run(pipeline._extract_call("sys", {"name": "B", "category": "job"}, "text", []))
    assert items == []
    assert len(agent.calls) == 1                       # no retry call


def test_extract_call_unparseable_output_retries(monkeypatch, fake_agent):
    """Genuinely unparseable output (no JSON array at all) still gets the
    one self-correction retry."""
    agent = fake_agent(responses=["sorry, I cannot do that", "[]"])
    monkeypatch.setattr(llm, "_run_agent", agent)
    items = asyncio.run(pipeline._extract_call("sys", {"name": "B", "category": "job"}, "text", []))
    assert items == []
    assert len(agent.calls) == 2                       # retried once


def test_extract_items_recall_records_the_shortfall(monkeypatch, fake_agent):
    """When the model returns fewer than the records present, the recall gap is
    stashed on the page so the caller can report it (never silent)."""
    import json
    urls, text = _records(25)
    page = {"text": text, "links": urls}
    # first chunk returns only 5 of its 20; second returns its 5 → 10/25
    resp1 = json.dumps([{"title": f"Job {i}", "company": "Company", "url": urls[i]}
                        for i in range(5)])
    resp2 = json.dumps([{"title": f"Job {i}", "company": "Company", "url": urls[i]}
                        for i in range(20, 25)])
    agent = fake_agent(responses=[resp1, resp2])
    monkeypatch.setattr(llm, "_run_agent", agent)
    asyncio.run(pipeline._extract_items({"name": "B", "category": "job"}, page))
    assert page["_extract_recall"] == {"records": 25, "kept": 10}
