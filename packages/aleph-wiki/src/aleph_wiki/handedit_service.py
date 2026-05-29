"""Hand-edit mark + clear. Wiki agent reads via `list_active_for_page`."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from aleph_core.errors import NotFound
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_wiki.models import HandEditMark, WikiPage, WikiRevision

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def mark_section(
    session: AsyncSession,
    *,
    project_id: UUID,
    page_id: UUID,
    section_anchor: str | None,
    applied_by: UUID,
) -> HandEditMark:
    page = (
        await session.execute(select(WikiPage).where(WikiPage.id == page_id))
    ).scalar_one_or_none()
    if page is None:
        msg = f"wiki page {page_id} not found"
        raise NotFound(msg)
    body_sha = ""
    if page.current_revision_id is not None:
        rev = (
            await session.execute(
                select(WikiRevision).where(WikiRevision.id == page.current_revision_id)
            )
        ).scalar_one_or_none()
        if rev is not None:
            body_sha = (
                rev.body_sha256
                if section_anchor is None
                else hashlib.sha256(rev.body_md.encode("utf-8")).hexdigest()
            )
    mark = HandEditMark(
        id=uuid7(),
        project_id=project_id,
        page_id=page_id,
        section_anchor=section_anchor,
        body_sha256_at_edit=body_sha or "0" * 64,
        applied_at=utcnow(),
        applied_by=applied_by,
        cleared_at=None,
        cleared_by=None,
        created_by=applied_by,
        access_scope="project",
    )
    session.add(mark)
    await session.flush()
    return mark


async def clear_section(
    session: AsyncSession,
    *,
    project_id: UUID,
    page_id: UUID,
    section_anchor: str | None,
    cleared_by: UUID,
) -> int:
    """Clears all active hand-edit marks for the given (page, anchor)."""
    stmt = select(HandEditMark).where(
        HandEditMark.project_id == project_id,
        HandEditMark.page_id == page_id,
        HandEditMark.cleared_at.is_(None),
    )
    if section_anchor is not None:
        stmt = stmt.where(HandEditMark.section_anchor == section_anchor)
    rows = list((await session.execute(stmt)).scalars().all())
    now = utcnow()
    for m in rows:
        m.cleared_at = now
        m.cleared_by = cleared_by
    await session.flush()
    return len(rows)


async def list_active_for_page(
    session: AsyncSession, *, project_id: UUID, page_id: UUID
) -> list[HandEditMark]:
    stmt = select(HandEditMark).where(
        HandEditMark.project_id == project_id,
        HandEditMark.page_id == page_id,
        HandEditMark.cleared_at.is_(None),
    )
    return list((await session.execute(stmt)).scalars().all())
