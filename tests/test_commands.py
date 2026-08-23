"""The profile / sources / config commands. Everything runs through the real
dispatcher; no network — source detection is stubbed."""
from __future__ import annotations

import pytest

from src import commands, config
from src.__main__ import main


@pytest.fixture
def isolated(tmp_overrides, tmp_path, monkeypatch):
    """Overrides and the custom-source file both in tmp — never the real data/."""
    monkeypatch.setattr(config, "CUSTOM_SOURCES_PATH", tmp_path / "sources.custom.yaml")

    return tmp_path


# ── profile ────────────────────────────────────────────────────────────────
def test_profile_shows_the_effective_values(isolated, capsys):
    assert main(["profile"]) == 0

    out = capsys.readouterr().out

    assert "role" in out and "must_have" in out
    assert "base file" in out            # which layer is in force


def test_profile_fields_lists_what_can_be_edited(isolated, capsys):
    assert main(["profile", "--fields"]) == 0

    out = capsys.readouterr().out

    assert "must_have_skill" in out and "list" in out
    assert "min_salary" in out and "scalar" in out


def test_set_coerces_a_numeric_scalar(isolated, capsys):
    assert main(["profile", "--set", "min_salary", "90000"]) == 0

    capsys.readouterr()

    assert config.load_profile()["preferences"]["min_salary_usd"] == 90000   # int, not "90000"


def test_set_coerces_a_boolean_scalar(isolated, capsys):
    assert main(["profile", "--set", "relocation_ok", "false"]) == 0

    capsys.readouterr()

    assert config.load_profile()["preferences"]["relocation_ok"] is False


def test_add_and_remove_a_list_field(isolated, capsys):
    assert main(["profile", "--add", "must_have_skill", "Zig"]) == 0
    capsys.readouterr()

    assert "Zig" in config.load_profile()["skills"]["must_have"]

    assert main(["profile", "--remove", "must_have_skill", "Zig"]) == 0
    capsys.readouterr()

    assert "Zig" not in config.load_profile()["skills"]["must_have"]


def test_an_unknown_field_fails_loudly_and_changes_nothing(isolated, capsys):
    assert main(["profile", "--set", "favourite_colour", "green"]) == 1
    assert "skipped" in capsys.readouterr().out
    assert config.load_overrides().get("profile", {}).get("set", {}) == {}


def test_scalar_field_rejects_add(isolated, capsys):
    assert main(["profile", "--add", "min_salary", "1"]) == 1
    assert "only supports 'set'" in capsys.readouterr().out


def test_reset_drops_the_overrides(isolated, capsys):
    main(["profile", "--set", "min_salary", "1234"])
    capsys.readouterr()

    assert main(["profile", "--reset"]) == 0
    capsys.readouterr()

    assert config.load_profile()["preferences"]["min_salary_usd"] != 1234


# ── sources ────────────────────────────────────────────────────────────────
def test_sources_counts_by_category(isolated, capsys):
    assert main(["sources"]) == 0

    out = capsys.readouterr().out

    assert out.startswith("enabled:")
    assert "job:" in out


def test_sources_list_marks_state_and_added_boards(isolated, capsys):
    config.add_custom_source({"name": "My Board", "url": "https://x.io/jobs",
                              "category": "job", "render": "static", "enabled": True})

    assert main(["sources", "--list"]) == 0

    out = capsys.readouterr().out

    assert "My Board" in out and "(added)" in out
    assert out.startswith(("on ", "off"))


def test_toggling_a_board_survives_into_enabled_resources(isolated, capsys):
    name = config.enabled_resources()[0]["name"]

    assert main(["sources", "--off", name]) == 0
    capsys.readouterr()

    assert name not in {r["name"] for r in config.enabled_resources()}

    assert main(["sources", "--on", name]) == 0
    capsys.readouterr()

    assert name in {r["name"] for r in config.enabled_resources()}


def test_removing_a_board_that_was_never_added_is_an_error(isolated, capsys):
    assert main(["sources", "--remove", "Nope"]) == 1
    assert "no added source" in capsys.readouterr().err


def test_add_probes_the_url_and_stores_what_it_found(isolated, monkeypatch, capsys):
    async def fake_detect(url, category="job", name=None):
        return {"name": name or "Acme", "url": url, "category": category,
                "render": "ashby", "enabled": True, "notes": "probed",
                "detected_count": 12}

    monkeypatch.setattr(commands.detect, "detect_source", fake_detect)

    assert main(["sources", "--add", "https://jobs.ashbyhq.com/acme",
                 "--category", "vc_board"]) == 0

    out = capsys.readouterr().out
    saved = config.load_custom_sources()[0]

    assert "12 postings" in out
    assert saved["render"] == "ashby" and saved["category"] == "vc_board"
    assert "detected_count" not in saved       # probe metadata is not persisted


def test_add_reports_a_board_it_could_not_parse(isolated, monkeypatch, capsys):
    async def nothing(url, category="job", name=None):
        return None

    monkeypatch.setattr(commands.detect, "detect_source", nothing)

    assert main(["sources", "--add", "https://example.com"]) == 1
    assert "no postings parsed" in capsys.readouterr().err
    assert config.load_custom_sources() == []


def test_a_failing_probe_reports_instead_of_raising(isolated, monkeypatch, capsys):
    async def boom(url, category="job", name=None):
        raise RuntimeError("dns exploded")

    monkeypatch.setattr(commands.detect, "detect_source", boom)

    assert main(["sources", "--add", "https://example.com"]) == 1
    assert "dns exploded" in capsys.readouterr().err


# ── config ─────────────────────────────────────────────────────────────────
def test_config_prints_effective_values_not_raw_yaml(isolated, capsys):
    assert main(["config"]) == 0

    out = capsys.readouterr().out

    assert "threshold  60" in out          # defaulted, never printed as None
    assert "cap        500" in out


def test_setting_a_value_is_picked_up_by_the_pipeline(isolated, capsys):
    assert main(["config", "--set", "threshold", "75"]) == 0
    capsys.readouterr()

    assert config.score_threshold() == 75


def test_out_of_range_and_unknown_keys_are_refused(isolated, capsys):
    assert main(["config", "--set", "threshold", "500"]) == 1
    assert "between 0 and 100" in capsys.readouterr().err

    assert main(["config", "--set", "nonsense", "3"]) == 1
    assert "usage:" in capsys.readouterr().err

    assert config.score_threshold() == 60       # untouched by either attempt


def test_reset_restores_the_yaml_defaults(isolated, capsys):
    main(["config", "--set", "window", "5"])
    capsys.readouterr()

    assert main(["config", "--reset"]) == 0
    capsys.readouterr()

    assert config.default_window_days() == 1
