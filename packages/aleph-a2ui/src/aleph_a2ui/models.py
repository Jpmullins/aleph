"""InteractiveCard / InteractiveCardVersion / CardAction.

Spec §4.4. InteractiveCardVersion is immutable — same trigger pattern
as WikiRevision (installed in the inc4_a2ui migration).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class InteractiveCard(CommonColumns, Base):
    __tablename__ = "interactive_cards"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    card_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    pinned_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pinned_target_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # WP-4d: an agent `spotlight` action flips this bit; the Briefs builder
    # orders spotlighted cards first and flags them in props. Survives rebuilds.
    spotlighted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class InteractiveCardVersion(Base):
    __tablename__ = "interactive_card_versions"
    __table_args__ = (UniqueConstraint("card_id", "version_no", name="uq_card_version_no"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    card_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    a2ui_payload_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    data_model_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    parent_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_id: Mapped[UUID] = mapped_column(nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)


class CardAction(Base):
    __tablename__ = "card_actions"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    card_id: Mapped[UUID | None] = mapped_column(nullable=True)
    surface_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(nullable=True)
    target_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    result_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    actor_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    ledger_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
