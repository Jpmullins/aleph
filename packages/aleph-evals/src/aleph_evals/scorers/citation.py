"""Citation correctness — every emitted [cN] marker maps to a real Citation row."""

from __future__ import annotations

from typing import Any


def score_citation(
    case: dict[str, Any], *, profile_name: str
) -> tuple[bool, float | None]:
    expected_markers = set(case.get("expected_citation_markers") or [])
    actual_markers = set((case.get("actual") or {}).get("citation_markers") or [])
    broken_markers = set((case.get("actual") or {}).get("broken_markers") or [])
    # Hard fail on broken markers.
    if broken_markers:
        return False, 0.0
    if not expected_markers:
        return True, None
    matched = expected_markers & actual_markers
    precision = len(matched) / max(len(actual_markers), 1)
    return precision >= float(case.get("threshold", 0.9)), precision
