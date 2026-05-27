"""Notes API."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, status
from pydantic import BaseModel, ConfigDict, Field

from aleph_core.errors import NotFound
from aleph_notes.models import Note, NoteSection
from aleph_notes.note_service import (
    create_note,
    create_section,
    get_note,
    list_notes,
    update_section,
)
from aleph_observability.tracing import current_trace_id
from aleph_security.roles import ProjectRole, require_at_least

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep

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
async def get_notes(
    project_id: ProjectScopeDep, session: SessionDep
) -> list[NoteOut]:
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


@router.get(
    "/{project_id}/notes/{note_id}", response_model=NoteDetailOut
)
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
