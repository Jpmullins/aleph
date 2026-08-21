"""Sandbox viz pipeline API (WP-4c).

`POST /viz/render-code` is the agent's hands into the sandbox: it creates an
`AgentRun`, mints a short-lived agent token, and enqueues
`render_code_artifact_job` in `aleph-workers`. The worker fans the code out to
the isolated, credential-less `aleph-code-runner` (over a dedicated Redis
queue), awaits the bytes, persists them as a versioned artifact, and pins a card
to Briefs. NO agent code executes in this API process — this route only
dispatches.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Request, status
from pydantic import BaseModel, Field

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentRun
from aleph_observability.tracing import current_trace_id
from aleph_security.agent_token import mint_agent_token
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1/projects", tags=["viz"])


class RenderCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=200_000)
    output_kind: Literal["png", "svg", "vega", "html"]
    title: str = Field(default="Sandbox chart", max_length=512)
    pin: bool = True
    timeout_s: int = Field(default=30, ge=1, le=60)


class RenderCodeOut(BaseModel):
    dispatched: bool
    agent_run_id: UUID


@router.post(
    "/{project_id}/viz/render-code",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RenderCodeOut,
)
async def post_render_code(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[RenderCodeIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> RenderCodeOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)

    agent_run_id = uuid7()
    correlation_id = f"vizcode-{agent_run_id.hex}"
    run = AgentRun(
        id=agent_run_id,
        project_id=project_id,
        agent_kind="viz_code",
        correlation_id=correlation_id,
        status="pending",
        input_payload={"output_kind": body.output_kind, "title": body.title},
        created_by=principal.user_id,
    )
    session.add(run)
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="viz.render_code.dispatch",
        target_id=agent_run_id,
        target_kind="agent_run",
        payload={"output_kind": body.output_kind, "title": body.title},
        trace_id=current_trace_id(),
    )

    token = mint_agent_token(
        secret=request.app.state.settings.aleph_agent_token_secret,
        user_id=principal.user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id=correlation_id,
        ttl_seconds=3600,
    )

    # Durable before dispatched: the worker resolves these ids the moment it
    # dequeues, and an arq worker beats an uncommitted transaction. A job that
    # loses that race fails on a row that does not exist yet.
    await session.commit()

    dispatched = False
    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(request.app.state.settings.redis_url))
        try:
            await pool.enqueue_job(
                "render_code_artifact_job",
                str(project_id),
                body.code,
                body.output_kind,
                body.title,
                body.pin,
                token,
                body.timeout_s,
            )
            dispatched = True
        finally:
            await pool.aclose()
    except Exception as exc:
        run.status = "failed"
        run.error_text = f"failed to enqueue render_code_artifact_job: {exc}"[:2048]

    return RenderCodeOut(dispatched=dispatched, agent_run_id=agent_run_id)
