"""Smoke endpoint: round-trip a hardcoded "ping" prompt through the gateway.

CI's acceptance path: POST /v1/projects/{id}/smoke/llm returns the response,
model, tokens, cost; verifies ModelCall + CostLedgerEvent + Langfuse trace.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body
from pydantic import BaseModel

from aleph_api.deps import LiteLLMDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import NotFound
from aleph_core.schemas.model_profile import Capability
from aleph_db.repos import model_profile as profile_repo
from aleph_models.client import ChatMessage
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1/projects", tags=["smoke"])


class SmokeIn(BaseModel):
    prompt: str = "ping"


class SmokeOut(BaseModel):
    model: str
    content: str
    cost_usd: str
    model_call_id: str
    trace_id: str | None
    latency_ms: int


@router.post("/{project_id}/smoke/llm", response_model=SmokeOut)
async def smoke_llm(
    project_id: ProjectScopeDep,
    body: Annotated[SmokeIn, Body()],
    session: SessionDep,
    litellm: LiteLLMDep,
    principal: PrincipalDep,
) -> SmokeOut:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)

    profile = await profile_repo.get_project_profile(session, project_id)
    if profile is None:
        msg = f"project {project_id} has no profile"
        raise NotFound(msg)
    resp = await litellm.chat(
        principal=principal,
        project_id=project_id,
        agent_run_id=None,
        capability=Capability.CLASSIFICATION,
        profile_bindings=profile.bindings_jsonb,
        messages=[ChatMessage(role="user", content=body.prompt)],
        max_tokens=32,
        temperature=0.0,
        purpose="inc0.smoke",
    )
    content = resp.choices[0].message.content if resp.choices else ""
    return SmokeOut(
        model=resp.model,
        content=content,
        cost_usd=resp.cost_usd,
        model_call_id=resp.model_call_id,
        trace_id=resp.trace_id,
        latency_ms=resp.latency_ms,
    )
