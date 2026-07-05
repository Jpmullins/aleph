"""Read-only Langfuse MCP server for Aleph self-diagnosis.

Exposes the Langfuse public REST API as MCP tools so a Claude Code session (or
any MCP client) can inspect the platform's own traces/observations/scores to
diagnose failures, latency, and cost — the whole point of running Langfuse.

Strictly READ-ONLY: every tool is a GET against `/api/public/*`. There is no
write path here on purpose; observability tooling must never mutate platform
state.

Credentials are read from `deploy/compose/.env` (LANGFUSE_PUBLIC_KEY /
LANGFUSE_SECRET_KEY / LANGFUSE_HOST) so no secrets live in `.mcp.json`; env
vars override the file when set. Launched via:

    uv run --with fastmcp --with httpx python deploy/mcp/langfuse_mcp.py
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

_ENV_FILE = Path(__file__).resolve().parents[2] / "deploy" / "compose" / ".env"


def _load_env() -> dict[str, str]:
    """Overlay deploy/compose/.env under the real environment (env wins)."""
    values: dict[str, str] = {}
    if _ENV_FILE.is_file():
        for raw in _ENV_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


_env = _load_env()
_PUBLIC_KEY = _env.get("LANGFUSE_PUBLIC_KEY", "")
_SECRET_KEY = _env.get("LANGFUSE_SECRET_KEY", "")
# Inside compose LANGFUSE_HOST is http://langfuse:3000; from the host it's
# localhost. Prefer an explicit override, else map the compose name to localhost.
_HOST = _env.get("LANGFUSE_HOST", "http://localhost:3000").replace(
    "http://langfuse:3000", "http://localhost:3000"
)
_AUTH = "Basic " + base64.b64encode(f"{_PUBLIC_KEY}:{_SECRET_KEY}".encode()).decode()

mcp = FastMCP("langfuse-aleph")


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    resp = httpx.get(
        f"{_HOST}/api/public/{path}",
        params=clean,
        headers={"Authorization": _AUTH},
        timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def list_traces(
    limit: int = 20,
    name: str | None = None,
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
    tags: str | None = None,
) -> dict[str, Any]:
    """List recent traces (newest first). Optionally filter by trace `name`,
    ISO-8601 `from_timestamp`/`to_timestamp`, or a comma-separated `tags`.
    Returns the trace list plus paging metadata (totalItems)."""
    return _get(
        "traces",
        {
            "limit": limit,
            "name": name,
            "fromTimestamp": from_timestamp,
            "toTimestamp": to_timestamp,
            "tags": tags,
        },
    )


@mcp.tool()
def get_trace(trace_id: str) -> dict[str, Any]:
    """Fetch one full trace by id, including its nested observations, latency,
    cost, and any scores — use after list_traces to drill into a specific run."""
    return _get(f"traces/{trace_id}")


@mcp.tool()
def list_observations(
    limit: int = 20,
    level: str | None = None,
    type: str | None = None,
    trace_id: str | None = None,
    name: str | None = None,
    from_start_time: str | None = None,
) -> dict[str, Any]:
    """List observations (spans/generations/events). Filter by `level`
    (DEBUG|DEFAULT|WARNING|ERROR), `type` (SPAN|GENERATION|EVENT), `trace_id`,
    `name`, or ISO-8601 `from_start_time`. Use level=ERROR to surface failures."""
    return _get(
        "observations",
        {
            "limit": limit,
            "level": level,
            "type": type,
            "traceId": trace_id,
            "name": name,
            "fromStartTime": from_start_time,
        },
    )


@mcp.tool()
def recent_errors(limit: int = 20, from_start_time: str | None = None) -> dict[str, Any]:
    """Shortcut for self-diagnosis: the most recent ERROR-level observations
    (failed LLM calls, tool errors, exceptions). Optionally bound with an
    ISO-8601 `from_start_time`."""
    return _get(
        "observations",
        {"limit": limit, "level": "ERROR", "fromStartTime": from_start_time},
    )


@mcp.tool()
def daily_metrics(
    from_timestamp: str | None = None,
    to_timestamp: str | None = None,
) -> dict[str, Any]:
    """Daily rollups of trace counts, token usage, and cost across the project —
    for cost/volume trend and regression checks over a date range."""
    return _get("metrics/daily", {"fromTimestamp": from_timestamp, "toTimestamp": to_timestamp})


if __name__ == "__main__":
    mcp.run()
