"""Synthesis-flag precision — coverage_judgment matches expected."""

from __future__ import annotations

from typing import Any


def score_synthesis(case: dict[str, Any], *, profile_name: str) -> tuple[bool, float | None]:
    expected = case.get("expected_judgment")
    actual = (case.get("actual") or {}).get("coverage_judgment")
    if expected is None:
        return True, None
    return expected == actual, 1.0 if expected == actual else 0.0
