"""DeepResearch Bench adapter."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class DeepResearchBenchAdapter:
    """AIQ DeepResearch Bench wrapper. Same vendor-dependent shape as FreshQA."""

    kind = "deepresearch"

    def run_cases(self, cases: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        try:
            import aiq_agent.evals.deepresearch_bench as dr_mod  # type: ignore[import-not-found]
        except ImportError as exc:
            msg = (
                "DeepResearch Bench adapter requires the AIQ submodule at "
                "vendor/aiq. See docs/operations/aiq-runbook.md."
            )
            raise RuntimeError(msg) from exc
        for case in cases:
            score = dr_mod.score_case(case)  # type: ignore[attr-defined]
            yield {
                "case_key": case.get("id"),
                "passed": bool(score.get("passed")),
                "score": float(score.get("score", 0)),
                "actual": score,
            }
