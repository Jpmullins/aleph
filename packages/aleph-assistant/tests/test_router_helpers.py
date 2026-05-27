"""Pure-function tests for the wiki-first retrieval router."""

from __future__ import annotations

from aleph_assistant.retrieval.router import _safe_json


def test_safe_json_object() -> None:
    assert _safe_json('{"a": 1}') == {"a": 1}


def test_safe_json_recovers_from_prefix() -> None:
    out = _safe_json('here is the answer:\n\n{"a": 1, "b": [2]}')
    assert out == {"a": 1, "b": [2]}


def test_safe_json_empty_on_garbage() -> None:
    assert _safe_json("definitely not json") == {}


def test_safe_json_empty_on_truncated() -> None:
    assert _safe_json("{") == {}
