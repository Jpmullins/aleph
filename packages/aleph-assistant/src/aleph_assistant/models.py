"""AssistantSession / AssistantThread / AssistantMessage models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class AssistantSession(CommonColumns, Base):
    __tablename__ = "assistant_sessions"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class AssistantThread(CommonColumns, Base):
    __tablename__ = "assistant_threads"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    session_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    parent_thread_id: Mapped[UUID | None] = mapped_column(nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AssistantMessage(CommonColumns, Base):
    __tablename__ = "assistant_messages"
    __table_args__ = (
        UniqueConstraint("thread_id", "ordinal", name="uq_messages_thread_ord"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    thread_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="complete"
    )
    retrieval_jsonb: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    attached_cards_jsonb: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0"
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_text: Mapped[str | None] = mapped_column(String(4096), nullable=True)
