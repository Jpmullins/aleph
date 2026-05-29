"""AIQ tokenomics → CostLedger adapter.

AIQ reports per-phase token + cost telemetry. The adapter writes one
`ModelCall` + `CostLedgerEvent` per LLM call so the cost ledger is the
single source of truth for project spend, including AIQ-driven calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class PhaseStat:
    phase: str
    model: str
    capability: str
    input_tokens: int
    cached_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    cache_savings_usd: Decimal
    latency_ms: int
    timestamp: datetime
    trace_id: str | None


async def record_aiq_phase_stats(
    session: AsyncSession,
    *,
    project_id: UUID,
    agent_run_id: UUID,
    stats: list[PhaseStat],
) -> int:
    """Insert ModelCall + CostLedgerEvent rows for each AIQ phase stat."""
    from aleph_db.repos.cost import CostWriter

    if not stats:
        return 0
    writer = CostWriter(session)
    for s in stats:
        await writer.record_call(
            project_id=project_id,
            agent_run_id=agent_run_id,
            capability=s.capability,
            model=s.model,
            purpose=f"aiq.{s.phase}",
            input_tokens=s.input_tokens,
            cached_tokens=s.cached_tokens,
            completion_tokens=s.completion_tokens,
            cost_usd=s.cost_usd,
            cache_savings_usd=s.cache_savings_usd,
            latency_ms=s.latency_ms,
            trace_id=s.trace_id,
            timestamp=s.timestamp,
        )
    return len(stats)
