"""ReviewRun + ReviewFinding + ApprovalRequest models."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class ReviewRun(CommonColumns, Base):
    __tablename__ = "review_runs"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    target_revision_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    target_scope: Mapped[str] = mapped_column(String(16), nullable=False, server_default="revision")
    agent_run_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewFinding(CommonColumns, Base):
    __tablename__ = "review_findings"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    review_run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    finding_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    target_page_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    target_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    target_section_anchor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    target_claim_id: Mapped[UUID | None] = mapped_column(nullable=True)
    target_source_id: Mapped[UUID | None] = mapped_column(nullable=True)
    evidence_refs_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    proposed_patch_jsonb: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    auto_resolvable: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    approval_request_id: Mapped[UUID | None] = mapped_column(nullable=True)


class ApprovalRequest(CommonColumns, Base):
    __tablename__ = "approval_requests"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="medium")
    proposed_patch_jsonb: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    evidence_refs_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    requested_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_id: Mapped[UUID | None] = mapped_column(nullable=True)
