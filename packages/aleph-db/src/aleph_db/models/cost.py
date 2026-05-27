"""ModelCall, CostLedgerEvent, Budget."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    agent_run_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    capability: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    input_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    cached_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0"
    )
    cache_savings_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0"
    )
    latency_ms: Mapped[int] = mapped_column(nullable=False, server_default="0")
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )


class CostLedgerEvent(Base):
    __tablename__ = "cost_ledger_events"
    __table_args__ = (
        UniqueConstraint("model_call_id", name="uq_cost_ledger_model_call"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    agent_run_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    model_call_id: Mapped[UUID] = mapped_column(nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )


class Budget(CommonColumns, Base):
    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_budgets_project_id"),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    cap_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    soft_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default="80"
    )
    hard_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default="100"
    )
    spent_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default="0"
    )
