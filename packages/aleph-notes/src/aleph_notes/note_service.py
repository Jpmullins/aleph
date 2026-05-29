"""Note CRUD service."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from aleph_core.errors import NotFound
from aleph_core.ids import uuid7
from aleph_notes.models import Note, NoteSection

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def create_note(
    session: AsyncSession,
    *,
    project_id: UUID,
    title: str,
    created_by: UUID,
) -> Note:
    n = Note(
        id=uuid7(),
        project_id=project_id,
        title=title[:255] or "Untitled",
        created_by=created_by,
        access_scope="project",
    )
    session.add(n)
    await session.flush()
    return n


async def list_notes(session: AsyncSession, *, project_id: UUID) -> list[Note]:
    stmt = select(Note).where(Note.project_id == project_id).order_by(Note.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_note(
    session: AsyncSession, *, project_id: UUID, note_id: UUID
) -> tuple[Note, list[NoteSection]] | None:
    note = (
        await session.execute(select(Note).where(Note.id == note_id, Note.project_id == project_id))
    ).scalar_one_or_none()
    if note is None:
        return None
    sections = list(
        (
            await session.execute(
                select(NoteSection)
                .where(NoteSection.note_id == note_id)
                .order_by(NoteSection.ordinal.asc())
            )
        )
        .scalars()
        .all()
    )
    return note, sections


async def create_section(
    session: AsyncSession,
    *,
    project_id: UUID,
    note_id: UUID,
    body_md: str,
    anchor: str | None,
    created_by: UUID,
) -> NoteSection:
    next_ord = (
        await session.execute(
            select(func.coalesce(func.max(NoteSection.ordinal), -1)).where(
                NoteSection.note_id == note_id
            )
        )
    ).scalar_one() + 1
    s = NoteSection(
        id=uuid7(),
        project_id=project_id,
        note_id=note_id,
        ordinal=next_ord,
        body_md=body_md,
        anchor=anchor,
        created_by=created_by,
        access_scope="project",
    )
    session.add(s)
    await session.flush()
    return s


async def update_section(
    session: AsyncSession,
    *,
    project_id: UUID,
    section_id: UUID,
    body_md: str,
) -> NoteSection:
    s = (
        await session.execute(
            select(NoteSection).where(
                NoteSection.id == section_id,
                NoteSection.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if s is None:
        msg = f"section {section_id} not found"
        raise NotFound(msg)
    s.body_md = body_md
    await session.flush()
    return s
