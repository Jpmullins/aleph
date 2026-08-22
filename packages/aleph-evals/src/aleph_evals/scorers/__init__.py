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


class SelfGradingFixture(RuntimeError):
    """A case that supplies the answer it is meant to be graded on."""


def score(
    kind: str,
    case: dict[str, Any],
    *,
    profile_name: str,
    actual: dict[str, Any] | None = None,
) -> tuple[bool, float | None]:
    """Grade one case against what the SYSTEM produced.

    ``actual`` is what a run of Aleph returned. It is a separate argument from
    ``case`` on purpose, and the separation is the whole point of this change.

    Every scorer here read `case["actual"]` — the fixture supplied both the
    expected answer and the observed one, and `_run_dataset` loaded the fixture
    and scored it without executing anything at all. So `pass_rate: 1.0` meant
    "the JSON file agrees with itself". Seven scorers, six datasets, one number
    that could not fail for any reason connected to the code.

    A fixture carrying its own `actual` is therefore REFUSED rather than scored.
    Silently ignoring it would leave the old files scoring 0 and looking like a
    regression; refusing says what is actually wrong, which is that nobody has
    wired an executor for that dataset yet.
    """
    scorer = _REGISTRY.get(kind)
    if scorer is None:
        return True, None
    if "actual" in case:
        msg = (
            f"the {kind!r} fixture carries its own 'actual'. A scorer reading the "
            "answer out of the file it is grading measures nothing — pass the "
            "observed result in as `actual=` from a real run, or delete the key."
        )
        raise SelfGradingFixture(msg)
    merged = {**case, "actual": actual or {}}
    return scorer(merged, profile_name=profile_name)


__all__ = ["SelfGradingFixture", "score"]
