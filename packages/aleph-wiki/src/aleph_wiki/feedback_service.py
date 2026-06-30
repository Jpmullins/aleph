"""RejectionFeedback service.

Inc 1 wires the consumption side (wiki agent reads pending feedback on
compile and updates `addressed_in_revision_id` on commit). The full
analyst-facing rejection UI lands in Inc 5; until then rows can be
posted via `POST /v1/projects/{id}/wiki/feedback/rejection`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability import current_trace_id
from aleph_wiki.models import RejectionFeedback

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def write_feedback(
    session: AsyncSession,
    *,
    project_id: UUID,
    page_id: UUID | None,
    concept_name: str,
    rejected_revision_id: UUID | None,
    reason: str,
    rejected_by: UUID,
    ledger: LedgerWriter | None = None,
    actor_kind: str = "user",
) -> RejectionFeedback:
    fb = RejectionFeedback(
        id=uuid7(),
        project_id=project_id,
        page_id=page_id,
        concept_name=concept_name[:512],
        rejected_revision_id=rejected_revision_id,
        reason=reason[:4096],
        rejected_by=rejected_by,
        rejected_at=utcnow(),
        addressed_in_revision_id=None,
        created_by=rejected_by,
        access_scope="project",
    )
    session.add(fb)
    await session.flush()
    if ledger is not None:
        await ledger.append(
            project_id=project_id,
            actor_id=rejected_by,
            actor_kind=actor_kind,
            action_kind="wiki.feedback.write",
            target_id=fb.id,
            target_kind="rejection_feedback",
            payload={
                "concept_name": fb.concept_name,
                "page_id": str(page_id) if page_id else None,
            },
            trace_id=current_trace_id(),
        )
    return fb


async def pending_for_concept(
    session: AsyncSession,
    *,
    project_id: UUID,
    concept_name: str,
) -> list[RejectionFeedback]:
    stmt = (
        select(RejectionFeedback)
        .where(
            RejectionFeedback.project_id == project_id,
            RejectionFeedback.concept_name == concept_name,
            RejectionFeedback.addressed_in_revision_id.is_(None),
        )
        .order_by(RejectionFeedback.rejected_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_addressed(
    session: AsyncSession,
    *,
    feedback_ids: list[UUID],
    revision_id: UUID,
) -> None:
    if not feedback_ids:
        return
    stmt = select(RejectionFeedback).where(RejectionFeedback.id.in_(feedback_ids))
    rows = list((await session.execute(stmt)).scalars().all())
    for r in rows:
        r.addressed_in_revision_id = revision_id
    await session.flush()
