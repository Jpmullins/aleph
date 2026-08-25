"""``delegated_subagent_job`` — runs one delegated subagent to a terminal status.

`docs/decisions.md` D17, phase A. The API's Agent Protocol routes hand
`deepagents`' `AsyncSubAgentMiddleware` a ticket and return; this is what makes
that ticket true.

**Why this is not `background_task_job`.** That supervisor already owns the four
things a long job needs — claiming, heartbeating, cancellation, convergence —
and reusing it was the first instinct. It resolves its handler from
`BACKGROUND_TASK_HANDLERS[run.agent_kind]`, and `test_every_kind_has_a_handler`
asserts that registry and `BACKGROUND_TASK_KINDS` agree in BOTH directions. A
delegation's kind carries the subagent name (`delegation:retriever`), so making
it fit would mean adding five entries to the list the ticket route offers the
agent as "kinds of work you may ask for" — conflating two features to reuse a
dispatcher. The convergence helper is shared instead, which is the part that was
actually worth not rewriting.

**Every exit path reaches a terminal status.** A run left at `running` is what
the stale-run reaper exists to clean up, and needing the reaper is a bug rather
than a design. That includes the token-rejected path: `mint_agent_token` refuses
a TTL over an hour, so a delegation that waits in the queue longer than that
arrives with a token this worker cannot verify, and raising before the run is
claimed would leave it `pending` forever while arq retried against the same dead
token.

**The result is written to the THREAD, not only the run.** `_build_check_result`
reads a finished task's output from `thread["values"]["messages"]` — the run's
`result_payload` is Aleph's record, and the thread's `values` is the contract.
Writing one and not the other produces a task the supervisor sees succeed and
can never read.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from aleph_db.models.agent import AgentRun, AgentThread
from aleph_db.repos.agent_runs import SYSTEM_ACTOR
from aleph_db.repos.background_tasks import STATUS_FAILED, STATUS_SUCCEEDED
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal
from aleph_workers.jobs.background import _converge

_log = structlog.get_logger(__name__)

#: What `agent_kind` looks like for a delegation. The suffix is the subagent.
KIND_PREFIX = "delegation:"


def subagent_of(agent_kind: str) -> str | None:
    """The subagent a delegation kind names, or None if it is not one."""
    if not agent_kind.startswith(KIND_PREFIX):
        return None
    return agent_kind[len(KIND_PREFIX) :] or None


async def _write_thread_values(maker: Any, *, run_id: UUID, messages: list[dict[str, Any]]) -> None:
    """Put the output where the middleware reads it from.

    Its own session and its own transaction, for the same reason `_converge` has
    one: this runs on the failure path too, where the caller's session may be in
    an unusable state after an exception.
    """
    async with maker() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        ).scalar_one_or_none()
        if run is None or run.agent_thread_id is None:
            return
        thread = (
            await session.execute(select(AgentThread).where(AgentThread.id == run.agent_thread_id))
        ).scalar_one_or_none()
        if thread is None:
            return
        values = dict(thread.values_jsonb or {})
        values["messages"] = messages
        thread.values_jsonb = values
        await session.commit()


async def delegated_subagent_job(
    ctx: dict[str, Any], agent_run_id_str: str, agent_token: str
) -> dict[str, Any]:
    """Run the subagent a delegation names, and converge the run either way."""
    secret: str = ctx["agent_token_secret"]
    maker = ctx["session_maker"]
    run_id = UUID(agent_run_id_str)

    try:
        claims = verify_agent_token(agent_token, secret=secret)
    except Exception as exc:
        _log.warning(
            "delegation.token_rejected",
            agent_run_id=agent_run_id_str,
            error=f"{type(exc).__name__}: {exc}",
        )
        await _converge(
            maker,
            run_id=run_id,
            status=STATUS_FAILED,
            actor_id=SYSTEM_ACTOR,
            actor_kind="system",
            error_text=(
                "delegation token could not be verified (it expires one hour "
                f"after dispatch): {type(exc).__name__}: {exc}"
            ),
        )
        return {"ok": False, "error": "delegation token rejected"}

    principal = Principal(
        user_id=claims.user_id,
        subject="agent",
        email="",
        actor_kind=claims.actor_kind,
        agent_run_id=claims.agent_run_id,
        correlation_id=claims.correlation_id,
        project_id=claims.project_id,
    )

    async with maker() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            _log.warning("delegation.run_missing", agent_run_id=agent_run_id_str)
            return {"ok": False, "error": "run not found"}
        if run.status in ("succeeded", "failed", "cancelled"):
            # A re-delivered job. Refusing is the point: flipping a terminal run
            # back to `running` would make a cancelled delegation resume, and
            # `check_async_task` has already cached the terminal status.
            _log.info(
                "delegation.already_terminal", agent_run_id=agent_run_id_str, status=run.status
            )
            return {"ok": True, "skipped": run.status}
        name = subagent_of(run.agent_kind)
        messages = list((run.input_payload or {}).get("messages") or [])
        run.status = "running"
        await session.commit()

    if name is None:
        await _converge(
            maker,
            run_id=run_id,
            status=STATUS_FAILED,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            error_text=(
                f"run {run_id} is not a delegation: agent_kind carries no {KIND_PREFIX!r} prefix"
            ),
        )
        return {"ok": False, "error": "not a delegation"}

    try:
        output = await _run_subagent(ctx, name=name, messages=messages, principal=principal)
    except Exception as exc:
        _log.exception("delegation.failed", agent_run_id=agent_run_id_str, subagent=name)
        await _write_thread_values(
            maker,
            run_id=run_id,
            messages=[{"role": "assistant", "content": f"{type(exc).__name__}: {exc}"}],
        )
        await _converge(
            maker,
            run_id=run_id,
            status=STATUS_FAILED,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            error_text=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    await _write_thread_values(maker, run_id=run_id, messages=output)
    await _converge(
        maker,
        run_id=run_id,
        status=STATUS_SUCCEEDED,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        result={"messages": output},
    )
    return {"ok": True, "subagent": name, "messages": len(output)}


async def _run_subagent(
    ctx: dict[str, Any],
    *,
    name: str,
    messages: list[dict[str, Any]],
    principal: Principal,
) -> list[dict[str, Any]]:
    """Build the named subagent and run it to completion.

    Imported inside the function because `aleph_api` is an APP and workers do
    not import apps at module scope — the same rule `background_kinds` follows.
    The delegatable set is defined once, in `aleph_api.subagents.DELEGATABLE`, so
    the route's validation and this construction cannot disagree about what
    exists.
    """
    from deepagents import create_deep_agent

    from aleph_api.subagents import build_subagent

    settings = ctx["settings"]
    spec = build_subagent(name, settings=settings)
    agent = create_deep_agent(
        model=spec["model"],
        system_prompt=spec.get("system_prompt", ""),
        tools=list(spec.get("tools") or []),
        middleware=list(spec.get("middleware") or []),
    )
    result = await agent.ainvoke({"messages": messages})
    out: list[dict[str, Any]] = []
    for m in result.get("messages", []):
        role = getattr(m, "type", None) or getattr(m, "role", "assistant")
        content = getattr(m, "content", None)
        if content:
            out.append(
                {
                    "role": "assistant" if role in ("ai", "assistant") else str(role),
                    "content": content if isinstance(content, str) else str(content),
                }
            )
    return out
