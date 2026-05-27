"""Agent-token mint endpoint.

Only owners of a project can mint a token for an AgentRun in that project.
The token signs over `agent_run_id`, `project_id`, `user_id`, `actor_kind`,
and `correlation_id` with a configurable TTL (default 1h, max 1h).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel

from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_db.models.agent import AgentRun
from aleph_db.repos.project import get_member, get_project
from aleph_observability.tracing import current_trace_id
from aleph_security.agent_token import AgentActorKind, mint_agent_token
from aleph_security.roles import ProjectRole, require_at_least

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep

router = APIRouter(prefix="/v1", tags=["agent-tokens"])


class MintRequest(BaseModel):
    project_id: str
    agent_kind: str
    actor_kind: AgentActorKind = "aleph_agent"
    correlation_id: str | None = None
    input_payload: dict = {}
    ttl_seconds: int = 3600


class MintResponse(BaseModel):
    agent_run_id: str
    correlation_id: str
    token: str
    expires_in: int


@router.post("/agent-tokens", response_model=MintResponse)
async def mint_token(
    request: Request,
    body: Annotated[MintRequest, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> MintResponse:
    try:
        project_id = UUID(body.project_id)
    except ValueError as exc:
        msg = "invalid project_id"
        raise ValidationFailed(msg) from exc

    project = await get_project(session, project_id)
    if project is None:
        msg = f"project not found: {project_id}"
        raise NotFound(msg)
    member = await get_member(
        session, project_id=project_id, user_id=principal.user_id
    )
    if member is None:
        # Same as the project-scope dep: 404 on non-membership.
        msg = f"project not found: {project_id}"
        raise NotFound(msg)
    principal.cache_role(project_id, member.role)
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)

    agent_run_id = uuid7()
    correlation_id = body.correlation_id or agent_run_id.hex[:16]

    run = AgentRun(
        id=agent_run_id,
        project_id=project_id,
        agent_kind=body.agent_kind,
        correlation_id=correlation_id,
        status="pending",
        input_payload=body.input_payload,
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(run)
    await session.flush()

    secret = request.app.state.settings.aleph_agent_token_secret
    token = mint_agent_token(
        secret=secret,
        user_id=principal.user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind=body.actor_kind,
        correlation_id=correlation_id,
        ttl_seconds=body.ttl_seconds,
    )

    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="agent_run.create",
        target_id=agent_run_id,
        target_kind="agent_run",
        payload={
            "agent_kind": body.agent_kind,
            "actor_kind": body.actor_kind,
            "correlation_id": correlation_id,
        },
        trace_id=current_trace_id(),
    )

    return MintResponse(
        agent_run_id=str(agent_run_id),
        correlation_id=correlation_id,
        token=token,
        expires_in=body.ttl_seconds,
    )
