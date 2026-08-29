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

from functools import lru_cache
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from aleph_db.models.agent import AgentRun, AgentThread
from aleph_db.repos.agent_runs import SYSTEM_ACTOR
from aleph_db.repos.background_tasks import STATUS_FAILED, STATUS_SUCCEEDED
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal
from aleph_security.request_context import bind_principal, reset_principal
from aleph_workers.gateway import gateways
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
        output = await _run_subagent(
            ctx, name=name, messages=messages, principal=principal, run_id=run_id
        )
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
    run_id: UUID,
) -> list[dict[str, Any]]:
    """Build the named subagent and run it to completion.

    Imported inside the function because `aleph_api` is an APP and workers do
    not import apps at module scope — the same rule `background_kinds` follows.
    The delegatable set is defined once, in `aleph_api.subagents.DELEGATABLE`, so
    the route's validation and this construction cannot disagree about what
    exists.
    """
    from deepagents import create_deep_agent

    from aleph_api.chat_runs import RUN_ID_KEY
    from aleph_api.copilot_agent import (
        bind_runtime,
        bindings_for_project,
        endpoint_for_project,
        use_agent_bindings,
        use_agent_endpoint,
    )
    from aleph_api.subagents import build_subagent

    # The API's Settings, not the worker's.
    #
    # `build_subagent` and `_gateway_chat_model` are API code and read API
    # settings — `aleph_agent_request_timeout_s` among them, which is what the
    # third end-to-end attempt failed on. `WorkerSettings` is a different class
    # with a different field set; passing it produces an AttributeError deep
    # inside a subagent builder, which is a confusing place to learn that two
    # settings objects are not interchangeable.
    #
    # Both are pydantic-settings over the SAME environment (compose shares one
    # env block between api and workers), so constructing the API's here reads
    # the same values the API would. Cached per process because construction
    # validates every field.
    settings = _api_settings()

    # `bindings_for_project` reads `copilot_agent._runtime["session_maker"]`, a
    # MODULE-LEVEL dict the API populates in its lifespan. This is a different
    # process, so it is empty here and the lookup silently returns the boot
    # bindings — which are also empty, so every capability resolves to nothing.
    #
    # The first end-to-end delegation failed with `NoModelBound: 'synthesis'` on
    # a project whose profile binds synthesis, and the second failed the same way
    # after I established the ContextVars but not this. Both are the same shape:
    # process-local state the worker has to establish for itself, and neither is
    # visible from reading the call site.
    maker = ctx["session_maker"]
    #
    # The client is the PROJECT's, resolved through `WorkerGateways`, not one
    # built from `LITELLM_BASE_URL`. WS-MEP-4 removed the boot-time
    # `ctx["litellm_client"]` precisely because one client for every project
    # meant a project's own `gateway_endpoints` row reached the settings screen
    # and none of its background traffic. A delegation is background traffic.
    project_id = principal.project_id
    litellm = await gateways(ctx).litellm(project_id) if project_id else None
    bind_runtime(
        session_maker=maker,
        settings=settings,
        litellm=litellm,
    )

    # INSIDE the project's binding and endpoint context, and this is not
    # optional. `_gateway_chat_model` resolves a capability to a model from
    # `_active_bindings()` / `_active_endpoint()`, which are ContextVars the API
    # enters before it builds a graph. Building a subagent outside them resolves
    # against nothing, and the first end-to-end delegation failed with exactly
    # that: `NoModelBound: no model is bound for capability 'synthesis'` on a
    # project whose profile binds it. The worker is a different process; it has
    # to establish the same context the API does.
    bindings = dict(await bindings_for_project(project_id) or {})
    endpoint = await endpoint_for_project(project_id, settings=settings)

    # The subagent's tools resolve their project from the graph config and their
    # caller from the principal ContextVar — both established by the HTTP request
    # in the API. There is no request here, so both are established explicitly.
    #
    # Without the principal, `_authorized` fails closed and every tool answers
    # "no project scope on this run"; the delegation then SUCCEEDS while doing
    # nothing, which is the failure shape that looks like it worked. The first
    # green end-to-end run did exactly that and the transcript is what showed it.
    token = bind_principal(principal)
    try:
        with use_agent_bindings(bindings), use_agent_endpoint(endpoint):
            spec = build_subagent(name, settings=settings)
            agent = create_deep_agent(
                model=spec["model"],
                system_prompt=spec.get("system_prompt", ""),
                tools=list(spec.get("tools") or []),
                middleware=list(spec.get("middleware") or []),
            )
            result = await agent.ainvoke(
                {"messages": messages},
                # `_project_id_from_config` reads `projectId` from `configurable`;
                # the API normally supplies it through a project-prefixed thread
                # id, which a delegated run has no equivalent of.
                #
                # `RUN_ID_KEY` is the OTHER half, and leaving it out is not
                # cosmetic. Tools that make model calls of their own read the
                # turn they belong to out of `configurable` via
                # `run_id_from_config`; `read_wiki_deeply` drives the retrieval
                # router, which makes three more (`corpus_search.query_embed`,
                # `page_selection`, `compose`). Omitting the key writes all
                # three with `agent_run_id=NULL` while they still declare
                # `purpose="assistant.*"` — which is precisely what status.sh
                # number 5 counts, and precisely what it counted here: three
                # orphans from ONE delegated wiki read, on a run that had a real
                # `AgentRun` row the whole time. The run exists; this is the
                # channel that says so.
                config={
                    "configurable": {
                        "projectId": str(project_id),
                        RUN_ID_KEY: str(run_id),
                    }
                },
            )
    finally:
        reset_principal(token)

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


@lru_cache(maxsize=1)
def _api_settings() -> Any:
    """The API's Settings, constructed from this process's environment.

    Raises if the worker's environment is missing something the API requires —
    which converges the delegation to a stated failure rather than a subagent
    that half-builds and dies somewhere less legible.
    """
    from aleph_api.settings import Settings

    return Settings()  # pyright: ignore[reportCallIssue] - fields come from env
