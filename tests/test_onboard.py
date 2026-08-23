"""The `onboard` command: reading the résumé, refusing an unreadable one, and
the save confirmation. The LLM call is faked."""
from __future__ import annotations

from src import config, onboard
from src.__main__ import main

_PROFILE = {
    "identity": {"role": "Data Engineer", "seniority": "senior", "years_experience": 5},
    "skills": {"must_have": ["Python", "SQL"], "nice_to_have": []},
    "preferences": {"work_format": ["remote"], "relocation_ok": False},
    "focus_categories": ["job"],
}


def _fake_llm(monkeypatch, profile=None):
    async def fake(_text):
        return profile if profile is not None else _PROFILE

    monkeypatch.setattr(onboard, "profile_from_resume", fake)


def _resume(tmp_path, text="Senior Data Engineer with 5 years of Python and SQL. " * 4):
    path = tmp_path / "cv.txt"
    path.write_text(text, encoding="utf-8")

    return str(path)


def test_saves_the_profile_when_confirmed(monkeypatch, tmp_path, capsys):
    saved = {}
    _fake_llm(monkeypatch)
    monkeypatch.setattr(config, "save_active_profile", lambda p: saved.update(p))
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    assert main(["onboard", _resume(tmp_path)]) == 0

    out = capsys.readouterr()

    assert "Data Engineer" in out.out                 # preview shown before saving
    assert saved["identity"]["role"] == "Data Engineer"


def test_yes_flag_skips_the_prompt(monkeypatch, tmp_path, capsys):
    saved = {}
    _fake_llm(monkeypatch)
    monkeypatch.setattr(config, "save_active_profile", lambda p: saved.update(p))

    def _no_input(_prompt):
        raise AssertionError("must not ask when -y is given")

    monkeypatch.setattr("builtins.input", _no_input)

    assert main(["onboard", "-y", _resume(tmp_path)]) == 0
    assert saved

    capsys.readouterr()


def test_declining_leaves_the_profile_untouched(monkeypatch, tmp_path, capsys):
    _fake_llm(monkeypatch)

    def _must_not_save(_p):
        raise AssertionError("declined, yet it saved")

    monkeypatch.setattr(config, "save_active_profile", _must_not_save)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    assert main(["onboard", _resume(tmp_path)]) == 1
    assert "not saved" in capsys.readouterr().err


def test_a_scanned_pdf_is_refused_before_spending_a_token(monkeypatch, tmp_path, capsys):
    def _must_not_call(_text):
        raise AssertionError("called the model on unreadable input")

    monkeypatch.setattr(onboard, "profile_from_resume", _must_not_call)

    assert main(["onboard", _resume(tmp_path, text="page 1")]) == 1
    assert "scanned/image PDF" in capsys.readouterr().err


def test_missing_file_reports_instead_of_raising(tmp_path, capsys):
    assert main(["onboard", str(tmp_path / "nope.pdf")]) == 1
    assert "cannot read the resume" in capsys.readouterr().err


def test_unusable_model_output_is_not_saved(monkeypatch, tmp_path, capsys):
    _fake_llm(monkeypatch, profile={"identity": {}, "skills": {"must_have": []}})

    def _must_not_save(_p):
        raise AssertionError("saved an unusable profile")

    monkeypatch.setattr(config, "save_active_profile", _must_not_save)

    assert main(["onboard", _resume(tmp_path)]) == 1
    assert "usable profile" in capsys.readouterr().err
