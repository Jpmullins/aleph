"""Helpers for emitting AgentEvent rows per workflow phase.

Per spec §1.8: each LangGraph node in a long-running agent workflow should
emit `phase_started` on entry and `phase_completed` / `phase_failed` on
exit. The activity card on the frontend subscribes to a per-project SSE
stream of these events to render sub-phase progress (which the
top-level `AgentRun.status` cannot express).

Events are written through a short-lived session of their own so they
commit immediately and become visible to the SSE poller, independent of
the calling node's longer-running data session.
"""

from __future__ import annotations

import functools
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Callable

from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID

    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession


__all__ = [
    "emit_phase_started",
    "emit_phase_completed",
    "emit_phase_failed",
    "phase",
    "with_phase",
]


async def emit_phase_started(
    session: "AsyncSession",
    *,
    agent_run_id: "UUID",
    phase_name: str,
    payload: dict[str, Any] | None = None,
) -> AgentEvent:
    event = AgentEvent(
        id=uuid7(),
        agent_run_id=agent_run_id,
        event_kind="phase_started",
        payload_jsonb={"phase": phase_name, **(payload or {})},
    )
    session.add(event)
    await session.flush()
    return event


async def emit_phase_completed(
    session: "AsyncSession",
    *,
    agent_run_id: "UUID",
    phase_name: str,
    duration_ms: int,
    payload: dict[str, Any] | None = None,
) -> AgentEvent:
    event = AgentEvent(
        id=uuid7(),
        agent_run_id=agent_run_id,
        event_kind="phase_completed",
        payload_jsonb={
            "phase": phase_name,
            "duration_ms": duration_ms,
            **(payload or {}),
        },
    )
    session.add(event)
    await session.flush()
    return event


async def emit_phase_failed(
    session: "AsyncSession",
    *,
    agent_run_id: "UUID",
    phase_name: str,
    error_text: str,
) -> AgentEvent:
    event = AgentEvent(
        id=uuid7(),
        agent_run_id=agent_run_id,
        event_kind="phase_failed",
        payload_jsonb={"phase": phase_name, "error": error_text[:1024]},
    )
    session.add(event)
    await session.flush()
    return event


@asynccontextmanager
async def phase(
    maker: "async_sessionmaker[AsyncSession]",
    *,
    agent_run_id: "UUID | None",
    phase_name: str,
    started_payload: dict[str, Any] | None = None,
) -> "AsyncIterator[dict[str, Any]]":
    """Wrap a workflow node body in phase_started / phase_completed / phase_failed events.

    Pattern:

        async def _node_concept_extraction(state: WikiIngestState) -> dict:
            async with phase(
                ctx.session_maker,
                agent_run_id=state["agent_run_id"],
                phase_name="concept_extraction",
            ) as p:
                ...
                p["concept_count"] = len(concepts)  # extra completed-payload data
                return {"concepts": concepts}

    The wrapper opens its own short-lived session so events commit
    immediately (and are visible to the SSE poller) without coupling to
    the caller's data session.

    If `agent_run_id` is None (e.g. invoked outside an agent run for a
    test), the wrapper is a no-op pass-through.
    """
    if agent_run_id is None:
        yield {}
        return

    completed_payload: dict[str, Any] = started_payload.copy() if started_payload else {}
    start = time.monotonic()

    async with maker() as session:
        await emit_phase_started(
            session,
            agent_run_id=agent_run_id,
            phase_name=phase_name,
            payload=started_payload,
        )
        await session.commit()

    try:
        yield completed_payload
    except Exception as exc:
        async with maker() as session:
            await emit_phase_failed(
                session,
                agent_run_id=agent_run_id,
                phase_name=phase_name,
                error_text=str(exc),
            )
            await session.commit()
        raise

    duration_ms = int((time.monotonic() - start) * 1000)
    async with maker() as session:
        await emit_phase_completed(
            session,
            agent_run_id=agent_run_id,
            phase_name=phase_name,
            duration_ms=duration_ms,
            payload=completed_payload or None,
        )
        await session.commit()


def with_phase(phase_name: str, *, ctx_getter: "Callable[[], Any]") -> Any:
    """Decorator that wraps a LangGraph node coroutine in phase events.

    Usage:

        @with_phase("concept_extraction", ctx_getter=_ctx)
        async def _node_concept_extraction(state):
            ...

    `ctx_getter` is a callable returning a per-invocation context object
    that exposes `.session_maker` (the workflow's WorkflowContext-style
    dataclass).  The decorator reads `state["agent_run_id"]` and emits
    `phase_started` / `phase_completed` / `phase_failed` against that
    AgentRun. If `agent_run_id` is missing (test invocation, etc.), the
    decorator becomes a no-op pass-through.
    """

    def decorator(fn: Any) -> Any:
        @functools.wraps(fn)
        async def wrapper(state: dict[str, Any]) -> Any:
            maker = ctx_getter().session_maker
            agent_run_id = state.get("agent_run_id") if isinstance(state, dict) else None
            async with phase(
                maker, agent_run_id=agent_run_id, phase_name=phase_name
            ):
                return await fn(state)

        return wrapper

    return decorator
