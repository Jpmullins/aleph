"""Hypothesis CRUD + evidence + version recording."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_hypotheses.confidence import (
    Confidence,
    EvidenceRow,
    next_confidence_from_evidence,
)
from aleph_hypotheses.models import (
    Hypothesis,
    HypothesisEvidence,
    HypothesisVersion,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal


async def _next_short_id(session: "AsyncSession") -> str:
    n = (await session.execute(select(func.count()).select_from(Hypothesis))).scalar_one()
    return f"H{int(n) + 1:04d}"


async def create_hypothesis(
    session: "AsyncSession",
    *,
    ledger: "LedgerWriter",
    principal: "Principal",
    project_id: UUID,
    title: str,
    statement: str,
) -> Hypothesis:
    hypothesis = Hypothesis(
        id=uuid7(),
        project_id=project_id,
        short_id=await _next_short_id(session),
        title=title[:512],
        statement=statement,
        confidence=Confidence.UNDER_INVESTIGATION.value,
        status="active",
        current_version_id=None,
        last_evidence_change_at=None,
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(hypothesis)
    await session.flush()

    event = await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="hypothesis.create",
        target_id=hypothesis.id,
        target_kind="hypothesis",
        payload={"short_id": hypothesis.short_id, "title": hypothesis.title},
        trace_id=None,
    )

    version = HypothesisVersion(
        id=uuid7(),
        project_id=project_id,
        hypothesis_id=hypothesis.id,
        version_no=1,
        statement=statement,
        confidence=hypothesis.confidence,
        rationale="initial",
        author_kind=principal.actor_kind,
        author_id=principal.user_id,
        parent_version_id=None,
        ledger_event_id=event.id,
    )
    session.add(version)
    await session.flush()
    hypothesis.current_version_id = version.id
    await session.flush()
    return hypothesis


async def get_hypothesis(
    session: "AsyncSession", *, project_id: UUID, hypothesis_id: UUID
) -> Hypothesis | None:
    return (
        await session.execute(
            select(Hypothesis).where(
                Hypothesis.id == hypothesis_id,
                Hypothesis.project_id == project_id,
            )
        )
    ).scalar_one_or_none()


async def list_hypotheses(
    session: "AsyncSession", *, project_id: UUID
) -> list[Hypothesis]:
    stmt = (
        select(Hypothesis)
        .where(Hypothesis.project_id == project_id)
        .order_by(Hypothesis.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def record_version(
    session: "AsyncSession",
    *,
    ledger: "LedgerWriter",
    principal: "Principal",
    hypothesis: Hypothesis,
    statement: str,
    confidence: str,
    rationale: str,
) -> HypothesisVersion:
    if hypothesis.current_version_id is None:
        msg = "hypothesis has no current version"
        raise ValidationFailed(msg)
    max_no = (
        await session.execute(
            select(func.coalesce(func.max(HypothesisVersion.version_no), 0))
            .where(HypothesisVersion.hypothesis_id == hypothesis.id)
        )
    ).scalar_one()
    new_no = int(max_no) + 1

    event = await ledger.append(
        project_id=hypothesis.project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="hypothesis.version.create",
        target_id=hypothesis.id,
        target_kind="hypothesis",
        payload={
            "version_no": new_no,
            "confidence": confidence,
            "rationale": rationale[:1024],
        },
        trace_id=None,
    )

    version = HypothesisVersion(
        id=uuid7(),
        project_id=hypothesis.project_id,
        hypothesis_id=hypothesis.id,
        version_no=new_no,
        statement=statement,
        confidence=confidence,
        rationale=rationale,
        author_kind=principal.actor_kind,
        author_id=principal.user_id,
        parent_version_id=hypothesis.current_version_id,
        ledger_event_id=event.id,
    )
    session.add(version)
    hypothesis.current_version_id = version.id
    hypothesis.confidence = confidence
    hypothesis.statement = statement
    await session.flush()
    return version


async def add_evidence(
    session: "AsyncSession",
    *,
    ledger: "LedgerWriter",
    principal: "Principal",
    hypothesis_id: UUID,
    stance: str,
    evidence_kind: str,
    target_id: UUID,
    weight: float = 1.0,
    note: str = "",
) -> HypothesisEvidence:
    if stance not in ("supports", "contradicts", "contextualizes"):
        msg = f"invalid stance: {stance}"
        raise ValidationFailed(msg)
    hypothesis = await session.get(Hypothesis, hypothesis_id)
    if hypothesis is None:
        msg = f"hypothesis {hypothesis_id} not found"
        raise NotFound(msg)
    if hypothesis.current_version_id is None:
        msg = "hypothesis has no current version"
        raise ValidationFailed(msg)

    ev = HypothesisEvidence(
        id=uuid7(),
        project_id=hypothesis.project_id,
        hypothesis_id=hypothesis_id,
        hypothesis_version_id=hypothesis.current_version_id,
        stance=stance,
        evidence_kind=evidence_kind,
        target_id=target_id,
        weight=weight,
        note=note,
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(ev)
    hypothesis.last_evidence_change_at = utcnow()
    await session.flush()

    # Re-derive confidence + record a new version if it changed.
    all_ev = list(
        (
            await session.execute(
                select(HypothesisEvidence).where(
                    HypothesisEvidence.hypothesis_id == hypothesis_id
                )
            )
        ).scalars().all()
    )
    new_conf = next_confidence_from_evidence(
        [EvidenceRow(stance=e.stance, weight=e.weight) for e in all_ev]
    )
    if new_conf.value != hypothesis.confidence:
        await record_version(
            session,
            ledger=ledger,
            principal=principal,
            hypothesis=hypothesis,
            statement=hypothesis.statement,
            confidence=new_conf.value,
            rationale=f"Evidence-driven transition to {new_conf.value}",
        )
    await ledger.append(
        project_id=hypothesis.project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="hypothesis.evidence.add",
        target_id=ev.id,
        target_kind="hypothesis_evidence",
        payload={
            "hypothesis_id": str(hypothesis_id),
            "stance": stance,
            "evidence_kind": evidence_kind,
            "weight": weight,
        },
        trace_id=None,
    )
    return ev
