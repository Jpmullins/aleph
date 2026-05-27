"""Cost rollup schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelCallRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    capability: str
    model: str
    purpose: str
    input_tokens: int
    cached_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    cache_savings_usd: Decimal
    latency_ms: int
    timestamp: datetime
    trace_id: str | None


class CostBucket(BaseModel):
    key: str
    cost_usd: Decimal
    call_count: int


class CostRollup(BaseModel):
    """Returned by GET /v1/projects/{id}/cost."""

    cap_usd: Decimal
    spent_usd: Decimal
    soft_pct: Decimal
    hard_pct: Decimal
    by_phase: list[CostBucket]
    by_model: list[CostBucket]
    recent_calls: list[ModelCallRecord]
