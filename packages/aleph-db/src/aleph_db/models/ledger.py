"""ActionLedgerEvent + LedgerChainHead.

The chain-head row anchors the hash chain per project (with a single
global head row for null-project events such as user.create). Inserts to
`action_ledger_events` go through the LedgerWriter, which takes a
SELECT FOR UPDATE lock on the head row to serialize chain extension
and avoid hash-race conditions.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base


class ActionLedgerEvent(Base):
    """Append-only hash-chained audit log. No updates, no deletes.

    The Postgres triggers `ledger_no_update` and `ledger_no_delete`
    (installed in the initial migration) enforce immutability at the
    storage layer as a second line of defense after the service-layer
    invariants.
    """

    __tablename__ = "action_ledger_events"
    __table_args__ = (
        Index("ix_ledger_project_time", "project_id", "timestamp"),
        Index("ix_ledger_action_kind", "action_kind"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    actor_id: Mapped[UUID] = mapped_column(nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(nullable=True)
    target_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    prev_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
    chain_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class LedgerChainHead(Base):
    """One row per project_id (plus a single null-project row for global ops).

    Updated within the same transaction as each event insert; the
    SELECT FOR UPDATE on the matching row serializes chain extension.
    """

    __tablename__ = "ledger_chain_heads"

    # NOTE: Postgres treats NULL as not equal to NULL in unique constraints,
    # so the null-project row is enforced by a partial unique index added in
    # the migration. The primary key is a synthetic uuid; (project_id) is
    # logically unique with the partial index handling NULLs.
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID | None] = mapped_column(nullable=True, unique=True)
    head_event_id: Mapped[UUID | None] = mapped_column(nullable=True)
    head_chain_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="0" * 64
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
