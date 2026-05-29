"""EvalDataset / EvalCase / EvalRun / EvalResult models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class EvalDataset(CommonColumns, Base):
    __tablename__ = "eval_datasets"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    fixture_path: Mapped[str] = mapped_column(String(512), nullable=False)
    gate_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    gate_thresholds_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    introduced_in_increment: Mapped[int] = mapped_column(Integer, nullable=False)


class EvalCase(Base):
    __tablename__ = "eval_cases"
    __table_args__ = (
        UniqueConstraint("eval_dataset_id", "case_key", name="uq_eval_cases_dataset_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    eval_dataset_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    case_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expected_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tags_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    origin: Mapped[str] = mapped_column(String(32), nullable=False, server_default="fixture")
    origin_ref_id: Mapped[UUID | None] = mapped_column(nullable=True)


class EvalRun(CommonColumns, Base):
    __tablename__ = "eval_runs"

    eval_dataset_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    model_profile_name: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True)
    runner_version: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    pass_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    metrics_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, server_default="0")
    report_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class EvalResult(Base):
    __tablename__ = "eval_results"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    eval_run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    eval_case_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    diff_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
