"""Hypotheses API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, status
from pydantic import BaseModel, ConfigDict, Field

from aleph_core.errors import NotFound
from aleph_hypotheses.hypothesis_service import (
    add_evidence,
    create_hypothesis,
    get_hypothesis,
    list_hypotheses,
    record_version,
)
from aleph_hypotheses.models import (
    HypothesisEvidence,
    HypothesisVersion,
)
from aleph_security.roles import ProjectRole, require_at_least
from sqlalchemy import select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep

router = APIRouter(prefix="/v1/projects", tags=["hypotheses"])


class HypothesisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    short_id: str
    title: str
    statement: str
    confidence: str
    status: str
    current_version_id: UUID | None
    last_evidence_change_at: datetime | None
    created_at: datetime


class HypothesisCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    statement: str = Field(min_length=1, max_length=4096)


class HypothesisUpdateIn(BaseModel):
    statement: str = Field(min_length=1, max_length=4096)
    rationale: str = Field(default="", max_length=2048)


class EvidenceIn(BaseModel):
    stance: str = Field(pattern=r"^(supports|contradicts|contextualizes)$")
    evidence_kind: str = Field(
        pattern=r"^(claim|source_page|chunk|finding|other_hypothesis)$"
    )
    target_id: UUID
    weight: float = Field(default=1.0, ge=0, le=10.0)
    note: str = Field(default="", max_length=2048)


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    hypothesis_id: UUID
    hypothesis_version_id: UUID
    stance: str
    evidence_kind: str
    target_id: UUID
    weight: float
    note: str


@router.get("/{project_id}/hypotheses", response_model=list[HypothesisOut])
async def get_hypotheses(
    project_id: ProjectScopeDep, session: SessionDep
) -> list[HypothesisOut]:
    rows = await list_hypotheses(session, project_id=project_id)
    return [HypothesisOut.model_validate(r) for r in rows]


@router.post(
    "/{project_id}/hypotheses",
    status_code=status.HTTP_201_CREATED,
    response_model=HypothesisOut,
)
async def post_hypothesis(
    project_id: ProjectScopeDep,
    body: Annotated[HypothesisCreateIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> HypothesisOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    h = await create_hypothesis(
        session,
        ledger=ledger,
        principal=principal,
        project_id=project_id,
        title=body.title,
        statement=body.statement,
    )
    return HypothesisOut.model_validate(h)


@router.get(
    "/{project_id}/hypotheses/{hypothesis_id}",
    response_model=HypothesisOut,
)
async def get_one(
    project_id: ProjectScopeDep,
    hypothesis_id: UUID,
    session: SessionDep,
) -> HypothesisOut:
    h = await get_hypothesis(
        session, project_id=project_id, hypothesis_id=hypothesis_id
    )
    if h is None:
        msg = f"hypothesis {hypothesis_id} not found"
        raise NotFound(msg)
    return HypothesisOut.model_validate(h)


@router.patch(
    "/{project_id}/hypotheses/{hypothesis_id}",
    response_model=HypothesisOut,
)
async def patch_hypothesis(
    project_id: ProjectScopeDep,
    hypothesis_id: UUID,
    body: Annotated[HypothesisUpdateIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> HypothesisOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    h = await get_hypothesis(
        session, project_id=project_id, hypothesis_id=hypothesis_id
    )
    if h is None:
        msg = f"hypothesis {hypothesis_id} not found"
        raise NotFound(msg)
    await record_version(
        session,
        ledger=ledger,
        principal=principal,
        hypothesis=h,
        statement=body.statement,
        confidence=h.confidence,
        rationale=body.rationale,
    )
    return HypothesisOut.model_validate(h)


@router.post(
    "/{project_id}/hypotheses/{hypothesis_id}/evidence",
    status_code=status.HTTP_201_CREATED,
    response_model=EvidenceOut,
)
async def post_evidence(
    project_id: ProjectScopeDep,
    hypothesis_id: UUID,
    body: Annotated[EvidenceIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> EvidenceOut:
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    ev = await add_evidence(
        session,
        ledger=ledger,
        principal=principal,
        hypothesis_id=hypothesis_id,
        stance=body.stance,
        evidence_kind=body.evidence_kind,
        target_id=body.target_id,
        weight=body.weight,
        note=body.note,
    )
    return EvidenceOut.model_validate(ev)


class HypothesisVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    version_no: int
    statement: str
    confidence: str
    rationale: str
    author_kind: str
    author_id: UUID
    parent_version_id: UUID | None
    created_at: datetime


@router.get(
    "/{project_id}/hypotheses/{hypothesis_id}/versions",
    response_model=list[HypothesisVersionOut],
)
async def get_versions(
    project_id: ProjectScopeDep,
    hypothesis_id: UUID,
    session: SessionDep,
) -> list[HypothesisVersionOut]:
    rows = list(
        (
            await session.execute(
                select(HypothesisVersion)
                .where(
                    HypothesisVersion.project_id == project_id,
                    HypothesisVersion.hypothesis_id == hypothesis_id,
                )
                .order_by(HypothesisVersion.version_no.desc())
            )
        )
        .scalars()
        .all()
    )
    return [HypothesisVersionOut.model_validate(r) for r in rows]
