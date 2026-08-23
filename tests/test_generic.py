"""Generic-mode features: active profile, résumé onboarding, custom sources,
render auto-detection, auth mode. Pure/deterministic parts only (no LLM/network;
those are verified live)."""
from __future__ import annotations

import pytest

from src import config, detect, export, onboard, overrides


# ── auth mode ──────────────────────────────────────────────────────────────
def test_auth_mode_switches_on_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert config.auth_mode() == "subscription"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    assert config.auth_mode() == "api_key"


# ── active profile (onboarding target, seed untouched) ─────────────────────
def test_active_profile_save_load_and_fallback(monkeypatch, tmp_path):
    active = tmp_path / "profile.active.yaml"
    monkeypatch.setattr(config, "ACTIVE_PROFILE_PATH", active)

    assert config.profile_base_path() == config.seed_profile_path()   # seed fallback

    config.save_active_profile({"identity": {"role": "Backend Engineer"}})

    assert config.profile_base_path() == active                       # active now wins
    import yaml
    saved = yaml.safe_load(active.read_text(encoding="utf-8"))
    assert saved["identity"]["role"] == "Backend Engineer"


def test_load_profile_falls_back_to_seed_on_corrupt_active(monkeypatch, tmp_path):
    active = tmp_path / "profile.active.yaml"
    active.write_text("identity: [unterminated", encoding="utf-8")   # corrupt YAML
    monkeypatch.setattr(config, "ACTIVE_PROFILE_PATH", active)
    monkeypatch.setattr(overrides, "load_overrides", lambda: {})
    profile = config.load_profile()
    assert profile and profile != {}   # fell back to the bundled seed, not raised/empty


def test_load_profile_empty_dict_when_seed_also_unreadable(monkeypatch, tmp_path):
    active = tmp_path / "profile.active.yaml"
    active.write_text("not: [valid", encoding="utf-8")
    monkeypatch.setattr(config, "ACTIVE_PROFILE_PATH", active)
    monkeypatch.setattr(config, "LOCAL_PROFILE_PATH", tmp_path / "profile.yaml")
    monkeypatch.setattr(config, "EXAMPLE_PROFILE_PATH", tmp_path / "none.yaml")   # no seed either
    monkeypatch.setattr(overrides, "load_overrides", lambda: {})

    assert config.load_profile() == {}


def test_seed_falls_back_to_the_example_without_a_local_profile(monkeypatch, tmp_path):
    """profile.yaml is yours and stays out of version control, so a fresh
    checkout has only the example — it must still resolve to a usable seed."""
    monkeypatch.setattr(config, "LOCAL_PROFILE_PATH", tmp_path / "profile.yaml")

    assert config.seed_profile_path() == config.EXAMPLE_PROFILE_PATH

    seed = config._read_yaml_dict(config.EXAMPLE_PROFILE_PATH)

    assert seed["focus_categories"] and seed["identity"]["role"]
    assert seed["skills"]["must_have"] and seed["exclude"]["keywords"]


