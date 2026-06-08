"""Project CRUD + membership endpoints.

Every state-changing path writes ledger events in the same transaction.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from arq import create_pool
from fastapi import APIRouter, Body, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.schemas.project import (
    ProjectCreate,
    ProjectMemberOut,
    ProjectOut,
    ProjectUpdate,
)
from aleph_db.models.agent import AgentRun
from aleph_db.models.identity import ProjectMember
from aleph_db.repos import model_profile as profile_repo
from aleph_db.repos import project as project_repo
from aleph_observability.tracing import current_trace_id
from aleph_rks.models import Connector, ConnectorBinding
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1/projects", tags=["projects"])


class _AddMember(BaseModel):
    user_id: UUID
    role: str = Field(pattern=r"^(owner|editor|viewer)$")


class _UpdateMemberRole(BaseModel):
    role: str = Field(pattern=r"^(owner|editor|viewer)$")


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProjectOut)
async def create_project(
    request: Request,
    body: Annotated[ProjectCreate, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> ProjectOut:
    template = await profile_repo.get_template(session, body.model_profile_name)
    if template is None:
        msg = f"unknown model profile template: {body.model_profile_name}"
        raise ValidationFailed(msg)

    project_id = uuid7()
    profile = profile_repo.new_project_profile(
        template=template, project_id=project_id, created_by=principal.user_id
    )
    session.add(profile)
    await session.flush()

    budget = project_repo.new_budget(
        project_id=project_id,
        cap_usd=body.budget_usd,
        created_by=principal.user_id,
    )
    session.add(budget)
    await session.flush()

    project = project_repo.new_project(
        title=body.title,
        description=body.description,
        model_profile_id=profile.id,
        budget_id=budget.id,
        created_by=principal.user_id,
    )
    project.id = project_id
    session.add(project)
    await session.flush()

    member = project_repo.new_member(
        project_id=project_id,
        user_id=principal.user_id,
        role=ProjectRole.OWNER.value,
        created_by=principal.user_id,
    )
    session.add(member)
    await session.flush()

    trace_id = current_trace_id()

    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="project.create",
        target_id=project_id,
        target_kind="project",
        payload={"title": body.title, "description": body.description},
        trace_id=trace_id,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="model_profile.copy_from_template",
        target_id=profile.id,
        target_kind="model_profile",
        payload={"template": template.name, "name": profile.name},
        trace_id=trace_id,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="budget.set",
        target_id=budget.id,
        target_kind="budget",
        payload={"cap_usd": str(body.budget_usd)},
        trace_id=trace_id,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="project_member.add",
        target_id=member.id,
        target_kind="project_member",
        payload={"user_id": str(principal.user_id), "role": ProjectRole.OWNER.value},
        trace_id=trace_id,
    )

    # Seed connector bindings so research works out of the box with no manual
    # setup. Default no-auth connectors (arXiv, OpenAlex, Semantic Scholar,
    # HuggingFace, RSS, upload) are enabled immediately; default auth-required
    # connectors (Tavily, Exa, …) are bound but left disabled until the
    # analyst supplies credentials. One seed event covers the whole set.
    default_connectors = list(
        (await session.execute(select(Connector).where(Connector.enabled_by_default.is_(True))))
        .scalars()
        .all()
    )
    seeded: list[dict[str, object]] = []
    for connector in default_connectors:
        enabled = not connector.requires_auth
        session.add(
            ConnectorBinding(
                id=uuid7(),
                project_id=project_id,
                connector_id=connector.id,
                enabled=enabled,
                config_jsonb={},
                created_by=principal.user_id,
                access_scope="project",
            )
        )
        seeded.append({"kind": connector.kind, "enabled": enabled})
    if seeded:
        await session.flush()
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="connector_bindings.seed",
            target_id=project_id,
            target_kind="project",
            payload={"bindings": seeded},
            trace_id=trace_id,
        )

    # Bootstrap-on-create: kick off the background wiki build from title +
    # description. The AgentRun is created synchronously so the Activity card
    # shows "Bootstrapping project" the instant the project screen loads; the
    # worker job drives the phases. No cost gate — bounded by bootstrap_max_topics.
    settings = request.app.state.settings
    if getattr(settings, "bootstrap_auto_enabled", False):
        from aleph_security.agent_token import mint_agent_token

        boot_run_id = uuid7()
        boot_corr = f"bootstrap-{boot_run_id.hex[:8]}"
        session.add(
            AgentRun(
                id=boot_run_id,
                project_id=project_id,
                agent_kind="bootstrap",
                correlation_id=boot_corr,
                status="pending",
                input_payload={"title": body.title, "description": body.description},
                created_by=principal.user_id,
                access_scope="project",
            )
        )
        await session.flush()
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="bootstrap.dispatch",
            target_id=boot_run_id,
            target_kind="agent_run",
            payload={"title": body.title},
            trace_id=trace_id,
        )
        boot_token = mint_agent_token(
            secret=settings.aleph_agent_token_secret,
            user_id=principal.user_id,
            project_id=project_id,
            agent_run_id=boot_run_id,
            actor_kind="aleph_agent",
            correlation_id=boot_corr,
            ttl_seconds=3600,
        )
        try:
            from arq.connections import RedisSettings

            pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            try:
                await pool.enqueue_job(
                    "bootstrap_project_job", str(project_id), str(boot_run_id), boot_token
                )
            finally:
                await pool.aclose()
        except Exception:
            # Enqueue failure is non-fatal: the run stays 'pending' and can be
            # re-dispatched; project creation must still succeed.
            pass

    return ProjectOut.model_validate(project)


@router.get("", response_model=list[ProjectOut])
async def list_projects(session: SessionDep, principal: PrincipalDep) -> list[ProjectOut]:
    rows = await project_repo.list_member_projects(session, user_id=principal.user_id)
    return [ProjectOut.model_validate(p) for p in rows]


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: ProjectScopeDep, session: SessionDep) -> ProjectOut:
    p = await project_repo.get_project(session, project_id)
    if p is None:  # pragma: no cover — scope dep guards above
        msg = f"project not found: {project_id}"
        raise NotFound(msg)
    return ProjectOut.model_validate(p)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: ProjectScopeDep,
    body: Annotated[ProjectUpdate, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> ProjectOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    p = await project_repo.get_project(session, project_id)
    if p is None:  # pragma: no cover
        msg = f"project not found: {project_id}"
        raise NotFound(msg)
    changed: dict[str, str] = {}
    if body.title is not None and body.title != p.title:
        p.title = body.title
        changed["title"] = body.title
    if body.description is not None and body.description != p.description:
        p.description = body.description
        changed["description"] = body.description
    if body.status is not None and body.status != p.status:
        # Only owner can archive/delete.
        if body.status in ("archived", "deleted"):
            require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
        p.status = body.status
        changed["status"] = body.status
    if changed:
        await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="project.update",
            target_id=project_id,
            target_kind="project",
            payload=changed,
            trace_id=current_trace_id(),
        )
        # Flush bumped `updated_at` server-side; refresh so Pydantic
        # serialization doesn't trigger a lazy reload outside the
        # greenlet context.
        await session.refresh(p)
    return ProjectOut.model_validate(p)


@router.post(
    "/{project_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=ProjectMemberOut,
)
async def add_member(
    project_id: ProjectScopeDep,
    body: Annotated[_AddMember, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> ProjectMemberOut:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    existing = await project_repo.get_member(session, project_id=project_id, user_id=body.user_id)
    if existing is not None:
        msg = "member already exists"
        raise ValidationFailed(msg)
    member = project_repo.new_member(
        project_id=project_id,
        user_id=body.user_id,
        role=body.role,
        created_by=principal.user_id,
    )
    session.add(member)
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="project_member.add",
        target_id=member.id,
        target_kind="project_member",
        payload={"user_id": str(body.user_id), "role": body.role},
        trace_id=current_trace_id(),
    )
    return ProjectMemberOut.model_validate(member)


@router.delete("/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    project_id: ProjectScopeDep,
    user_id: UUID,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> None:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    member = await project_repo.get_member(session, project_id=project_id, user_id=user_id)
    if member is None:
        msg = "member not found"
        raise NotFound(msg)
    if member.user_id == principal.user_id:
        msg = "cannot remove yourself; transfer ownership first"
        raise ValidationFailed(msg)
    member_id = member.id
    await session.delete(member)
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="project_member.remove",
        target_id=member_id,
        target_kind="project_member",
        payload={"user_id": str(user_id)},
        trace_id=current_trace_id(),
    )


@router.patch("/{project_id}/members/{user_id}", response_model=ProjectMemberOut)
async def change_member_role(
    project_id: ProjectScopeDep,
    user_id: UUID,
    body: Annotated[_UpdateMemberRole, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> ProjectMemberOut:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    member = await project_repo.get_member(session, project_id=project_id, user_id=user_id)
    if member is None:
        msg = "member not found"
        raise NotFound(msg)
    if member.role == body.role:
        return ProjectMemberOut.model_validate(member)
    prior = member.role
    member.role = body.role
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="project_member.role_change",
        target_id=member.id,
        target_kind="project_member",
        payload={"user_id": str(user_id), "from": prior, "to": body.role},
        trace_id=current_trace_id(),
    )
    return ProjectMemberOut.model_validate(member)


@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
async def list_members(project_id: ProjectScopeDep, session: SessionDep) -> list[ProjectMemberOut]:
    stmt = (
        select(ProjectMember)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.created_at)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [ProjectMemberOut.model_validate(m) for m in rows]


class AgentRunOut(BaseModel):
    id: UUID
    agent_kind: str
    status: str
    started_at: str | None
    completed_at: str | None
    correlation_id: str
    error_text: str | None
    created_at: str


@router.get("/{project_id}/agent-runs", response_model=list[AgentRunOut])
async def list_agent_runs(
    project_id: ProjectScopeDep,
    session: SessionDep,
    limit: int = 20,
) -> list[AgentRunOut]:
    """Recent agent runs for the Activity card. Newest first."""
    stmt = (
        select(AgentRun)
        .where(AgentRun.project_id == project_id)
        .order_by(AgentRun.created_at.desc())
        .limit(max(1, min(limit, 100)))
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        AgentRunOut(
            id=r.id,
            agent_kind=r.agent_kind,
            status=r.status,
            started_at=r.started_at.isoformat() if r.started_at else None,
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
            correlation_id=r.correlation_id,
            error_text=r.error_text,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]
