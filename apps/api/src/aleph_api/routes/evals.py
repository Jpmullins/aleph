"""Eval API + UserFeedback API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.feedback_writer import record_feedback
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_evals.models import (
    EvalDataset,
    EvalRun,
)
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1", tags=["evals"])


class EvalDatasetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    description: str
    kind: str
    case_count: int
    fixture_path: str
    gate_kind: str
    introduced_in_increment: int


class EvalRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    eval_dataset_id: UUID
    model_profile_name: str
    runner_version: str
    started_at: datetime
    completed_at: datetime | None
    status: str
    pass_count: int
    fail_count: int
    metrics_jsonb: dict


class FeedbackIn(BaseModel):
    target_kind: str = Field(
        pattern=(r"^(claim|source|chart|finding|hypothesis|assistant_message|wiki_page)$")
    )
    target_id: UUID
    signal: str = Field(
        pattern=(
            r"^(thumbs_up|thumbs_down|marked_wrong|misleading|false_positive|excellent|note_only)$"
        )
    )
    note: str = Field(default="", max_length=4096)
    severity: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class FeedbackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    target_kind: str
    target_id: UUID
    signal: str
    note: str
    severity: str | None
    promoted_to_eval_case_id: UUID | None
    created_at: datetime


@router.get("/eval-datasets", response_model=list[EvalDatasetOut])
async def list_datasets(session: SessionDep) -> list[EvalDatasetOut]:
    rows = list(
        (await session.execute(select(EvalDataset).order_by(EvalDataset.name))).scalars().all()
    )
    return [EvalDatasetOut.model_validate(r) for r in rows]


@router.get("/eval-runs", response_model=list[EvalRunOut])
async def list_runs(
    session: SessionDep,
    dataset_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[EvalRunOut]:
    stmt = select(EvalRun).order_by(EvalRun.started_at.desc()).limit(limit)
    if dataset_id:
        stmt = stmt.where(EvalRun.eval_dataset_id == dataset_id)
    rows = list((await session.execute(stmt)).scalars().all())
    return [EvalRunOut.model_validate(r) for r in rows]


@router.post(
    "/projects/{project_id}/feedback",
    status_code=status.HTTP_201_CREATED,
    response_model=FeedbackOut,
)
async def post_feedback(
    project_id: ProjectScopeDep,
    body: Annotated[FeedbackIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> FeedbackOut:
    require_at_least(principal, project_id, at_least=ProjectRole.VIEWER)
    fb = await record_feedback(
        session,
        ledger,
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        target_kind=body.target_kind,
        target_id=body.target_id,
        signal=body.signal,
        note=body.note,
        severity=body.severity,
        context=body.context,
    )
    return FeedbackOut.model_validate(fb)
