"""Hypothesis + HypothesisVersion + HypothesisEvidence."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class Hypothesis(CommonColumns, Base):
    __tablename__ = "hypotheses"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    short_id: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="under_investigation"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    last_evidence_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class HypothesisVersion(Base):
    __tablename__ = "hypothesis_versions"
    __table_args__ = (
        UniqueConstraint("hypothesis_id", "version_no", name="uq_hypothesis_version_no"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    hypothesis_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(String(24), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_id: Mapped[UUID] = mapped_column(nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)


class HypothesisEvidence(CommonColumns, Base):
    __tablename__ = "hypothesis_evidence"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    hypothesis_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    hypothesis_version_id: Mapped[UUID] = mapped_column(nullable=False)
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
