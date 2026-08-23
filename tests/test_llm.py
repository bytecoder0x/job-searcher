"""Characterization tests for src/llm.py's JSON parsing helpers — pin CURRENT behavior."""
from __future__ import annotations

import pytest

from src import llm


# ── _parse_json_array / _parse_json_object / _parse_int_array ───────────
@pytest.mark.parametrize("text,expected", [
    ('[{"a": 1}]', [{"a": 1}]),
    ('```json\n[{"a": 1}]\n```', [{"a": 1}]),
    ("not json at all", []),
    ("[]", []),
    ('[1, 2, {"a": 1}]', [{"a": 1}]),   # non-dict elements dropped
])
def test_parse_json_array(text, expected):
    assert llm._parse_json_array(text) == expected


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ("garbage", {}),
    ("{}", {}),
])
def test_parse_json_object(text, expected):
    assert llm._parse_json_object(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("[0, 2, 4]", [0, 2, 4]),
    ("```json\n[0, 1]\n```", [0, 1]),
    ("junk", None),
    ("[]", []),           # valid empty answer, distinct from unparseable
    ("not an array {}", None),
])
def test_parse_int_array(text, expected):
    assert llm._parse_int_array(text) == expected
