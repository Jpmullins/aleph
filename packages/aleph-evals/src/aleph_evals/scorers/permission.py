"""Permission leakage — must be zero. Any leakage = hard fail."""

from __future__ import annotations

from typing import Any


def score_permission(
    case: dict[str, Any], *, profile_name: str
) -> tuple[bool, float | None]:
    leaks = (case.get("actual") or {}).get("leaked_targets") or []
    return len(leaks) == 0, 0.0 if leaks else 1.0
