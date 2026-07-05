"""Read-only Langfuse query client for platform self-diagnosis.

The platform emits its own traces to Langfuse (OTEL → collector → Langfuse v3).
This reader closes the loop: it lets in-process code — notably the Live agent's
`diagnose_platform` tool — read those traces back to answer "what is happening"
and "what is broken" without a human opening the Langfuse UI.

Strictly READ-ONLY. Every method is a GET against the Langfuse public REST API
(`/api/public/*`); there is no write path here by design (observability must
never mutate platform state, and Langfuse is not a source of truth). This is a
plain HTTP read, not an LLM/tool call, so it writes no `ModelCall`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx


@dataclass(slots=True)
class ErrorObservation:
    """One ERROR-level observation (a failed LLM call, tool error, exception)."""

    id: str
    name: str
    trace_id: str
    start_time: str
    status_message: str


@dataclass(slots=True)
class DiagnosticSnapshot:
    """A window's worth of platform health, aggregated for a readable summary."""

    window_hours: int
    since: str
    total_traces: int
    total_observations: int
    error_count: int
    recent_errors: list[ErrorObservation]


class LangfuseReader:
    """Async, read-only wrapper over the Langfuse public REST API."""

    def __init__(
        self,
        *,
        host: str,
        public_key: str,
        secret_key: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._host = host.rstrip("/")
        self._auth = httpx.BasicAuth(public_key, secret_key)
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        resp = await self._client.get(
            f"{self._host}/api/public/{path}", params=clean, auth=self._auth
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    async def list_traces(
        self,
        *,
        limit: int = 20,
        from_timestamp: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Recent traces (newest first) with paging metadata (`meta.totalItems`)."""
        return await self._get(
            "traces", {"limit": limit, "fromTimestamp": from_timestamp, "name": name}
        )

    async def list_observations(
        self,
        *,
        limit: int = 20,
        level: str | None = None,
        type: str | None = None,
        from_start_time: str | None = None,
    ) -> dict[str, Any]:
        """Observations (spans/generations/events); filter by `level`
        (DEBUG|DEFAULT|WARNING|ERROR), `type`, or ISO-8601 `from_start_time`."""
        return await self._get(
            "observations",
            {
                "limit": limit,
                "level": level,
                "type": type,
                "fromStartTime": from_start_time,
            },
        )

    async def daily_metrics(
        self, *, from_timestamp: str | None = None, to_timestamp: str | None = None
    ) -> dict[str, Any]:
        """Daily rollups of trace counts, token usage, and cost."""
        return await self._get(
            "metrics/daily", {"fromTimestamp": from_timestamp, "toTimestamp": to_timestamp}
        )

    async def diagnostic_snapshot(
        self, *, window_hours: int = 24, error_limit: int = 10
    ) -> DiagnosticSnapshot:
        """Aggregate a window of platform activity into a health summary: trace
        and observation totals plus the most recent ERROR-level observations."""
        since = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
        traces = await self.list_traces(limit=1, from_timestamp=since)
        errors = await self.list_observations(
            limit=error_limit, level="ERROR", from_start_time=since
        )
        all_obs = await self.list_observations(limit=1, from_start_time=since)

        def _meta_total(payload: dict[str, Any]) -> int:
            meta = payload.get("meta")
            if isinstance(meta, dict):
                total = cast("dict[str, Any]", meta).get("totalItems", 0)
                try:
                    return int(total or 0)
                except (TypeError, ValueError):
                    return 0
            return 0

        recent: list[ErrorObservation] = []
        for obs in cast("list[dict[str, Any]]", errors.get("data", [])):
            recent.append(
                ErrorObservation(
                    id=str(obs.get("id", "")),
                    name=str(obs.get("name") or "(unnamed)"),
                    trace_id=str(obs.get("traceId", "")),
                    start_time=str(obs.get("startTime", "")),
                    status_message=str(obs.get("statusMessage") or ""),
                )
            )
        return DiagnosticSnapshot(
            window_hours=window_hours,
            since=since,
            total_traces=_meta_total(traces),
            total_observations=_meta_total(all_obs),
            error_count=_meta_total(errors),
            recent_errors=recent,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
