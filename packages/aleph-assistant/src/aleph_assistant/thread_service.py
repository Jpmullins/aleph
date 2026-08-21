"""Sessions/threads/messages CRUD."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from aleph_assistant.models import (
    AssistantMessage,
    AssistantSession,
    AssistantThread,
)
from aleph_core.errors import NotFound
from aleph_core.ids import uuid7
from aleph_core.time import utcnow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def create_session(
    session: AsyncSession,
    *,
    project_id: UUID,
    title: str,
    created_by: UUID,
) -> tuple[AssistantSession, AssistantThread]:
    s = AssistantSession(
        id=uuid7(),
        project_id=project_id,
        title=title[:255] or "New session",
        last_activity_at=utcnow(),
        created_by=created_by,
    )
    session.add(s)
    await session.flush()
    t = AssistantThread(
        id=uuid7(),
        project_id=project_id,
        session_id=s.id,
        parent_thread_id=None,
        title=None,
        created_by=created_by,
    )
    session.add(t)
    await session.flush()
    return s, t


async def list_sessions(session: AsyncSession, *, project_id: UUID) -> list[AssistantSession]:
    stmt = (
        select(AssistantSession)
        .where(AssistantSession.project_id == project_id)
        .order_by(AssistantSession.last_activity_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_threads(session: AsyncSession, *, session_id: UUID) -> list[AssistantThread]:
    stmt = (
        select(AssistantThread)
        .where(AssistantThread.session_id == session_id)
        .order_by(AssistantThread.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_thread(
    session: AsyncSession, *, project_id: UUID, thread_id: UUID
) -> AssistantThread | None:
    return (
        await session.execute(
            select(AssistantThread).where(
                AssistantThread.id == thread_id,
                AssistantThread.project_id == project_id,
            )
        )
    ).scalar_one_or_none()


async def fork_thread(
    session: AsyncSession,
    *,
    project_id: UUID,
    parent_thread_id: UUID,
    from_ordinal: int,
    created_by: UUID,
) -> AssistantThread:
    parent = await get_thread(session, project_id=project_id, thread_id=parent_thread_id)
    if parent is None:
        msg = f"thread {parent_thread_id} not found"
        raise NotFound(msg)
    new_thread = AssistantThread(
        id=uuid7(),
        project_id=project_id,
        session_id=parent.session_id,
        parent_thread_id=parent.id,
        title=parent.title,
        created_by=created_by,
    )
    session.add(new_thread)
    await session.flush()
    # Copy messages [ordinal 0 .. from_ordinal].
    stmt = (
        select(AssistantMessage)
        .where(
            AssistantMessage.thread_id == parent.id,
            AssistantMessage.ordinal <= from_ordinal,
        )
        .order_by(AssistantMessage.ordinal.asc())
    )
    for m in (await session.execute(stmt)).scalars().all():
        session.add(
            AssistantMessage(
                id=uuid7(),
                project_id=project_id,
                thread_id=new_thread.id,
                ordinal=m.ordinal,
                role=m.role,
                body_md=m.body_md,
                status="complete",
                retrieval_jsonb=dict(m.retrieval_jsonb),
                attached_cards_jsonb=list(m.attached_cards_jsonb),
                created_by=created_by,
            )
        )
    await session.flush()
    return new_thread
