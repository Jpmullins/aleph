"""Arq worker for the isolated sandbox (WP-4c).

A dedicated worker that consumes ONLY the `code_runner` queue on a DEDICATED
Redis (`code-runner-redis`) reachable only on the isolated code-runner-net.
That bus carries exactly one payload kind — `run_code_job(code, output_kind,
timeout_s)` — never a token, a privileged job, or cross-project data. The
platform Redis (tokens, privileged queues, LISTEN/NOTIFY streams) is on a
different network this service cannot reach, so even a full sandbox escape
cannot touch it. WP-4c §6.

Env: only ``REDIS_URL`` is read (points at code-runner-redis). No DATABASE_URL /
ALEPH_S3_* / LITELLM_* / ALEPH_AGENT_TOKEN_SECRET — no credentials by construction.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, ClassVar

from arq.connections import RedisSettings

from aleph_code_runner.executor import CODE_RUNNER_QUEUE, RunResult, run_agent_code

_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
# The container runs a read-only rootfs with a writable tmpfs at /scratch, so
# per-run scratch dirs must live there (not the read-only /tmp).
_SCRATCH_ROOT = os.environ.get("CODE_RUNNER_SCRATCH", "/scratch")
# Hard wall-clock ceiling regardless of what a job requests.
_MAX_TIMEOUT_S = 60


async def run_code_job(
    ctx: dict[str, Any],
    code: str,
    output_kind: str,
    timeout_s: int = 30,
) -> RunResult:
    """Execute agent Python off the queue and return result bytes + metadata.

    Runs the blocking subprocess call in a worker thread so the arq event loop
    stays responsive. Returns a plain dict (see `executor.RunResult`); it never
    raises for agent-code faults — those come back as ``{ok: False, error}``.
    """
    bounded = max(1, min(int(timeout_s), _MAX_TIMEOUT_S))
    return await asyncio.to_thread(
        run_agent_code,
        code,
        output_kind,  # type: ignore[arg-type]
        timeout_s=bounded,
        cpu_s=max(1, bounded - 2),
        scratch_root=_SCRATCH_ROOT,
    )


class WorkerSettings:
    """arq entrypoint: `arq aleph_code_runner.arq.WorkerSettings`."""

    functions: ClassVar = [run_code_job]
    queue_name = CODE_RUNNER_QUEUE
    redis_settings = RedisSettings.from_dsn(_REDIS_URL)
    # One code job at a time per worker process keeps the resource story simple
    # (the container's mem_limit bounds a single execution).
    max_jobs = 2
    job_timeout = _MAX_TIMEOUT_S + 15
    keep_result = 300  # let the enqueuer poll `job.result()` after completion
