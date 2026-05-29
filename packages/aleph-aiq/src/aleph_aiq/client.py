"""AIQ REST client.

Wraps the NVIDIA AI-Q Blueprint (`aiq-agent:2.0.0`) HTTP API as deployed by
the `aiq-server` compose service. The endpoints this client depends on (from
the image's OpenAPI):

  GET  /health
  POST /v1/jobs/async/submit          {agent_type, input}
  GET  /v1/jobs/async/job/{id}
  GET  /v1/jobs/async/job/{id}/state
  GET  /v1/jobs/async/job/{id}/stream (SSE)
  POST /v1/jobs/async/job/{id}/cancel

`agent_type` is one of the image's registered agents — `deep_researcher`
(thorough, multi-loop) or `shallow_researcher` (fast). The submit body is
generic ({agent_type, input}); per-project data-source scoping is applied via
the project's AIQ config, not the submit call.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import httpx


class AIQJobStatus(StrEnum):
    SUBMITTED = "submitted"
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def _missing_(cls, value: object) -> AIQJobStatus:
        # AIQ may report statuses we don't model (e.g. "completed",
        # "in_progress"); fold unknowns into a coarse bucket rather than
        # raising, so polling never crashes on a new status string.
        text = str(value).lower()
        if text in {"completed", "complete", "done", "success"}:
            return cls.SUCCEEDED
        if text in {"error", "failure"}:
            return cls.FAILED
        if text in {"in_progress", "processing", "started"}:
            return cls.RUNNING
        return cls.PENDING


@dataclass(frozen=True)
class AIQJobOut:
    job_id: str
    status: AIQJobStatus
    output: dict[str, Any] | None
    error: str | None


class AIQClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = service_token
        self._http = http_client or httpx.AsyncClient(timeout=60.0)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def health(self) -> bool:
        try:
            resp = await self._http.get(f"{self._base}/health", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def dispatch_deep(
        self,
        *,
        project_id: UUID,
        topic: str,
        allowed_data_sources: list[str],
        depth: str = "deep",
        clarifier_enabled: bool = True,
    ) -> str:
        """Submit an async research job; returns the AIQ job id.

        `project_id`/`allowed_data_sources`/`clarifier_enabled` are part of
        Aleph's request model but not the image's generic submit contract
        (which is just {agent_type, input}); they're carried for tracing and
        for the per-project AIQ config, not sent on this call.
        """
        agent_type = "deep_researcher" if depth == "deep" else "shallow_researcher"
        resp = await self._http.post(
            f"{self._base}/v1/jobs/async/submit",
            json={"agent_type": agent_type, "input": topic},
            headers=self._headers(),
        )
        resp.raise_for_status()
        body = resp.json()
        return str(body["job_id"])

    async def get_job(self, job_id: str) -> AIQJobOut:
        resp = await self._http.get(
            f"{self._base}/v1/jobs/async/job/{job_id}", headers=self._headers()
        )
        resp.raise_for_status()
        body = resp.json()
        return AIQJobOut(
            job_id=str(body.get("job_id", job_id)),
            status=AIQJobStatus(body.get("status", "pending")),
            output=body.get("output"),
            error=body.get("error"),
        )

    async def get_report(self, job_id: str) -> str | None:
        """Fetch the completed research report markdown, or None if absent."""
        resp = await self._http.get(
            f"{self._base}/v1/jobs/async/job/{job_id}/report", headers=self._headers()
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("has_report"):
            return None
        report = body.get("report")
        return str(report) if report else None

    async def get_state(self, job_id: str) -> dict[str, Any]:
        """Fetch the job's state + artifacts (citation sources, tool outputs)."""
        resp = await self._http.get(
            f"{self._base}/v1/jobs/async/job/{job_id}/state", headers=self._headers()
        )
        resp.raise_for_status()
        body = resp.json()
        artifacts = body.get("artifacts")
        return artifacts if isinstance(artifacts, dict) else {}

    async def cancel(self, job_id: str) -> None:
        resp = await self._http.post(
            f"{self._base}/v1/jobs/async/job/{job_id}/cancel", headers=self._headers()
        )
        resp.raise_for_status()
