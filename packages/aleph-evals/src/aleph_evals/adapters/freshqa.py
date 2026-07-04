"""FreshQA harness adapter.

Runs FreshQA questions through the native research loop: each case's
question dispatches `POST /v1/projects/{id}/synthesize` and the case
passes when the research run completes. The adapter shape lets the eval
runner treat research benchmarks as just another dataset kind.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aleph_evals.adapters._research_round_trip import ResearchRoundTripDriver


class FreshQAAdapter:
    """FreshQA → native research round-trip wrapper.

    Requires a live stack (aleph-api base URL + target project). When
    unconfigured, `run_cases` raises `RuntimeError` with a clear message
    so the eval runner skips this dataset gracefully.
    """

    kind = "freshqa"

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
            depth="shallow",
        )

    def run_cases(self, cases: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        self._driver.require_configured("FreshQA adapter")
        for case in cases:
            topic = str(case.get("question") or case.get("topic") or "")
            result = self._driver.run_case(topic)
            passed = result["status"] == "completed"
            yield {
                "case_key": case.get("id") or case.get("question"),
                "passed": passed,
                "score": 1.0 if passed else 0.0,
                "actual": result,
            }
