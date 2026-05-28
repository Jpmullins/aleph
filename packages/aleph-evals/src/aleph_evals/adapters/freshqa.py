"""FreshQA harness adapter.

Wraps AIQ's FreshQA benchmark suite (lives under `vendor/aiq` once the
submodule is checked out). The adapter shape lets the eval runner
treat AIQ benchmarks as just another dataset kind.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class FreshQAAdapter:
    """AIQ FreshQA wrapper.

    Inc 8 ships the adapter shape. The actual harness invocation
    depends on the vendored AIQ release providing the FreshQA module;
    `run_cases` lazily imports `aiq_agent.evals.freshqa` and returns
    structured per-case results. When AIQ isn't vendored, the adapter
    raises `RuntimeError` with a clear message so the eval runner
    skips this dataset gracefully.
    """

    kind = "freshqa"

    def __init__(self, *, aiq_base_url: str | None = None) -> None:
        self._aiq_base_url = aiq_base_url

    def run_cases(
        self, cases: Iterable[dict[str, Any]]
    ) -> Iterable[dict[str, Any]]:
        try:
            import aiq_agent.evals.freshqa as freshqa_mod  # type: ignore[import-not-found]
        except ImportError as exc:
            msg = (
                "FreshQA adapter requires the AIQ submodule at vendor/aiq. "
                "Vendor AIQ per docs/operations/aiq-runbook.md, then retry."
            )
            raise RuntimeError(msg) from exc
        for case in cases:
            score = freshqa_mod.score_case(case)  # type: ignore[attr-defined]
            yield {
                "case_key": case.get("id") or case.get("question"),
                "passed": bool(score.get("passed")),
                "score": float(score.get("score", 0)),
                "actual": score,
            }
