"""Connectors API: list + per-project bindings."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_core.errors import NotFound
from aleph_core.ids import uuid7
from aleph_observability.tracing import current_trace_id
from aleph_rks.models import Connector, ConnectorBinding
from aleph_security.roles import ProjectRole, require_at_least

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep

router = APIRouter(prefix="/v1", tags=["connectors"])


class ConnectorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    name: str
    output_kind: str
    requires_auth: bool
    enabled_by_default: bool


class ConnectorBindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    connector_id: UUID
    enabled: bool
    config_jsonb: dict
    created_at: datetime


class ConnectorBindingIn(BaseModel):
    connector_id: UUID
    enabled: bool = True
    config_jsonb: dict = Field(default_factory=dict)


@router.get("/connectors", response_model=list[ConnectorOut])
async def list_connectors(session: SessionDep) -> list[ConnectorOut]:
    rows = list(
        (await session.execute(select(Connector).order_by(Connector.kind))).scalars().all()
    )
    return [ConnectorOut.model_validate(r) for r in rows]


@router.get(
    "/projects/{project_id}/connectors/bindings",
    response_model=list[ConnectorBindingOut],
)
async def list_bindings(
    project_id: ProjectScopeDep, session: SessionDep
) -> list[ConnectorBindingOut]:
    stmt = (
        select(ConnectorBinding)
        .where(ConnectorBinding.project_id == project_id)
        .order_by(ConnectorBinding.created_at)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return [ConnectorBindingOut.model_validate(r) for r in rows]


@router.post(
    "/projects/{project_id}/connectors/bindings",
    status_code=status.HTTP_201_CREATED,
    response_model=ConnectorBindingOut,
)
async def set_binding(
    project_id: ProjectScopeDep,
    body: Annotated[ConnectorBindingIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> ConnectorBindingOut:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    connector = (
        await session.execute(
            select(Connector).where(Connector.id == body.connector_id)
        )
    ).scalar_one_or_none()
    if connector is None:
        msg = f"connector not found: {body.connector_id}"
        raise NotFound(msg)
    existing = (
        await session.execute(
            select(ConnectorBinding).where(
                ConnectorBinding.project_id == project_id,
                ConnectorBinding.connector_id == body.connector_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.enabled = body.enabled
        existing.config_jsonb = body.config_jsonb
        await session.flush()
        binding = existing
        action = "connector_binding.update"
    else:
        binding = ConnectorBinding(
            id=uuid7(),
            project_id=project_id,
            connector_id=body.connector_id,
            enabled=body.enabled,
            config_jsonb=body.config_jsonb,
            created_by=principal.user_id,
            access_scope="project",
        )
        session.add(binding)
        await session.flush()
        action = "connector_binding.create"
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind=action,
        target_id=binding.id,
        target_kind="connector_binding",
        payload={
            "connector_kind": connector.kind,
            "enabled": body.enabled,
        },
        trace_id=current_trace_id(),
    )
    return ConnectorBindingOut.model_validate(binding)
