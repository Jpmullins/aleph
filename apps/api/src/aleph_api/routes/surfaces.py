"""Surface composition API.

`GET /v1/projects/{id}/surfaces/{tab}` returns the A2UI surface JSON for
a right-panel tab. The renderer subscribes to updates via the existing
SSE channel; for Inc 4 this returns a one-shot snapshot.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from aleph_a2ui.components.cards import (
    ApprovalCardProps,
    ClaimCardProps,
    approval_card,
    claim_card,
)
from aleph_a2ui.components.surfaces import (
    artifacts_surface_v09,
    briefs_surface_v09,
    hypotheses_surface_v09,
    notes_surface_v09,
    wiki_surface_v09,
)
from aleph_api.deps import SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import NotFound, ValidationFailed
from aleph_hypotheses.hypothesis_service import list_hypotheses
from aleph_hypotheses.models import HypothesisEvidence
from aleph_notes.models import Note, NoteSection
from aleph_wiki.models import WikiClaim, WikiPage

router = APIRouter(prefix="/v1/projects", tags=["surfaces"])


class SurfaceMessagesOut(BaseModel):
    """A2UI v0.9 message-list payload (Wave 4).

    Every right-panel tab is rendered through the upstream `@a2ui` v0_9
    `MessageProcessor` + `<A2uiSurface>` against the shared catalog, so each
    body carries an ordered list of server-to-client messages (`createSurface`
    + `updateComponents` [+ `updateDataModel`]) instead of the legacy
    `{tab, surface}` tree.
    """

    tab: str
    messages: list[dict[str, Any]]


@router.get("/{project_id}/surfaces/{tab}", response_model=None)
async def get_surface(
    project_id: ProjectScopeDep,
    tab: str,
    session: SessionDep,
    page_id: str | None = Query(default=None),
) -> SurfaceMessagesOut:
    tab_lc = tab.lower()
    if tab_lc == "wiki":
        return SurfaceMessagesOut(
            tab=tab_lc,
            messages=await _wiki_messages(session, project_id, page_id),
        )
    if tab_lc == "artifacts":
        return SurfaceMessagesOut(tab=tab_lc, messages=artifacts_surface_v09())
    if tab_lc == "notes":
        return SurfaceMessagesOut(
            tab=tab_lc, messages=await _notes_messages(session, project_id)
        )
    if tab_lc == "hypotheses":
        return SurfaceMessagesOut(
            tab=tab_lc,
            messages=await _hypotheses_messages(session, project_id),
        )
    if tab_lc == "briefs":
        return SurfaceMessagesOut(
            tab=tab_lc, messages=await _briefs_messages(session, project_id)
        )
    msg = f"unknown tab: {tab}"
    raise NotFound(msg)


async def _hypotheses_messages(session: Any, project_id: UUID) -> list[dict[str, Any]]:
    """Build the v0.9 message list for the Hypotheses tab.

    One `HypothesisCard` per hypothesis, with evidence counts aggregated from
    `hypothesis_evidence`. Props bind into `/items/<i>/...` (Wave 4 T6 delta
    path).
    """
    hyps = await list_hypotheses(session, project_id=project_id)
    counts: dict[UUID, int] = {}
    ev_rows = list(
        (
            await session.execute(
                select(HypothesisEvidence.hypothesis_id).where(
                    HypothesisEvidence.project_id == project_id
                )
            )
        )
        .scalars()
        .all()
    )
    for hid in ev_rows:
        counts[hid] = counts.get(hid, 0) + 1
    return hypotheses_surface_v09(
        hypotheses=[
            {
                "hypothesis_id": str(h.id),
                "title": h.title,
                "confidence": h.confidence,
                "evidence_count": counts.get(h.id, 0),
            }
            for h in hyps
        ]
    )


async def _wiki_messages(
    session: Any, project_id: UUID, page_id: str | None
) -> list[dict[str, Any]]:
    """v0.9 message list for the Wiki tab — a single `WikiSurface`.

    The frontend `WikiSurface` fetches `/wiki/pages` directly and renders its own
    page browser + reader; `children` is reserved for embedded viz/claim cards
    placed on the surface. When a `page_id` is supplied we attach that page's
    claims as `ClaimCard`s (legacy `A2UIComponent` `{type,id,props}` shape, which
    the surface's renderer consumes).
    """
    cards: list[dict[str, Any]] = []
    if page_id:
        try:
            pid = UUID(page_id)
        except ValueError as exc:
            msg = "invalid page_id"
            raise ValidationFailed(msg) from exc
        page = await session.get(WikiPage, pid)
        if page and page.project_id == project_id and page.current_revision_id is not None:
            claim_rows = list(
                (
                    await session.execute(
                        select(WikiClaim).where(
                            WikiClaim.revision_id == page.current_revision_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            for c in claim_rows[:25]:
                cards.append(
                    claim_card(
                        ClaimCardProps(
                            claim_id=c.id,
                            text=c.text,
                            confidence=c.confidence,
                        ),
                        card_id=f"claim-{c.id}",
                    )
                )
    return wiki_surface_v09(
        current_page_id=UUID(page_id) if page_id else None,
        view_mode="page",
        children=cards,
    )


async def _notes_messages(session: Any, project_id: UUID) -> list[dict[str, Any]]:
    """v0.9 message list for the Notes tab — a single `NotesSurface`.

    The surface view fetches its own note list + editor; we forward up to 10
    notes' sections as `NotebookCellCard` children for any agent/embedded use.
    """
    rows = list(
        (
            await session.execute(
                select(Note).where(Note.project_id == project_id).order_by(Note.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    cards: list[dict[str, Any]] = []
    for n in rows[:10]:
        sec_stmt = (
            select(NoteSection)
            .where(NoteSection.note_id == n.id)
            .order_by(NoteSection.ordinal.asc())
            .limit(5)
        )
        sec_rows = list((await session.execute(sec_stmt)).scalars().all())
        for s in sec_rows:
            cards.append(
                {
                    "type": "NotebookCellCard",
                    "id": f"section-{s.id}",
                    "props": {
                        "section_id": str(s.id),
                        "body_md": s.body_md,
                        "ordinal": s.ordinal,
                        "edit_action": "edit_note",
                    },
                }
            )
    return notes_surface_v09(children=cards)


async def _briefs_messages(session: Any, project_id: UUID) -> list[dict[str, Any]]:
    """v0.9 message list for the Briefs tab — a single `BriefsSurface`.

    Pending `SynthesisProposal`s render as `ApprovalCard` children (the legacy
    `A2UIComponent` `{type,id,props}` shape the surface view consumes).
    """
    from aleph_connectors.models import SynthesisProposal

    rows = list(
        (
            await session.execute(
                select(SynthesisProposal).where(
                    SynthesisProposal.project_id == project_id,
                    SynthesisProposal.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    cards: list[dict[str, Any]] = []
    for p in rows:
        cards.append(
            approval_card(
                ApprovalCardProps(
                    target_id=p.page_id,
                    target_kind="wiki_page",
                    title=f"Synthesis: {p.topic}",
                    summary=f"Approve the proposed wiki revision for “{p.topic}”.",
                    severity="info",
                ),
                card_id=f"synth-{p.id}",
            )
        )
    return briefs_surface_v09(badge_count=len(rows), children=cards)
