"""Briefs API.

Returns the list of `SynthesisProposal`s (Inc 3) plus any future
`ReviewFinding`s (Inc 5) plus any pinned-to-briefs InteractiveCards.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from aleph_a2ui.components.cards import (
    ApprovalCardProps,
    approval_card,
)
from aleph_a2ui.components.surfaces import briefs_surface
from aleph_api.deps import SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_connectors.models import SynthesisProposal

router = APIRouter(prefix="/v1/projects", tags=["briefs"])


class BriefsOut(BaseModel):
    badge_count: int
    surface: dict[str, Any]


@router.get("/{project_id}/briefs", response_model=BriefsOut)
async def get_briefs(project_id: ProjectScopeDep, session: SessionDep) -> BriefsOut:
    stmt = (
        select(SynthesisProposal)
        .where(
            SynthesisProposal.project_id == project_id,
            SynthesisProposal.status == "pending",
        )
        .order_by(SynthesisProposal.created_at.desc())
    )
    proposals = list((await session.execute(stmt)).scalars().all())
    cards = [
        approval_card(
            ApprovalCardProps(
                target_id=p.id,
                target_kind="synthesis_proposal",
                title=p.topic,
                summary=f"Synthesis draft from agent_run {p.agent_run_id}",
                severity="medium",
            ),
            card_id=f"approval-{p.id}",
        )
        for p in proposals
    ]
    surface = briefs_surface(
        badge_count=len(cards),
        children=cards,
    )
    return BriefsOut(badge_count=len(cards), surface=surface)
