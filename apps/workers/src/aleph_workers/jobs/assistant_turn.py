"""assistant_turn_job: run one assistant turn through the LangGraph workflow."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.models.model_profile import ModelProfile
from aleph_assistant.agent.workflow import AssistantTurnState, AssistantTurnWorkflow
from aleph_assistant.models import AssistantMessage
from aleph_assistant.thread_service import list_messages
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal


async def assistant_turn_job(
    ctx: dict[str, Any],
    project_id_str: str,
    thread_id_str: str,
    user_message_id_str: str,
    assistant_message_id_str: str,
    agent_token: str,
) -> dict[str, Any]:
    secret: str = ctx["agent_token_secret"]
    claims = verify_agent_token(agent_token, secret=secret)
    principal = Principal(
        user_id=claims.user_id,
        subject="agent",
        email="",
        actor_kind=claims.actor_kind,
        agent_run_id=claims.agent_run_id,
        correlation_id=claims.correlation_id,
    )

    project_id = UUID(project_id_str)
    thread_id = UUID(thread_id_str)
    user_message_id = UUID(user_message_id_str)
    assistant_message_id = UUID(assistant_message_id_str)

    maker = ctx["session_maker"]
    litellm = ctx["litellm_client"]

    async with maker() as session:
        profile = (
            await session.execute(
                select(ModelProfile).where(ModelProfile.project_id == project_id)
            )
        ).scalar_one_or_none()
        if profile is None:
            msg = f"no profile for project {project_id}"
            raise RuntimeError(msg)
        user_msg = await session.get(AssistantMessage, user_message_id)
        if user_msg is None:
            msg = f"user message {user_message_id} not found"
            raise RuntimeError(msg)
        prior = await list_messages(session, thread_id=thread_id, limit=200)
        # Exclude the just-posted user message and the in-progress assistant msg.
        prior = [m for m in prior if m.id not in (user_message_id, assistant_message_id)]

        # Mark AgentRun running.
        run = await session.get(AgentRun, claims.agent_run_id)
        if run is not None:
            run.status = "running"
            run.started_at = utcnow()
            await session.commit()

    workflow = AssistantTurnWorkflow(
        session_maker=maker, litellm=litellm, principal=principal, profile=profile
    )
    state: AssistantTurnState = {
        "agent_run_id": claims.agent_run_id,
        "project_id": project_id,
        "thread_id": thread_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "user_query": user_msg.body_md,
        "prior_messages": prior,
        "profile_bindings": profile.bindings_jsonb,
    }
    error_text: str | None = None
    try:
        await workflow.run(state)
        run_status = "succeeded"
    except Exception as exc:  # noqa: BLE001
        error_text = str(exc)[:4096]
        run_status = "failed"

    async with maker() as session:
        run = await session.get(AgentRun, claims.agent_run_id)
        if run is not None:
            run.status = run_status
            run.completed_at = utcnow()
            run.error_text = error_text
        await session.commit()
    return {"ok": run_status == "succeeded", "error": error_text}
