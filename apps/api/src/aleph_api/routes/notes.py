"""Notes API."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, status
from pydantic import BaseModel, ConfigDict, Field

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_connectors.models import SynthesisProposal
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_notes.note_service import (
    create_note,
    create_section,
    get_note,
    list_notes,
    update_section,
)
from aleph_observability.tracing import current_trace_id
from aleph_security.roles import ProjectRole, require_at_least
from aleph_wiki.wiki_service import WikiService

router = APIRouter(prefix="/v1/projects", tags=["notes"])


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str


class NoteSectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    note_id: UUID
    ordinal: int
    body_md: str
    anchor: str | None


class NoteDetailOut(BaseModel):
    note: NoteOut
    sections: list[NoteSectionOut]


class NoteCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class SectionCreateIn(BaseModel):
    body_md: str = Field(default="", max_length=64_000)
    anchor: str | None = None


class SectionUpdateIn(BaseModel):
    body_md: str = Field(max_length=64_000)


@router.get("/{project_id}/notes", response_model=list[NoteOut])
async def get_notes(project_id: ProjectScopeDep, session: SessionDep) -> list[NoteOut]:
    rows = await list_notes(session, project_id=project_id)
    return [NoteOut.model_validate(r) for r in rows]


@router.post(
    "/{project_id}/notes",
    status_code=status.HTTP_201_CREATED,
    response_model=NoteOut,
)
async def post_note(
    project_id: ProjectScopeDep,
    body: Annotated[NoteCreateIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> NoteOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    n = await create_note(
        session,
        project_id=project_id,
        title=body.title,
        created_by=principal.user_id,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="note.create",
        target_id=n.id,
        target_kind="note",
        payload={"title": body.title},
        trace_id=current_trace_id(),
    )
    return NoteOut.model_validate(n)


@router.get("/{project_id}/notes/{note_id}", response_model=NoteDetailOut)
async def get_one_note(
    project_id: ProjectScopeDep, note_id: UUID, session: SessionDep
) -> NoteDetailOut:
    out = await get_note(session, project_id=project_id, note_id=note_id)
    if out is None:
        msg = f"note not found: {note_id}"
        raise NotFound(msg)
    note, sections = out
    return NoteDetailOut(
        note=NoteOut.model_validate(note),
        sections=[NoteSectionOut.model_validate(s) for s in sections],
    )


@router.post(
    "/{project_id}/notes/{note_id}/sections",
    status_code=status.HTTP_201_CREATED,
    response_model=NoteSectionOut,
)
async def post_section(
    project_id: ProjectScopeDep,
    note_id: UUID,
    body: Annotated[SectionCreateIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> NoteSectionOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    note_pair = await get_note(session, project_id=project_id, note_id=note_id)
    if note_pair is None:
        msg = f"note not found: {note_id}"
        raise NotFound(msg)
    s = await create_section(
        session,
        project_id=project_id,
        note_id=note_id,
        body_md=body.body_md,
        anchor=body.anchor,
        created_by=principal.user_id,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="note.section.create",
        target_id=s.id,
        target_kind="note_section",
        payload={"note_id": str(note_id), "ordinal": s.ordinal},
        trace_id=current_trace_id(),
    )
    return NoteSectionOut.model_validate(s)


@router.patch(
    "/{project_id}/notes/{note_id}/sections/{section_id}",
    response_model=NoteSectionOut,
)
async def patch_section(
    project_id: ProjectScopeDep,
    note_id: UUID,
    section_id: UUID,
    body: Annotated[SectionUpdateIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> NoteSectionOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    s = await update_section(
        session, project_id=project_id, section_id=section_id, body_md=body.body_md
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="note.section.update",
        target_id=s.id,
        target_kind="note_section",
        payload={"note_id": str(note_id), "ordinal": s.ordinal},
        trace_id=current_trace_id(),
    )
    return NoteSectionOut.model_validate(s)


class PromoteOut(BaseModel):
    proposal_id: UUID
    page_id: UUID
    revision_id: UUID


@router.post(
    "/{project_id}/notes/{note_id}/promote",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PromoteOut,
)
async def promote_note(
    project_id: ProjectScopeDep,
    note_id: UUID,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> PromoteOut:
    """Promote a note to a draft wiki page + pending proposal (Briefs).

    Concatenates the note's sections into a draft `synthesis` wiki page and
    records a pending SynthesisProposal so it lands in Briefs for approval —
    the analyst-authored counterpart to AIQ synthesis.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    out = await get_note(session, project_id=project_id, note_id=note_id)
    if out is None:
        msg = f"note not found: {note_id}"
        raise NotFound(msg)
    note, sections = out
    body_md = "\n\n".join(s.body_md for s in sections if s.body_md.strip())
    if not body_md.strip():
        msg = "cannot promote an empty note"
        raise ValidationFailed(msg)

    # Provenance run for the promotion action (SynthesisProposal needs one).
    run = AgentRun(
        id=uuid7(),
        project_id=project_id,
        agent_kind="note_promotion",
        correlation_id=f"note-{uuid7().hex[:12]}",
        status="succeeded",
        input_payload={"note_id": str(note_id)},
        created_by=principal.user_id,
        access_scope="project",
        completed_at=utcnow(),
    )
    session.add(run)
    await session.flush()

    svc = WikiService(session)
    result = await svc.commit_revision(
        principal=principal,
        ledger=ledger,
        project_id=project_id,
        page_id=None,
        title=note.title,
        slug=None,
        page_kind="synthesis",
        body_md=body_md,
        summary=body_md[:512],
        claims=[],
        wikilinks=[],
        commit_message=f"Promoted from note: {note.title}",
        respect_hand_edits=True,
    )
    proposal = SynthesisProposal(
        id=uuid7(),
        project_id=project_id,
        page_id=result.page_id,
        revision_id=result.revision_id,
        agent_run_id=run.id,
        topic=note.title[:512],
        status="pending",
        approval_decision_id=None,
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(proposal)
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="note.promote",
        target_id=proposal.id,
        target_kind="synthesis_proposal",
        payload={"note_id": str(note_id), "page_id": str(result.page_id)},
        trace_id=current_trace_id(),
    )
    return PromoteOut(
        proposal_id=proposal.id, page_id=result.page_id, revision_id=result.revision_id
    )
