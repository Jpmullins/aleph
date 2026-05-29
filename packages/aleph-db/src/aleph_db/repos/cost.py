"""Cost ledger writer + rollup query helpers."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import desc, func, select

from aleph_core.ids import uuid7
from aleph_db.models.cost import Budget, CostLedgerEvent, ModelCall

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class CostWriter:
    """Inserts ModelCall + CostLedgerEvent in one transaction.

    Trigger `cost_to_budget` (initial migration) rolls the cost into
    the project's `budgets.spent_usd`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_call(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID | None,
        capability: str,
        model: str,
        purpose: str,
        input_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
        cost_usd: Decimal,
        cache_savings_usd: Decimal,
        latency_ms: int,
        trace_id: str | None,
        timestamp: datetime,
    ) -> ModelCall:
        call_id = uuid7()
        call = ModelCall(
            id=call_id,
            project_id=project_id,
            agent_run_id=agent_run_id,
            capability=capability,
            model=model,
            purpose=purpose,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            cache_savings_usd=cache_savings_usd,
            latency_ms=latency_ms,
            trace_id=trace_id,
            timestamp=timestamp,
        )
        self._session.add(call)

        event = CostLedgerEvent(
            id=uuid7(),
            project_id=project_id,
            agent_run_id=agent_run_id,
            model_call_id=call_id,
            cost_usd=cost_usd,
            timestamp=timestamp,
        )
        self._session.add(event)

        await self._session.flush()
        return call


async def get_budget(session: AsyncSession, project_id: UUID) -> Budget | None:
    return (
        await session.execute(select(Budget).where(Budget.project_id == project_id))
    ).scalar_one_or_none()


async def cost_by_phase(session: AsyncSession, project_id: UUID) -> list[tuple[str, Decimal, int]]:
    stmt = (
        select(
            ModelCall.purpose,
            func.coalesce(func.sum(ModelCall.cost_usd), 0).label("cost"),
            func.count(ModelCall.id).label("calls"),
        )
        .where(ModelCall.project_id == project_id)
        .group_by(ModelCall.purpose)
        .order_by(desc("cost"))
    )
    rows = (await session.execute(stmt)).all()
    return [(purpose, Decimal(cost), int(calls)) for purpose, cost, calls in rows]


async def cost_by_model(session: AsyncSession, project_id: UUID) -> list[tuple[str, Decimal, int]]:
    stmt = (
        select(
            ModelCall.model,
            func.coalesce(func.sum(ModelCall.cost_usd), 0).label("cost"),
            func.count(ModelCall.id).label("calls"),
        )
        .where(ModelCall.project_id == project_id)
        .group_by(ModelCall.model)
        .order_by(desc("cost"))
    )
    rows = (await session.execute(stmt)).all()
    return [(model, Decimal(cost), int(calls)) for model, cost, calls in rows]


async def recent_calls(
    session: AsyncSession, project_id: UUID, *, limit: int = 25
) -> list[ModelCall]:
    stmt = (
        select(ModelCall)
        .where(ModelCall.project_id == project_id)
        .order_by(ModelCall.timestamp.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
