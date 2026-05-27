"""Background smoke job — round-trips a prompt through the gateway.

Inc 0 demonstrates the worker → LiteLLM gateway → ModelCall + CostLedger
loop using the same client the API uses. No Aleph agent exists yet;
the agent_token is verified directly and used to construct a Principal.
"""

from __future__ import annotations

from typing import Any

from aleph_core.schemas.model_profile import Capability
from aleph_db.repos import cost as cost_repo  # noqa: F401  (re-exported for IDE)
from aleph_db.repos import model_profile as profile_repo
from aleph_models.client import ChatMessage
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal


async def smoke_llm_job(
    ctx: dict[str, Any],
    project_id_str: str,
    agent_token: str,
    prompt: str = "ping",
) -> dict[str, Any]:
    secret: str = ctx["agent_token_secret"]
    claims = verify_agent_token(agent_token, secret=secret)

    from uuid import UUID

    project_id = UUID(project_id_str)
    if claims.project_id != project_id:
        msg = f"agent token project_id mismatch: {claims.project_id} != {project_id}"
        raise ValueError(msg)

    principal = Principal(
        user_id=claims.user_id,
        subject="agent",
        email="",
        actor_kind=claims.actor_kind,
        agent_run_id=claims.agent_run_id,
        correlation_id=claims.correlation_id,
    )

    litellm = ctx["litellm_client"]
    maker = ctx["session_maker"]

    async with maker() as session:
        profile = await profile_repo.get_project_profile(session, project_id)
    if profile is None:
        msg = f"no profile for project {project_id}"
        raise RuntimeError(msg)

    resp = await litellm.chat(
        principal=principal,
        project_id=project_id,
        agent_run_id=claims.agent_run_id,
        capability=Capability.CLASSIFICATION,
        profile_bindings=profile.bindings_jsonb,
        messages=[ChatMessage(role="user", content=prompt)],
        max_tokens=32,
        temperature=0.0,
        purpose="inc0.smoke.worker",
    )
    return {
        "ok": True,
        "model": resp.model,
        "content": resp.choices[0].message.content if resp.choices else "",
        "cost_usd": resp.cost_usd,
        "model_call_id": resp.model_call_id,
        "trace_id": resp.trace_id,
    }
