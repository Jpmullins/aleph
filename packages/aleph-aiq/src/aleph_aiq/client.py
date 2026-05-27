"""AIQ REST client.

The actual AIQ server is vendored at `vendor/aiq` as a git submodule
(see the Inc 3 spec §3.3) and runs as the `aiq-server` compose service
on port 8001. This client wraps the REST endpoints we depend on:

  POST /v1/jobs/async/agents
  GET  /v1/jobs/{id}
  GET  /v1/jobs/{id}/stream     (SSE)
  POST /v1/jobs/{id}/cancel
  POST /v1/jobs/{id}/clarify
  GET  /v1/health

Note: until the AIQ submodule is checked out and the aiq-server image
is built, calls from this client will fail at connect time. That's
intentional — operators bootstrap the submodule once, then this client
is the boundary the rest of Aleph uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import httpx


class AIQJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
            resp = await self._http.get(f"{self._base}/v1/health", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def dispatch_deep(
        self,
        *,
        project_id: UUID,
        topic: str,
        allowed_data_sources: list[str],
        clarifier_enabled: bool = True,
    ) -> str:
        resp = await self._http.post(
            f"{self._base}/v1/jobs/async/agents",
            json={
                "query": topic,
                "depth": "deep",
                "project_id": str(project_id),
                "allowed_data_sources": allowed_data_sources,
                "clarifier_enabled": clarifier_enabled,
            },
            headers=self._headers(),
        )
        resp.raise_for_status()
        body = resp.json()
        return str(body["job_id"])

    async def get_job(self, job_id: str) -> AIQJobOut:
        resp = await self._http.get(
            f"{self._base}/v1/jobs/{job_id}", headers=self._headers()
        )
        resp.raise_for_status()
        body = resp.json()
        return AIQJobOut(
            job_id=str(body["job_id"]),
            status=AIQJobStatus(body["status"]),
            output=body.get("output"),
            error=body.get("error"),
        )

    async def cancel(self, job_id: str) -> None:
        resp = await self._http.post(
            f"{self._base}/v1/jobs/{job_id}/cancel", headers=self._headers()
        )
        resp.raise_for_status()

    async def clarify(self, job_id: str, *, answer: str) -> None:
        resp = await self._http.post(
            f"{self._base}/v1/jobs/{job_id}/clarify",
            json={"answer": answer},
            headers=self._headers(),
        )
        resp.raise_for_status()
