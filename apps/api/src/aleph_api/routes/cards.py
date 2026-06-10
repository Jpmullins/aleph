"""A2UI card action API + catalog endpoint.

`POST /v1/projects/{id}/cards/actions` is the single dispatch chokepoint
for every A2UI card interaction. It routes through `ActionRouter`,
records a `CardAction` row, writes a ledger event, and returns the
handler's structured result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_a2ui.action_router import ActionRouter, CardActionRequest
from aleph_a2ui.card_service import pin_card
from aleph_a2ui.models import CardAction
from aleph_a2ui.catalog import CATALOG, CatalogValidationError, validate_component
from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import ValidationFailed
from aleph_core.ids import uuid7
from aleph_observability.tracing import current_trace_id
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


class CardActionRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    surface_kind: str
    action_kind: str
    card_id: UUID | None
    target_id: UUID | None
    target_kind: str | None
    result: dict[str, Any] = Field(validation_alias="result_jsonb")
    actor_kind: str
    created_at: datetime


@router.get(
    "/projects/{project_id}/cards/actions",
    response_model=list[CardActionRowOut],
)
async def list_card_actions(
    project_id: ProjectScopeDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[CardActionRowOut]:
    """Recent card actions, newest first — the agent's view of what the
    analyst did on the surfaces (approvals, dismissals, unpins, ...)."""
    rows = list(
        (
            await session.execute(
                select(CardAction)
                .where(CardAction.project_id == project_id)
                .order_by(CardAction.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [CardActionRowOut.model_validate(r) for r in rows]


class CardPinIn(BaseModel):
    card_kind: str
    title: str = Field(min_length=1, max_length=255)
    props: dict[str, Any] = Field(default_factory=dict)


class CardPinOut(BaseModel):
    card_id: UUID


@router.post(
    "/projects/{project_id}/cards/pin",
    status_code=201,
    response_model=CardPinOut,
)
async def pin_card_route(
    project_id: ProjectScopeDep,
    body: Annotated[CardPinIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> CardPinOut:
    """Persist a catalog-validated card to the Briefs pile.

    Agents call this (via the pin_to_briefs tool) so chart/table/claim cards
    survive the chat transcript; the Briefs surface unions pinned cards in.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    card_id = uuid7()
    payload = {"type": body.card_kind, "id": f"pinned-{card_id}", "props": body.props}
    try:
        validate_component(payload)
    except CatalogValidationError as exc:
        msg = f"card payload rejected by catalog: {exc}"
        raise ValidationFailed(msg) from exc
    event = await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="card.pin",
        target_id=card_id,
        target_kind="interactive_card",
        payload={"card_kind": body.card_kind, "title": body.title, "pinned_to": "briefs"},
        trace_id=current_trace_id(),
    )
    await pin_card(
        session,
        project_id=project_id,
        card_id=card_id,
        card_kind=body.card_kind,
        title=body.title,
        payload=payload,
        author_id=principal.user_id,
        author_kind=principal.actor_kind,
        ledger_event_id=event.id,
        trace_id=current_trace_id(),
    )
    return CardPinOut(card_id=card_id)


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
