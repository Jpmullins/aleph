"""InteractiveCard pin/unpin service.

Persists agent- or analyst-authored A2UI cards so they outlive the chat
transcript: a pinned card is an `InteractiveCard` row plus an immutable
`InteractiveCardVersion` carrying the catalog-validated `{type,id,props}`
payload. Surfaces (Briefs today) union pinned cards into their children.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from aleph_a2ui.catalog import CATALOG_VERSION
from aleph_a2ui.models import InteractiveCard, InteractiveCardVersion
from aleph_core.errors import NotFound
from aleph_core.ids import uuid7

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def pin_card(
    session: AsyncSession,
    *,
    project_id: UUID,
    card_id: UUID,
    card_kind: str,
    title: str,
    payload: dict[str, Any],
    author_id: UUID,
    author_kind: str,
    ledger_event_id: UUID,
    pinned_to: str = "briefs",
    trace_id: str | None = None,
) -> InteractiveCard:
    card = InteractiveCard(
        id=card_id,
        project_id=project_id,
        card_kind=card_kind,
        catalog_version=CATALOG_VERSION,
        title=title[:255] or None,
        pinned_to=pinned_to,
        created_by=author_id,
    )
    session.add(card)
    version = InteractiveCardVersion(
        id=uuid7(),
        project_id=project_id,
        card_id=card_id,
        version_no=1,
        a2ui_payload_jsonb=payload,
        data_model_jsonb={},
        parent_version_id=None,
        author_kind=author_kind,
        author_id=author_id,
        trace_id=trace_id,
        ledger_event_id=ledger_event_id,
    )
    session.add(version)
    await session.flush()
    card.current_version_id = version.id
    await session.flush()
    return card


async def unpin_card(session: AsyncSession, *, project_id: UUID, card_id: UUID) -> InteractiveCard:
    card = (
        await session.execute(
            select(InteractiveCard).where(
                InteractiveCard.id == card_id,
                InteractiveCard.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if card is None:
        msg = f"card {card_id} not found"
        raise NotFound(msg)
    card.pinned_to = None
    card.pinned_target_id = None
    await session.flush()
    return card


async def list_pinned(
    session: AsyncSession, *, project_id: UUID, pinned_to: str = "briefs"
) -> list[tuple[InteractiveCard, InteractiveCardVersion]]:
    """Pinned cards with their current version payloads, newest first."""
    cards = list(
        (
            await session.execute(
                select(InteractiveCard)
                .where(
                    InteractiveCard.project_id == project_id,
                    InteractiveCard.pinned_to == pinned_to,
                )
                .order_by(InteractiveCard.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    out: list[tuple[InteractiveCard, InteractiveCardVersion]] = []
    for card in cards:
        if card.current_version_id is None:
            continue
        version = await session.get(InteractiveCardVersion, card.current_version_id)
        if version is not None:
            out.append((card, version))
    return out
