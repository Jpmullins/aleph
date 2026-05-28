"""Retrieval recall — measures top-K coverage of expected pages."""

from __future__ import annotations

from typing import Any


def score_retrieval(
    case: dict[str, Any], *, profile_name: str
) -> tuple[bool, float | None]:
    expected = set(case.get("expected_page_slugs") or [])
    actual = set((case.get("actual") or {}).get("page_slugs") or [])
    if not expected:
        return True, None
    hits = len(expected & actual)
    recall = hits / len(expected)
    threshold = float(case.get("threshold", 0.7))
    return recall >= threshold, recall
