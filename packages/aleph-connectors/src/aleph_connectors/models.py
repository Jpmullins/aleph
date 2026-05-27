"""SynthesisProposal + ApprovalDecision models.

Both are introduced in Inc 3 and reused by Inc 5's full reviewer +
approval workflow. ApprovalDecision is generic enough to record any
approve/reject across target kinds.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class SynthesisProposal(CommonColumns, Base):
    __tablename__ = "synthesis_proposals"
    __table_args__ = (
        UniqueConstraint("page_id", "revision_id", name="uq_synth_page_rev"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    page_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    revision_id: Mapped[UUID] = mapped_column(nullable=False)
    agent_run_id: Mapped[UUID] = mapped_column(nullable=False)
    topic: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    approval_decision_id: Mapped[UUID | None] = mapped_column(nullable=True)


class ApprovalDecision(CommonColumns, Base):
    __tablename__ = "approval_decisions"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    decided_by: Mapped[UUID] = mapped_column(nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
