"""Shared native-research round-trip driver for benchmark adapters.

Each benchmark case becomes a `POST /v1/projects/{id}/synthesize` call on a
live aleph-api; the driver polls `GET /v1/projects/{id}/agent-runs` until the
dispatched run completes and scores the case on run success. Requires a
running stack — the adapters raise `RuntimeError` up front when unconfigured
so the eval runner skips the dataset gracefully.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

_TERMINAL_STATUSES = frozenset({"completed", "failed"})


class ResearchRoundTripDriver:
    """Drives one benchmark case through the native research loop."""

    def __init__(
        self,
        *,
        api_base_url: str | None,
        project_id: str | None,
        auth_token: str | None = None,
        depth: str = "shallow",
        poll_interval_s: float = 5.0,
        timeout_s: float = 600.0,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/") if api_base_url else None
        self._project_id = project_id
        self._auth_token = auth_token
        self._depth = depth
        self._poll_interval_s = poll_interval_s
        self._timeout_s = timeout_s

    def require_configured(self, adapter_name: str) -> None:
        if not self._api_base_url or not self._project_id:
            msg = (
                f"{adapter_name} runs against the native research loop and needs "
                "a live stack: pass api_base_url + project_id (aleph-api base URL "
                "and the target project)."
            )
            raise RuntimeError(msg)

    def run_case(self, topic: str) -> dict[str, Any]:
        """Dispatch research on `topic`; block until the run is terminal."""
        assert self._api_base_url is not None and self._project_id is not None
        headers: dict[str, str] = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        base = f"{self._api_base_url}/v1/projects/{self._project_id}"
        with httpx.Client(headers=headers, timeout=30.0) as client:
            resp = client.post(f"{base}/synthesize", json={"topic": topic, "depth": self._depth})
            resp.raise_for_status()
            agent_run_id = resp.json()["agent_run_id"]
            deadline = time.monotonic() + self._timeout_s
            while time.monotonic() < deadline:
                runs = client.get(f"{base}/agent-runs", params={"limit": 100})
                runs.raise_for_status()
                run = next((r for r in runs.json() if r["id"] == agent_run_id), None)
                if run is not None and run["status"] in _TERMINAL_STATUSES:
                    return {
                        "agent_run_id": agent_run_id,
                        "status": run["status"],
                        "error_text": run.get("error_text"),
                    }
                time.sleep(self._poll_interval_s)
        return {"agent_run_id": agent_run_id, "status": "timeout", "error_text": None}
