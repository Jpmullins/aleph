"""AgentRun + AgentEvent."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class AgentRun(CommonColumns, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("correlation_id", name="uq_agent_runs_correlation_id"),)

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    agent_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    result_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_text: Mapped[str | None] = mapped_column(String(4096), nullable=True)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    agent_run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
