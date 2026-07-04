"""DeepResearch Bench adapter."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aleph_evals.adapters._research_round_trip import ResearchRoundTripDriver


class DeepResearchBenchAdapter:
    """DeepResearch Bench → native research round-trip wrapper.

    Same live-stack-dependent shape as FreshQA, but dispatches at
    `depth="deep"` so the loop exercises its full iteration budget.
    """

    kind = "deepresearch"

    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        project_id: str | None = None,
        auth_token: str | None = None,
    ) -> None:
        self._driver = ResearchRoundTripDriver(
            api_base_url=api_base_url,
            project_id=project_id,
            auth_token=auth_token,
            depth="deep",
        )

    def run_cases(self, cases: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        self._driver.require_configured("DeepResearch Bench adapter")
        for case in cases:
            topic = str(case.get("topic") or case.get("prompt") or case.get("question") or "")
            result = self._driver.run_case(topic)
            passed = result["status"] == "completed"
            yield {
                "case_key": case.get("id"),
                "passed": passed,
                "score": 1.0 if passed else 0.0,
                "actual": result,
            }
