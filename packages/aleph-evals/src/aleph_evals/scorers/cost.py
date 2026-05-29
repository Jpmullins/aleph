"""Cost drift — actual cost within ±drift_pct of baseline per profile."""

from __future__ import annotations

from typing import Any


def score_cost(case: dict[str, Any], *, profile_name: str) -> tuple[bool, float | None]:
    baseline = float((case.get("baselines") or {}).get(profile_name, case.get("baseline", 0)))
    actual = float((case.get("actual") or {}).get("cost_usd", 0))
    drift_pct = float(case.get("drift_pct", 15.0)) / 100.0
    if baseline <= 0:
        return True, None
    diff = abs(actual - baseline) / baseline
    return diff <= drift_pct, diff
