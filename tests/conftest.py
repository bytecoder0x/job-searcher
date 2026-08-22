"""Shared fixtures: isolated overrides/DB/export paths, sample data, fake agent."""
from __future__ import annotations

import pytest

from src import config, overrides, store


@pytest.fixture
def tmp_overrides(tmp_path, monkeypatch):
    """Points overrides.OVERRIDES_PATH at a temp file — never touches data/overrides.json."""
    monkeypatch.setattr(overrides, "OVERRIDES_PATH", tmp_path / "overrides.json")
    return tmp_path / "overrides.json"


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Points store._DB at a temp sqlite file — never touches data/seen.db."""
    monkeypatch.setattr(store, "_DB", tmp_path / "seen.db")
    return tmp_path / "seen.db"


@pytest.fixture
def tmp_export_dir(tmp_path, monkeypatch):
    """Points config.EXPORT_DIR at a temp dir — never writes into data/exports."""
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path)
    return tmp_path


def _make_position(**kw) -> dict:
    base = {"title": "Backend Engineer", "company": "Acme", "url": "https://x.test/1",
            "location": "Remote", "remote": True, "salary": None, "posted": "today",
            "tags": [], "category": "job", "source": "Test Board"}
    base.update(kw)

    return base


@pytest.fixture
def make_position():
    """Factory for a minimal valid parse-vacancy position dict, overridable via kwargs."""
    return _make_position


class FakeAgent:
    """Stand-in for llm._run_agent: pops scripted (text) responses in call
    order, or raises `raises` if set. Records every user prompt in .calls.
    Patch with monkeypatch.setattr(llm, "_run_agent", ...) — callers look it
    up as a module attribute at call time, so patching the llm module (not
    the caller's module) is what actually intercepts the call."""

    def __init__(self, responses: list[str] | None = None, raises: Exception | None = None):
        self.responses = list(responses or [])
        self.raises = raises
        self.calls: list[str] = []

    async def __call__(self, system, user, model, max_turns=None, resume=None):
        self.calls.append(user)

        if self.raises:
            raise self.raises

        text = self.responses.pop(0) if self.responses else "[]"

        return text, "fake-session-id"


@pytest.fixture
def fake_agent():
    """The FakeAgent class itself (tests instantiate with scripted responses)."""
    return FakeAgent
