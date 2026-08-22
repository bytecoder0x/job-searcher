"""Characterization tests for src/config.py — overrides, profile edits, windows."""
from __future__ import annotations

import json

from src import config


# ── overrides round-trip ─────────────────────────────────────────────────
def test_source_override_toggles_enabled_resources(tmp_overrides):
    before = {r["name"] for r in config.enabled_resources()}
    assert "Web3 Career" in before
    config.update_override("sources", "Web3 Career", False)
    after = {r["name"] for r in config.enabled_resources()}
    assert "Web3 Career" not in after
    config.update_override("sources", "Web3 Career", True)
    assert "Web3 Career" in {r["name"] for r in config.enabled_resources()}


def test_meta_override_changes_meta_and_threshold(tmp_overrides):
    assert config.score_threshold() == 60   # default
    config.update_override("meta", "score_threshold", 75)
    assert config.resources_meta()["score_threshold"] == 75
    assert config.score_threshold() == 75


def test_clear_overrides_restores_everything(tmp_overrides):
    config.update_override("sources", "Web3 Career", False)
    config.update_override("meta", "score_threshold", 90)
    config.clear_overrides()
    assert "Web3 Career" in {r["name"] for r in config.enabled_resources()}
    assert config.score_threshold() == 60


def test_load_overrides_missing_file_returns_empty(tmp_overrides):
    assert not tmp_overrides.exists()
    assert config.load_overrides() == {}


def test_load_overrides_corrupt_file_returns_empty(tmp_overrides):
    tmp_overrides.parent.mkdir(parents=True, exist_ok=True)
    tmp_overrides.write_text("{not json", encoding="utf-8")
    assert config.load_overrides() == {}


# ── apply_profile_edits + load_profile merge ─────────────────────────────
def test_add_skill_no_dup_on_readd(tmp_overrides):
    config.apply_profile_edits([{"op": "add", "field": "must_have_skill", "value": "Zig"}])
    config.apply_profile_edits([{"op": "add", "field": "must_have_skill", "value": "Zig"}])
    skills = config.load_profile()["skills"]["must_have"]
    assert skills.count("Zig") == 1


def test_remove_work_format_value(tmp_overrides):
    config.apply_profile_edits([{"op": "remove", "field": "work_format", "value": "onsite"}])
    formats = config.load_profile()["preferences"]["work_format"]
    assert "onsite" not in formats
    assert "remote" in formats


def test_set_scalar_field(tmp_overrides):
    config.apply_profile_edits([{"op": "set", "field": "min_salary", "value": 50000}])
    assert config.load_profile()["preferences"]["min_salary_usd"] == 50000


def test_add_then_remove_nets_removed(tmp_overrides):
    config.apply_profile_edits([{"op": "add", "field": "must_have_skill", "value": "Zig"}])
    config.apply_profile_edits([{"op": "remove", "field": "must_have_skill", "value": "Zig"}])
    skills = config.load_profile()["skills"]["must_have"]
    assert "Zig" not in skills


def test_set_twice_last_wins(tmp_overrides):
    config.apply_profile_edits([{"op": "set", "field": "min_salary", "value": 1}])
    config.apply_profile_edits([{"op": "set", "field": "min_salary", "value": 2}])
    assert config.load_profile()["preferences"]["min_salary_usd"] == 2


def test_invalid_op_skipped_without_raising(tmp_overrides):
    summaries = config.apply_profile_edits([{"op": "bogus", "field": "min_salary", "value": 1}])
    assert any("skipped" in s for s in summaries)
    # min_salary_usd unchanged from profile.yaml's null
    assert config.load_profile()["preferences"]["min_salary_usd"] is None


def test_malformed_profile_override_falls_back_to_raw(tmp_overrides, monkeypatch):
    config.save_overrides({"profile": "not-a-dict"})
    # _apply_profile_overrides raises on .get('set') of a str → load_profile degrades
    profile = config.load_profile()
    raw = config._read_yaml_dict(config.profile_base_path())

    assert profile["identity"]["name"] == raw["identity"]["name"]   # degraded to raw


# ── window helpers ────────────────────────────────────────────────────────
def test_default_and_max_window_days_defaults(tmp_overrides):
    assert config.default_window_days() == 1
    assert config.max_window_days() == 7


def test_window_days_overridable(tmp_overrides):
    config.update_override("meta", "default_window_days", 3)
    config.update_override("meta", "max_window_days", 14)
    assert config.default_window_days() == 3
    assert config.max_window_days() == 14


def test_matcher_input_cap_default(tmp_overrides):
    assert config.matcher_input_cap() == 500
