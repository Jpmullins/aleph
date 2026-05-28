"""Eval API + UserFeedback API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aleph_core.errors import NotFound
from aleph_core.feedback import UserFeedback
from aleph_core.ids import uuid7
from aleph_evals.models import (
    EvalCase,
    EvalDataset,
    EvalResult,
    EvalRun,
)
from aleph_observability.tracing import current_trace_id
from aleph_security.roles import ProjectRole, require_at_least

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep

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
        pattern=(
            r"^(claim|source|chart|finding|hypothesis|assistant_message|wiki_page)$"
        )
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
        (await session.execute(select(EvalDataset).order_by(EvalDataset.name)))
        .scalars()
        .all()
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
    fb = UserFeedback(
        id=uuid7(),
        project_id=project_id,
        target_kind=body.target_kind,
        target_id=body.target_id,
        signal=body.signal,
        note=body.note,
        severity=body.severity,
        context_jsonb=body.context,
        promoted_to_eval_case_id=None,
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(fb)
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind=f"feedback.{body.signal}",
        target_id=body.target_id,
        target_kind=body.target_kind,
        payload={
            "severity": body.severity,
            "note_length": len(body.note),
        },
        trace_id=current_trace_id(),
    )

    # Promote high-signal feedback to an EvalCase under a per-project
    # "user_feedback" EvalDataset (lazily created).
    if body.signal in ("marked_wrong", "misleading", "false_positive"):
        await _promote_to_eval_case(session, fb)

    return FeedbackOut.model_validate(fb)


async def _promote_to_eval_case(session, fb: UserFeedback) -> None:
    dataset_name = f"user_feedback:{fb.project_id}"
    existing = (
        await session.execute(
            select(EvalDataset).where(EvalDataset.name == dataset_name)
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = EvalDataset(
            id=uuid7(),
            name=dataset_name,
            description=f"User feedback promoted to regression cases for {fb.project_id}",
            kind="metric_only",
            case_count=0,
            fixture_path=f"<runtime:user_feedback:{fb.project_id}>",
            gate_kind="warning",
            gate_thresholds_jsonb={},
            introduced_in_increment=8,
            created_by=fb.created_by,
            access_scope="project",
        )
        session.add(existing)
        await session.flush()
    case = EvalCase(
        id=uuid7(),
        eval_dataset_id=existing.id,
        case_key=f"feedback-{fb.id}",
        payload_jsonb={
            "target_kind": fb.target_kind,
            "target_id": str(fb.target_id),
            "context": fb.context_jsonb,
        },
        expected_jsonb={
            "signal_not_in": ["marked_wrong", "misleading", "false_positive"],
            "note": fb.note,
        },
        tags_jsonb=["user_feedback", fb.signal],
        origin="user_feedback",
        origin_ref_id=fb.id,
    )
    session.add(case)
    existing.case_count += 1
    fb.promoted_to_eval_case_id = case.id
    await session.flush()
