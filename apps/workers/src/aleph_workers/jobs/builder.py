"""builder_job — runs the Builder LangGraph workflow.

The API route (post_build) creates the builder AgentRun in `pending` and puts
its id in the agent token; this job drives THAT run through
running -> succeeded/failed. It must never insert a second run — the UI's
Activity card watches the dispatching run, and a twin insert also violates
uq_agent_runs_correlation_id (both rows derive the same correlation_id).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from aleph_artifacts.artifact_service import get_artifact
from aleph_artifacts.builder import BuilderWorkflow
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal


async def _finish_run(
    maker: Any,
    run_id: UUID,
    *,
    status: str,
    version_id: str | None = None,
    error_text: str | None = None,
) -> None:
    async with maker() as session:
        run = await session.get(AgentRun, run_id)
        if run is not None:
            run.status = status
            run.completed_at = utcnow()
            run.result_payload = {"version_id": version_id}
            run.error_text = error_text
        await session.commit()


async def builder_job(
    ctx: dict[str, Any],
    project_id_str: str,
    artifact_id_str: str,
    artifact_kind: str,
    template_name: str,
    csl_style: str,
    wiki_page_ids_str: list[str],
    dataset_version_ids_str: list[str],
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
    if claims.agent_run_id is None:
        msg = "builder_job requires agent_run_id in the agent token"
        raise RuntimeError(msg)
    agent_run_id = claims.agent_run_id
    project_id = UUID(project_id_str)
    artifact_id = UUID(artifact_id_str)
    wiki_page_ids = [UUID(s) for s in wiki_page_ids_str]
    dataset_version_ids = [UUID(s) for s in dataset_version_ids_str]
    maker = ctx["session_maker"]
    asset_store = ctx["asset_store"]

    async with maker() as session:
        artifact = await get_artifact(session, project_id=project_id, artifact_id=artifact_id)
        if artifact is None:
            msg = f"artifact {artifact_id} not found"
            await session.rollback()
            await _finish_run(maker, agent_run_id, status="failed", error_text=msg)
            raise RuntimeError(msg)
        run = await session.get(AgentRun, agent_run_id)
        if run is not None:
            run.status = "running"
            run.started_at = utcnow()
        await session.commit()
        # Fresh load to avoid detached-state issues across awaits.
        artifact = await get_artifact(session, project_id=project_id, artifact_id=artifact_id)

    workflow = BuilderWorkflow(session_maker=maker, asset_store=asset_store, principal=principal)
    try:
        async with maker() as session:
            ledger = LedgerWriter(session)
            version = await workflow.run(
                ledger=ledger,
                project_id=project_id,
                agent_run_id=agent_run_id,
                artifact=artifact,  # type: ignore[arg-type]
                artifact_kind=artifact_kind,
                wiki_page_ids=wiki_page_ids,
                dataset_version_ids=dataset_version_ids,
                template_name=template_name,
                csl_style=csl_style,
            )
        status = "succeeded"
        error_text = None
        version_id = str(version.id)
    except Exception as exc:
        status = "failed"
        error_text = str(exc)[:4096]
        version_id = None

    await _finish_run(
        maker, agent_run_id, status=status, version_id=version_id, error_text=error_text
    )
    return {"ok": status == "succeeded", "version_id": version_id, "error": error_text}
