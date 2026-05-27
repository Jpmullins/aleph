"""Ledger event output schema."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

ActorKindStr = Literal["user", "aleph_agent", "aiq_agent", "system"]


class LedgerEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID | None
    actor_id: UUID
    actor_kind: ActorKindStr
    action_kind: str
    target_id: UUID | None
    target_kind: str | None
    payload_jsonb: dict
    trace_id: str | None
    timestamp: datetime
    prev_event_id: UUID | None
    chain_hash: str
