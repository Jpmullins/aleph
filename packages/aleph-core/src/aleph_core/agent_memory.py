"""AgentMemory model — per-project, per-agent structured scratchpad.

Distinct from `AssistantThread`/`AssistantMessage` (user-facing
conversation history) and from `AgentEvent` (progress on an
`AgentRun`).  Lookups by (project_id, agent_kind, namespace, key).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# We avoid importing aleph_db here so aleph_core stays leaf-level. The
# model is registered into aleph_db.Base by importing this module from
# the alembic env / the workers / api startup.

# Import is intentional and lazy.
from aleph_db.base import Base, CommonColumns


class AgentMemory(CommonColumns, Base):
    __tablename__ = "agent_memories"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "agent_kind",
            "namespace",
            "key",
            name="uq_agent_memory_lookup",
        ),
        Index(
            "ix_agent_memory_namespace",
            "project_id",
            "agent_kind",
            "namespace",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    agent_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
