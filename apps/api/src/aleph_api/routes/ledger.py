"""GET /v1/projects/{id}/ledger — paginated ledger events."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from aleph_api.deps import SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.schemas.ledger import LedgerEventOut
from aleph_db.models.ledger import ActionLedgerEvent
from aleph_db.repos.ledger import verify_project_chain

router = APIRouter(prefix="/v1/projects", tags=["ledger"])


class ChainVerifyOut(BaseModel):
    ok: bool
    count: int
    first_divergence_event_id: UUID | None = None


@router.get("/{project_id}/ledger", response_model=list[LedgerEventOut])
async def get_ledger(
    project_id: ProjectScopeDep,
    session: SessionDep,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    actor_kind: Annotated[str | None, Query()] = None,
    action_kind: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[LedgerEventOut]:
    stmt = select(ActionLedgerEvent).where(ActionLedgerEvent.project_id == project_id)
    if since:
        stmt = stmt.where(ActionLedgerEvent.timestamp >= since)
    if until:
        stmt = stmt.where(ActionLedgerEvent.timestamp <= until)
    if actor_kind:
        stmt = stmt.where(ActionLedgerEvent.actor_kind == actor_kind)
    if action_kind:
        stmt = stmt.where(ActionLedgerEvent.action_kind == action_kind)
    stmt = stmt.order_by(ActionLedgerEvent.timestamp.desc()).limit(limit)
    rows = list((await session.execute(stmt)).scalars().all())
    return [LedgerEventOut.model_validate(r) for r in rows]


@router.get("/{project_id}/ledger/verify", response_model=ChainVerifyOut)
async def verify_ledger(
    project_id: ProjectScopeDep,
    session: SessionDep,
) -> ChainVerifyOut:
    result = await verify_project_chain(session, project_id)
    return ChainVerifyOut(
        ok=result.ok,
        count=result.count,
        first_divergence_event_id=(
            result.first_divergence.event_id if result.first_divergence else None
        ),
    )
