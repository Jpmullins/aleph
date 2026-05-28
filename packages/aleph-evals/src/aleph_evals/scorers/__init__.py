"""Eval scorers — one per dataset kind."""

from __future__ import annotations

from typing import Any

from aleph_evals.scorers.citation import score_citation
from aleph_evals.scorers.cost import score_cost
from aleph_evals.scorers.coverage import score_coverage
from aleph_evals.scorers.permission import score_permission
from aleph_evals.scorers.retrieval import score_retrieval
from aleph_evals.scorers.synthesis import score_synthesis


_REGISTRY = {
    "retrieval": score_retrieval,
    "citation": score_citation,
    "coverage": score_coverage,
    "permission": score_permission,
    "synthesis": score_synthesis,
    "cost": score_cost,
    "metric_only": lambda case, profile_name: (True, None),
}


def score(kind: str, case: dict[str, Any], *, profile_name: str) -> tuple[bool, float | None]:
    scorer = _REGISTRY.get(kind)
    if scorer is None:
        return True, None
    return scorer(case, profile_name=profile_name)


__all__ = ["score"]
