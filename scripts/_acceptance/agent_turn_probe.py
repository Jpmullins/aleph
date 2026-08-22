"""Drive a real chat turn and print what it cost, in requests and in seconds.

Two numbers come out of one probe, because they are two views of one event.

**The per-turn upstream request count** (`WS-E1c` criterion 5, and the whole
argument in `WS-E5`). The owner's report was "weirdly rate limited". Nobody could
say whether one turn issued three upstream requests or thirty, so the discussion
had no number in it. The plan proposed a counting wrapper around the gateway
client. That would work, and it would measure a wrapper. The `model_calls` ledger
is strictly better evidence: it is the production write path, it is already
required by a standing rule, and — since `agent_run_id` attribution landed — a
turn's rows are exactly the requests that turn issued. If this count and reality
ever disagree, the *ledger* is what is wrong, and that is worth finding out.

**Time to first token** (number 7 in Part 1). Measured at the SSE boundary the
browser actually reads: from the POST to the first `TEXT_MESSAGE_CONTENT` frame.
Not from a log line, not from the model client — from the same stream a person
waits on. `RUN_ERROR` counts as a failed sample and is reported, never averaged
in; a fast failure is not a fast answer, and letting it into a latency average is
how a p95 improves while the product gets worse.

Both are printed with the sample count and the spread, because a single sample of
a latency number is an anecdote. No ceiling is asserted here: Part 1 says number 7
has no stated ceiling yet, and inventing one inside the instrument is how an
acceptance gate starts certifying its own opinion.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx

#: The frame the browser paints first. `TEXT_MESSAGE_START` arrives earlier but
#: carries no content, so timing it would report a number no user experiences.
FIRST_TOKEN_EVENT = "TEXT_MESSAGE_CONTENT"
RUN_ERROR_EVENT = "RUN_ERROR"
RUN_FINISHED_EVENT = "RUN_FINISHED"

#: Set by `aleph_api.agui_endpoint`. Without it there is no run to join the
#: ledger on, and the request count cannot be attributed at all.
AGENT_RUN_HEADER = "x-aleph-agent-run-id"

DEFAULT_PROMPT = "In one sentence, what is in this project's knowledge base? Do not use any tools."


@dataclass
class Turn:
    """One drive of the endpoint, and everything it is possible to learn from it."""

    ok: bool
    first_token_s: float | None
    total_s: float
    agent_run_id: uuid.UUID | None
    error: str | None = None
    upstream_requests: int | None = None
    models: list[str] = field(default_factory=list)


async def _drive(client: httpx.AsyncClient, url: str, project_id: str, prompt: str) -> Turn:
    thread_id = f"proj:{project_id}:probe-{uuid.uuid4().hex[:8]}"
    body = {
        "threadId": thread_id,
        "runId": uuid.uuid4().hex,
        "state": {},
        "messages": [{"id": uuid.uuid4().hex, "role": "user", "content": prompt}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    started = time.monotonic()
    first_token: float | None = None
    error: str | None = None
    ok = False
    run_id: uuid.UUID | None = None

    async with client.stream("POST", url, json=body) as response:
        raw = response.headers.get(AGENT_RUN_HEADER)
        if raw:
            try:
                run_id = uuid.UUID(raw)
            except ValueError:
                run_id = None
        if response.status_code != 200:
            await response.aread()
            return Turn(
                ok=False,
                first_token_s=None,
                total_s=time.monotonic() - started,
                agent_run_id=run_id,
                error=f"HTTP {response.status_code}",
            )
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == FIRST_TOKEN_EVENT and first_token is None:
                first_token = time.monotonic() - started
            elif kind == RUN_ERROR_EVENT:
                error = str(event.get("message", ""))[:200]
            elif kind == RUN_FINISHED_EVENT:
                ok = error is None

    return Turn(
        ok=ok,
        first_token_s=first_token,
        total_s=time.monotonic() - started,
        agent_run_id=run_id,
        error=error,
    )


async def _attribute(turns: list[Turn], database_url: str) -> None:
    """Fill in each turn's upstream request count from the ledger.

    A turn whose run id never made it back is left as `None` rather than as 0.
    Zero is a claim that the turn issued no upstream requests; `None` is the
    truth, which is that this probe cannot tell.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            for turn in turns:
                if turn.agent_run_id is None:
                    continue
                rows = (
                    await conn.execute(
                        text(
                            "select model, count(*) as n from model_calls "
                            "where agent_run_id = :rid group by model order by n desc"
                        ),
                        {"rid": turn.agent_run_id},
                    )
                ).all()
                turn.upstream_requests = sum(int(r.n) for r in rows)
                turn.models = [str(r.model) for r in rows]
    finally:
        await engine.dispose()


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile.

    `statistics.quantiles` interpolates, which invents a value between two
    samples — misleading at the sample sizes this probe realistically runs at.
    """
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-pct * len(ordered) // 100))))
    return ordered[rank - 1]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=os.environ.get("ALEPH_API_URL", "http://localhost:8000"))
    parser.add_argument("--project", default=os.environ.get("ALEPH_PROBE_PROJECT_ID"))
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    project_id = args.project
    if project_id is None and database_url:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(database_url)
        async with engine.connect() as conn:
            row = (
                await conn.execute(text("select id from projects order by created_at limit 1"))
            ).first()
        await engine.dispose()
        project_id = str(row.id) if row else None

    if project_id is None:
        print("SKIP: no project to drive a turn against (set ALEPH_PROBE_PROJECT_ID)")
        return 0

    url = f"{args.api.rstrip('/')}/copilotkit/agent/assistant"
    turns: list[Turn] = []
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        try:
            await client.get(f"{args.api.rstrip('/')}/healthz", timeout=5.0)
        except httpx.HTTPError as exc:
            print(f"SKIP: the API is not reachable at {args.api} ({type(exc).__name__})")
            return 0
        for _ in range(args.samples):
            turns.append(await _drive(client, url, project_id, args.prompt))

    if database_url:
        await _attribute(turns, database_url)

    print(f"agent turn probe — {len(turns)} sample(s) against {url}")
    print(f"  project {project_id}")
    for i, turn in enumerate(turns, 1):
        ttft = f"{turn.first_token_s:.2f}s" if turn.first_token_s is not None else "—"
        reqs = "—" if turn.upstream_requests is None else str(turn.upstream_requests)
        status = "ok" if turn.ok else f"FAILED ({turn.error or 'no RUN_FINISHED'})"
        print(
            f"  {i}. {status:<28} first-token {ttft:>7}  total {turn.total_s:6.2f}s  "
            f"upstream requests {reqs}"
        )
        if turn.models:
            print(f"       models: {', '.join(turn.models)}")

    good = [t for t in turns if t.ok and t.first_token_s is not None]
    failed = len(turns) - len(good)

    print()
    if good:
        latencies = [t.first_token_s for t in good if t.first_token_s is not None]
        print(
            f"  first-token  p50 {_percentile(latencies, 50):.2f}s   "
            f"p95 {_percentile(latencies, 95):.2f}s   "
            f"(n={len(latencies)}, failures excluded: {failed})"
        )
        if len(latencies) < 20:
            # Said out loud, because a p95 over three samples is the largest
            # sample, and printing it without this line invites it to be quoted
            # as though it were a measurement.
            print(
                f"       NOTE: p95 over {len(latencies)} samples is the slowest sample, "
                "not a percentile. Raise --samples for a number worth quoting."
            )
    else:
        print(f"  first-token  no successful turn to measure ({failed} failed)")

    counted = [t.upstream_requests for t in turns if t.upstream_requests is not None]
    if counted:
        spread = "identical" if len(set(counted)) == 1 else f"VARIES {sorted(counted)}"
        print(
            f"  upstream chat-completion requests per turn: "
            f"{min(counted)} to {max(counted)} across {len(counted)} turn(s) — {spread}"
        )
        if statistics.fmean(counted) == 0:
            # Attributed rows exist and total zero: either the turn genuinely
            # called no model, or attribution regressed. Both are worth a line.
            print(
                "       WARNING: zero model calls attributed to any turn — either the "
                "agent answered without a model, or agent_run_id attribution is broken."
            )
    else:
        print(
            "  upstream chat-completion requests per turn: not attributable "
            "(no run id came back, or DATABASE_URL is unset)"
        )

    if failed == len(turns):
        print("\nFAIL: every turn failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
