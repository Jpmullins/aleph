"""GET /v1/projects/{id}/ledger — paginated ledger events."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from aleph_api.deps import SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.schemas.ledger import LedgerEventOut
from aleph_db.models.ledger import ActionLedgerEvent

router = APIRouter(prefix="/v1/projects", tags=["ledger"])


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
