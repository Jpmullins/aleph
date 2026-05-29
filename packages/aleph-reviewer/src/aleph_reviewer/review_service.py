"""ReviewRun / ReviewFinding service helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_reviewer.models import ReviewFinding, ReviewRun

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def start_run(
    session: AsyncSession,
    *,
    project_id: UUID,
    kind: str,
    trigger: str,
    target_revision_id: UUID | None,
    target_scope: str,
    agent_run_id: UUID,
    created_by: UUID,
) -> ReviewRun:
    run = ReviewRun(
        id=uuid7(),
        project_id=project_id,
        kind=kind,
        trigger=trigger,
        target_revision_id=target_revision_id,
        target_scope=target_scope,
        agent_run_id=agent_run_id,
        status="running",
        finding_count=0,
        started_at=utcnow(),
        created_by=created_by,
        access_scope="project",
    )
    session.add(run)
    await session.flush()
    return run


async def add_finding(
    session: AsyncSession,
    *,
    review_run_id: UUID,
    project_id: UUID,
    finding_kind: str,
    severity: str,
    title: str,
    description: str,
    target_page_id: UUID | None = None,
    target_revision_id: UUID | None = None,
    target_section_anchor: str | None = None,
    target_claim_id: UUID | None = None,
    target_source_id: UUID | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    proposed_patch: dict[str, Any] | None = None,
    auto_resolvable: bool = False,
    created_by: UUID,
) -> ReviewFinding:
    f = ReviewFinding(
        id=uuid7(),
        project_id=project_id,
        review_run_id=review_run_id,
        finding_kind=finding_kind,
        severity=severity,
        title=title[:255],
        description=description,
        target_page_id=target_page_id,
        target_revision_id=target_revision_id,
        target_section_anchor=target_section_anchor,
        target_claim_id=target_claim_id,
        target_source_id=target_source_id,
        evidence_refs_jsonb=evidence_refs or [],
        proposed_patch_jsonb=proposed_patch,
        auto_resolvable=auto_resolvable,
        status="open",
        created_by=created_by,
        access_scope="project",
    )
    session.add(f)
    await session.flush()
    return f


async def finalize_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    status: str,
    finding_count: int,
) -> None:
    run = await session.get(ReviewRun, run_id)
    if run is None:
        return
    run.status = status
    run.finding_count = finding_count
    run.completed_at = utcnow()
    await session.flush()


async def list_findings(
    session: AsyncSession,
    *,
    project_id: UUID,
    status_filter: str | None = None,
) -> list[ReviewFinding]:
    stmt = (
        select(ReviewFinding)
        .where(ReviewFinding.project_id == project_id)
        .order_by(ReviewFinding.created_at.desc())
    )
    if status_filter:
        stmt = stmt.where(ReviewFinding.status == status_filter)
    return list((await session.execute(stmt)).scalars().all())
