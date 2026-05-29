"""A2UI card action API + catalog endpoint.

`POST /v1/projects/{id}/cards/actions` is the single dispatch chokepoint
for every A2UI card interaction. It routes through `ActionRouter`,
records a `CardAction` row, writes a ledger event, and returns the
handler's structured result.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, Field

from aleph_a2ui.action_router import ActionRouter, CardActionRequest
from aleph_a2ui.catalog import CATALOG
from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1", tags=["cards"])


class CardActionIn(BaseModel):
    surface_kind: str
    action_kind: str
    card_id: UUID | None = None
    target_id: UUID | None = None
    target_kind: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class CardActionOut(BaseModel):
    action_id: UUID
    ok: bool
    result: dict[str, Any]


@router.get("/a2ui/catalog")
async def get_catalog() -> dict[str, Any]:
    """Exposes the catalog JSON Schema so clients can validate against
    the same contract the server uses."""
    return CATALOG


@router.post(
    "/projects/{project_id}/cards/actions",
    response_model=CardActionOut,
)
async def dispatch_card_action(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[CardActionIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> CardActionOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    router_obj: ActionRouter = request.app.state.action_router
    result = await router_obj.dispatch(
        session=session,
        ledger=ledger,
        principal=principal,
        project_id=project_id,
        request=CardActionRequest(
            surface_kind=body.surface_kind,
            action_kind=body.action_kind,
            card_id=body.card_id,
            target_id=body.target_id,
            target_kind=body.target_kind,
            params=body.params,
        ),
    )
    return CardActionOut(action_id=result.action_id, ok=result.ok, result=result.result)