# ── custom sources CRUD + merge into all_resources ─────────────────────────
def test_custom_sources_crud_and_merge(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CUSTOM_SOURCES_PATH", tmp_path / "sources.custom.yaml")
    monkeypatch.setattr(config, "load_overrides", lambda: {})   # ignore real overrides.json
    base = len(config.all_resources())
    config.add_custom_source({"name": "My Board", "url": "https://x.io/jobs",
                              "category": "job", "render": "js", "enabled": True})
    assert len(config.all_resources()) == base + 1
    assert any(r["name"] == "My Board" for r in config.enabled_resources(["job"]))
    # replace by same name (case-insensitive) — no duplicate
    config.add_custom_source({"name": "my board", "url": "https://x.io/jobs2",
                              "category": "job", "render": "static", "enabled": True})
    mine = [r for r in config.load_custom_sources() if r["name"].lower() == "my board"]
    assert len(mine) == 1 and mine[0]["render"] == "static"
    assert config.remove_custom_source("My Board") is True
    assert config.remove_custom_source("My Board") is False   # already gone
    assert len(config.all_resources()) == base


def test_custom_source_overrides_catalog_by_name(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CUSTOM_SOURCES_PATH", tmp_path / "c.yaml")
    monkeypatch.setattr(config, "load_overrides", lambda: {})
    catalog_name = config.load_resources()["resources"][0]["name"]
    config.add_custom_source({"name": catalog_name, "url": "https://override.io",
                              "category": "job", "render": "js", "enabled": True})
    matches = [r for r in config.all_resources() if r["name"] == catalog_name]
    assert len(matches) == 1 and matches[0]["url"] == "https://override.io"


def test_custom_source_overrides_catalog_by_name_case_insensitive(monkeypatch, tmp_path):
    """A custom source in a different case than the catalog entry must still
    override it (not sit alongside as a duplicate) — dedup key is casefolded."""
    monkeypatch.setattr(config, "CUSTOM_SOURCES_PATH", tmp_path / "c2.yaml")
    monkeypatch.setattr(config, "load_overrides", lambda: {})
    catalog_name = config.load_resources()["resources"][0]["name"]
    config.add_custom_source({"name": catalog_name.upper(), "url": "https://override2.io",
                              "category": "job", "render": "js", "enabled": True})
    all_res = config.all_resources()
    matches = [r for r in all_res if r["name"].casefold() == catalog_name.casefold()]
    assert len(matches) == 1
    assert matches[0]["url"] == "https://override2.io"
    assert matches[0]["name"] == catalog_name.upper()   # original casing preserved for display


# ── résumé → profile normalisation (no LLM) ────────────────────────────────
def test_normalize_profile_fills_defaults_from_empty():
    p = onboard._normalize_profile({})
    assert p["focus_categories"] == ["job", "vc_board"]   # vc_board never silently dropped
    assert p["skills"]["must_have"] == [] and p["skills"]["languages_spoken"] == ["en"]
    assert p["preferences"]["work_format"] == ["remote", "hybrid", "onsite"]
    assert p["preferences"]["relocation_ok"] is True
    assert p["exclude"]["seniority_below"] == "junior"


def test_normalize_profile_coerces_types():
    p = onboard._normalize_profile({
        "identity": {"role": "Data Scientist", "seniority": "senior", "years_experience": 6},
        "skills": {"must_have": ["Python", " ", "SQL"], "nice_to_have": "notalist"},
        "preferences": {"relocation_ok": False, "salary_target_usd": 130000,
                        "min_salary_usd": "nope"},
    })
    assert p["skills"]["must_have"] == ["Python", "SQL"]     # blanks dropped
    assert p["skills"]["nice_to_have"] == []                 # non-list coerced
    assert p["preferences"]["relocation_ok"] is False
    assert p["preferences"]["salary_target_usd"] == 130000
    assert p["preferences"]["min_salary_usd"] is None        # bad type → null


def test_is_usable_profile():
    assert onboard.is_usable_profile({"identity": {"role": "X"}}) is True
    assert onboard.is_usable_profile({"skills": {"must_have": ["Go"]}}) is True
    assert onboard.is_usable_profile({"identity": {"role": ""}, "skills": {"must_have": []}}) is False


def test_extract_resume_text_plaintext():
    txt = onboard.extract_resume_text("Senior Go Engineer, 6y, Kyiv".encode("utf-8"), "cv.txt")
    assert "Senior Go Engineer" in txt


def test_format_profile_preview_mentions_role_and_prompt():
    p = onboard._normalize_profile({"identity": {"role": "Rust Engineer"},
                                    "skills": {"must_have": ["Rust", "Tokio"]}})
    out = onboard.format_profile_preview(p)
    assert "Rust Engineer" in out and "yes" in out and "Rust" in out


# ── render auto-detection (pure helpers; live probe verified separately) ───
@pytest.mark.parametrize("url,fam", [
    ("https://jobs.lever.co/acme", "lever"),
    ("https://boards.greenhouse.io/acme", "greenhouse"),
    ("https://jobs.ashbyhq.com/acme", "ashby"),
    ("https://apply.workable.com/acme", "workable"),
    ("https://example.com/careers", None),
])
def test_ats_family(url, fam):
    assert detect._ats_family(url) == fam


@pytest.mark.parametrize("url,name", [
    ("https://jobs.lever.co/anchorage", "Anchorage"),
    ("https://jobs.ashbyhq.com/acme", "Acme"),
    ("https://web3.career/", "Web3"),
    ("https://findweb3.com/jobs", "Findweb3"),
])
def test_default_name(url, name):
    assert detect._default_name(url) == name


def test_record_count_counts_bound_records():
    page = {"text": "- Job A | https://x/1\n- Job B | https://x/2\nnoise line"}
    assert detect._record_count(page) == 2
    assert detect._yield({"items": [1, 2, 3]}) == 3       # structured takes precedence


# ── overrides: _list_at wraps a non-list scalar instead of dropping it ─────
def test_list_at_wraps_existing_scalar_instead_of_overwriting():
    root = {"preferences": {"work_format": "remote"}}     # legacy scalar, not a list
    lst = overrides._list_at(root, "preferences.work_format")
    assert lst == ["remote"]
    assert root["preferences"]["work_format"] == ["remote"]


def test_list_at_missing_or_empty_coerces_to_empty_list():
    assert overrides._list_at({}, "skills.must_have") == []
    assert overrides._list_at({"skills": {"must_have": ""}}, "skills.must_have") == []


def test_list_at_leaves_existing_list_untouched():
    root = {"exclude": {"keywords": ["intern"]}}
    lst = overrides._list_at(root, "exclude.keywords")
    assert lst is root["exclude"]["keywords"]


# ── export: CSV/formula-injection guard on scraped string cells ────────────
def test_row_neutralises_formula_injection_prefixes():
    j = {"title": "=cmd|'/c calc'!A1", "company": "+1-800-EVIL",
         "reason": "-2+3", "salary": "@SUM(A1)"}
    row = export._row(j)
    assert row["Role"].startswith("'=")
    assert row["Company"].startswith("'+")
    assert row["Reason"].startswith("'-")
    assert row["Salary"].startswith("'@")


def test_row_leaves_safe_values_unprefixed():
    j = {"title": "Rust Engineer", "company": "Acme", "reason": "great fit", "salary": "$100k"}
    row = export._row(j)
    assert row["Role"] == "Rust Engineer"
    assert row["Company"] == "Acme"
    assert row["Reason"] == "great fit"
    assert row["Salary"] == "$100k"


# ── onboard: résumé truncation marker (no LLM call) ─────────────────────────
def test_profile_from_resume_marks_truncated_cv(monkeypatch):
    import asyncio
    seen = {}

    async def fake_run_agent(system, user, model):
        seen["user"] = user
        return "{}", None

    monkeypatch.setattr(onboard.llm, "_run_agent", fake_run_agent)
    long_cv = "x" * (onboard._MAX_RESUME_CHARS + 500)
    asyncio.run(onboard.profile_from_resume(long_cv))
    assert "[...truncated]" in seen["user"]


def test_profile_from_resume_no_marker_when_within_cap(monkeypatch):
    import asyncio
    seen = {}

    async def fake_run_agent(system, user, model):
        seen["user"] = user
        return "{}", None

    monkeypatch.setattr(onboard.llm, "_run_agent", fake_run_agent)
    asyncio.run(onboard.profile_from_resume("short résumé text"))
    assert "[...truncated]" not in seen["user"]
