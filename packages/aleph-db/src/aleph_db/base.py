"""Declarative base + the CommonColumns mixin every persistent table inherits."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aleph_core.ids import uuid7


class Base(DeclarativeBase):
    """SQLAlchemy declarative base. All Aleph tables inherit from this."""


class CommonColumns:
    """Standard columns on every persistent row.

    Inherit alongside Base. The mixin orders columns first so Alembic
    autogen keeps them at the top of the table definition.
    """

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    created_by: Mapped[UUID] = mapped_column(nullable=False)
    access_scope: Mapped[str] = mapped_column(String(64), nullable=False, server_default="project")
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
