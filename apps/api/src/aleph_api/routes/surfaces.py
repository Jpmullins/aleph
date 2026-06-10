"""Surface composition API.

`GET /v1/projects/{id}/surfaces/{tab}` returns the A2UI surface JSON for
a right-panel tab. The renderer subscribes to updates via the existing
SSE channel; for Inc 4 this returns a one-shot snapshot.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from starlette.responses import StreamingResponse

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
from aleph_a2ui.surface_streamer import (
    data_model_patches_to_messages,
    diff_data_model,
    split_surface_messages,
)
from aleph_api.deps import PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep, assert_stream_access
from aleph_core.errors import NotFound, ValidationFailed
from aleph_notes.models import Note, NoteSection
from aleph_wiki.models import WikiClaim, WikiPage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter(prefix="/v1/projects", tags=["surfaces"])

# Poll-fallback window for the delta stream. The stream wakes on a push signal
# (any mutation for the project) and recomputes-and-diffs the tab's surface,
# emitting `updateDataModel` deltas only when the model actually changed (no diff
# → nothing emitted). Absent any push it recomputes after this many seconds as a
# self-healing safety net. Surface recompute hits Postgres harder than the
# agent-events requery (full tab rebuild), so its fallback is more conservative.
_STREAM_FALLBACK_SEC = 10.0


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


async def _build_tab_messages(
    session: Any, project_id: UUID, tab_lc: str, page_id: str | None = None
) -> list[dict[str, Any]]:
    """Build the v0.9 message list for `tab_lc`. Shared by the snapshot route
    and the delta stream so both compute identical surfaces."""
    if tab_lc == "wiki":
        return await _wiki_messages(session, project_id, page_id)
    if tab_lc == "artifacts":
        return artifacts_surface_v09()
    if tab_lc == "notes":
        return await _notes_messages(session, project_id)
    if tab_lc == "hypotheses":
        return await _hypotheses_messages(session, project_id)
    if tab_lc == "briefs":
        return await _briefs_messages(session, project_id)
    msg = f"unknown tab: {tab_lc}"
    raise NotFound(msg)


@router.get("/{project_id}/surfaces/{tab}", response_model=None)
async def get_surface(
    project_id: ProjectScopeDep,
    tab: str,
    session: SessionDep,
    page_id: str | None = Query(default=None),
) -> SurfaceMessagesOut:
    tab_lc = tab.lower()
    messages = await _build_tab_messages(session, project_id, tab_lc, page_id)
    return SurfaceMessagesOut(tab=tab_lc, messages=messages)


@router.get("/{project_id}/surfaces/{tab}/stream", response_model=None)
async def stream_surface(
    project_id: Annotated[UUID, Path(...)],
    tab: str,
    request: Request,
    principal: PrincipalDep,
    page_id: str | None = Query(default=None),
) -> StreamingResponse:
    """Delta SurfaceStreamer (Wave 4 T6).

    On connect, emits the full v0_9 surface for `tab` (each `createSurface` /
    `updateComponents` / root `updateDataModel` as its own SSE `data:` event).
    Then, every `_STREAM_RECOMPUTE_SEC`, rebuilds the surface and emits:

    * an `updateComponents` message *iff* the structural component list changed
      (new/removed cards) — the processor updates existing components in place
      (no DOM teardown) and only ADDS genuinely-new ones; and
    * one `updateDataModel` message per minimal data-model patch
      (`diff_data_model`), so a bound prop change (e.g. a hypothesis confidence)
      re-renders only that prop, not the whole tree.

    If neither changed, nothing is emitted (just a heartbeat). Only the
    Hypotheses tab binds data into the model today, so it is the only tab that
    produces data deltas; the other four emit their structural surface once and
    then idle — which is correct, they self-refresh via react-query.

    Change signal: LISTEN/NOTIFY push (any mutation for the project) wakes a
    recompute-and-diff instantly, with a `_STREAM_FALLBACK_SEC` poll fallback.
    Recompute-and-diff stays the universal, tab-agnostic detector (surfaces
    aggregate across many entities/mutation kinds); the push just replaces the
    old fixed 2.5s poll as the wake trigger.
    """
    # Membership check WITHOUT pinning a pool connection for the stream's life.
    await assert_stream_access(request, project_id, principal)
    tab_lc = tab.lower()
    surface_id = tab_lc
    maker = request.app.state.session_maker
    broker = request.app.state.change_broker

    async def _gen() -> AsyncIterator[bytes]:
        # Initial full surface.
        async with maker() as session:
            messages = await _build_tab_messages(session, project_id, tab_lc, page_id)
        prev_structural, prev_model = split_surface_messages(messages)
        for m in messages:
            yield f"data: {json.dumps(m)}\n\n".encode()

        # Push: any mutation for this project wakes a recompute-and-diff the
        # instant it commits (the broker is fed by the LISTEN/NOTIFY listener).
        # `sub.wait` also returns after `_STREAM_FALLBACK_SEC` with no push, so a
        # dropped listener self-heals. Recompute-and-diff stays the universal,
        # tab-agnostic change detector; only the wake trigger changed (was a 2.5s
        # poll).
        async with broker.subscribe(project_id) as sub:
            while True:
                if await request.is_disconnected():
                    return
                await sub.wait(timeout=_STREAM_FALLBACK_SEC)
                if await request.is_disconnected():
                    return

                async with maker() as session:
                    fresh = await _build_tab_messages(session, project_id, tab_lc, page_id)
                structural, model = split_surface_messages(fresh)

                # Structural change (cards added/removed) → re-send updateComponents.
                # The processor updates existing component ids in place and only adds
                # new ones, so existing DOM nodes are preserved.
                if structural != prev_structural:
                    for m in structural:
                        if "updateComponents" in m:
                            yield f"data: {json.dumps(m)}\n\n".encode()
                    prev_structural = structural

                # Data-model deltas.
                patches = diff_data_model(prev_model, model)
                for delta in data_model_patches_to_messages(
                    surface_id=surface_id, patches=patches, next_model=model
                ):
                    yield f"data: {json.dumps(delta)}\n\n".encode()
                prev_model = model

                # Heartbeat so idle proxies don't close the connection.
                yield b": heartbeat\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _hypotheses_messages(session: Any, project_id: UUID) -> list[dict[str, Any]]:
    """Build the v0.9 message list for the Hypotheses tab.

    A single interactive `HypothesesSurface` (header, + New, ACH matrix,
    feedback, empty state) that fetches its own hypothesis list — the same
    pattern as the other four tabs.
    """
    return hypotheses_surface_v09()


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
                        select(WikiClaim).where(WikiClaim.revision_id == page.current_revision_id)
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
                    target_id=p.id,
                    target_kind="synthesis_proposal",
                    title=f"Synthesis: {p.topic}",
                    summary=f"Approve the proposed wiki revision for “{p.topic}”.",
                    severity="info",
                ),
                card_id=f"synth-{p.id}",
            )
        )
    return briefs_surface_v09(badge_count=len(rows), children=cards)
