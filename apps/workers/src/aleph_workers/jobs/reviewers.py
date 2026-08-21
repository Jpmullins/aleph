"""mechanical_review_job + editorial_review_job."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.models.model_profile import ModelProfile
from aleph_reviewer.editorial import EditorialReviewerWorkflow
from aleph_reviewer.mechanical import MechanicalReviewerWorkflow
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal


async def mechanical_review_job(
    ctx: dict[str, Any],
    project_id_str: str,
    revision_id_str: str,
    page_id_str: str,
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
    revision_id = UUID(revision_id_str)
    page_id = UUID(page_id_str)
    maker = ctx["session_maker"]

    # If wiki_ingest pre-created a pending AgentRun for this review,
    # promote it to running rather than inserting a duplicate (the
    # correlation_id is unique-constrained).
    async with maker() as session:
        existing = (
            await session.execute(select(AgentRun).where(AgentRun.id == claims.agent_run_id))
        ).scalar_one_or_none()
        if existing is not None:
            existing.status = "running"
            existing.started_at = utcnow()
            agent_run_id = existing.id
        else:
            run = AgentRun(
                id=uuid7(),
                project_id=project_id,
                agent_kind="mechanical_reviewer",
                correlation_id=f"mech-{uuid7().hex}",
                status="running",
                started_at=utcnow(),
                input_payload={
                    "revision_id": str(revision_id),
                    "page_id": str(page_id),
                },
                created_by=principal.user_id,
            )
            session.add(run)
            await session.flush()
            agent_run_id = run.id
        await session.commit()

    workflow = MechanicalReviewerWorkflow(
        session_maker=maker,
        principal=principal,
        scholar=ctx.get("scholar"),
    )
    n = await workflow.run(
        project_id=project_id,
        revision_id=revision_id,
        page_id=page_id,
        agent_run_id=agent_run_id,
    )

    async with maker() as session:
        run = await session.get(AgentRun, agent_run_id)
        if run is not None:
            run.status = "succeeded"
            run.completed_at = utcnow()
            run.result_payload = {"finding_count": n}
        await session.commit()
    return {"ok": True, "finding_count": n}


async def editorial_review_job(
    ctx: dict[str, Any],
    project_id_str: str,
    trigger: str,
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
    maker = ctx["session_maker"]
    litellm = ctx["litellm_client"]

    async with maker() as session:
        profile = (
            await session.execute(select(ModelProfile).where(ModelProfile.project_id == project_id))
        ).scalar_one_or_none()
        if profile is None:
            msg = f"no profile for project {project_id}"
            raise RuntimeError(msg)
        run = AgentRun(
            id=uuid7(),
            project_id=project_id,
            agent_kind="editorial_reviewer",
            correlation_id=claims.correlation_id or f"editor-{uuid7().hex}",
            status="running",
            started_at=utcnow(),
            input_payload={"trigger": trigger},
            created_by=principal.user_id,
        )
        session.add(run)
        await session.commit()
        agent_run_id = run.id

    workflow = EditorialReviewerWorkflow(
        session_maker=maker,
        litellm=litellm,
        principal=principal,
        profile=profile,
    )
    n = await workflow.run(
        project_id=project_id,
        agent_run_id=agent_run_id,
        trigger=trigger,
    )

    async with maker() as session:
        run = await session.get(AgentRun, agent_run_id)
        if run is not None:
            run.status = "succeeded"
            run.completed_at = utcnow()
            run.result_payload = {"finding_count": n}
        await session.commit()
    return {"ok": True, "finding_count": n}
