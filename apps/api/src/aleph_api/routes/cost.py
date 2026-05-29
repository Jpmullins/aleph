"""GET /v1/projects/{id}/cost — rollup of cost ledger + budget."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter

from aleph_api.deps import SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import NotFound
from aleph_core.schemas.cost import CostBucket, CostRollup, ModelCallRecord
from aleph_db.repos import cost as cost_repo

router = APIRouter(prefix="/v1/projects", tags=["cost"])


@router.get("/{project_id}/cost", response_model=CostRollup)
async def get_cost(project_id: ProjectScopeDep, session: SessionDep) -> CostRollup:
    budget = await cost_repo.get_budget(session, project_id)
    if budget is None:
        msg = f"project {project_id} has no budget"
        raise NotFound(msg)
    by_phase = [
        CostBucket(key=k, cost_usd=c, call_count=n)
        for k, c, n in await cost_repo.cost_by_phase(session, project_id)
    ]
    by_model = [
        CostBucket(key=k, cost_usd=c, call_count=n)
        for k, c, n in await cost_repo.cost_by_model(session, project_id)
    ]
    recent = await cost_repo.recent_calls(session, project_id)
    return CostRollup(
        cap_usd=Decimal(budget.cap_usd),
        spent_usd=Decimal(budget.spent_usd),
        soft_pct=Decimal(budget.soft_pct),
        hard_pct=Decimal(budget.hard_pct),
        by_phase=by_phase,
        by_model=by_model,
        recent_calls=[ModelCallRecord.model_validate(c) for c in recent],
    )
