"""backfill_index_job: index every normalized document that has no chunks.

The repair for an index that was never built. It exists because the failure it
repairs is not hypothetical — 45 normalized documents sat unindexed on the
deployed stack for as long as the embedder name was wrong, and there was no way
to ask for them to be indexed again short of re-ingesting the sources.

Idempotent by construction: it selects documents with **no chunk rows at all**,
so a second run over the same project returns ``(0, 0)``. Repairing the *dense*
leg of an already-chunked source is `reembed_job`'s job, and keeping the two
separate is what makes each safe to re-run.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.models.model_profile import ModelProfile
from aleph_db.models.project import Project
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability.tracing import current_trace_id, start_span
from aleph_rks.backfill import backfill_unindexed_for_project
from aleph_security.principal import Principal
from aleph_workers.gateway import gateways

_log = structlog.get_logger(__name__)


async def _finalize(
    maker: Any,
    run_id: UUID,
    status: str,
    result_payload: dict[str, Any] | None = None,
    *,
    error_text: str | None = None,
) -> None:
    """Every exit path reports. A run left `running` is what the reaper exists
    to clean up, and needing the reaper is a bug, not a design."""
    from sqlalchemy import select as _select

    async with maker() as session:
        run = (
            await session.execute(_select(AgentRun).where(AgentRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            return
        run.status = status
        run.completed_at = utcnow()
        if result_payload is not None:
            run.result_payload = result_payload
        if error_text is not None:
            run.error_text = error_text[:4096]
        await session.commit()


async def backfill_index_job(ctx: dict[str, Any], project_id_str: str) -> dict[str, Any]:
    maker = ctx["session_maker"]
    asset_store = ctx["asset_store"]
    pid = UUID(project_id_str)
    litellm = await gateways(ctx).litellm(pid)

    with start_span("worker.backfill_index", **{"aleph.project_id": project_id_str}):
        async with maker() as session:
            project = (
                await session.execute(select(Project).where(Project.id == pid))
            ).scalar_one_or_none()
            profile = (
                await session.execute(select(ModelProfile).where(ModelProfile.project_id == pid))
            ).scalar_one_or_none()
            if project is None or profile is None:
                return {"documents": 0, "chunks": 0, "reason": "no project or no model profile"}
            owner = project.created_by
            bindings = dict(profile.bindings_jsonb)

        # Mint a run FIRST, and embed under it. A repair pass spends real money
        # — 79 priced embed calls on the first live backfill — and every one of
        # them was written with `agent_run_id = NULL`, so the spend existed and
        # belonged to nothing. `select count(*) from model_calls where
        # agent_run_id is null` is one of the eight numbers that define done;
        # a job that adds to it while repairing something else is not a repair.
        run_id = uuid7()
        async with maker() as session:
            session.add(
                AgentRun(
                    id=run_id,
                    project_id=pid,
                    agent_kind="backfill_index",
                    correlation_id=f"backfill-{run_id.hex}",
                    status="running",
                    started_at=utcnow(),
                    input_payload={"project_id": project_id_str},
                    created_by=owner,
                )
            )
            await session.commit()

        principal = Principal(
            user_id=owner,
            subject="agent",
            email="",
            actor_kind="aleph_agent",
            agent_run_id=run_id,
        )
        try:
            documents, chunks = await backfill_unindexed_for_project(
                maker=maker,
                project_id=pid,
                asset_store=asset_store,
                litellm=litellm,
                principal=principal,
                profile_bindings=bindings,
                agent_run_id=run_id,
            )
        except Exception as exc:
            await _finalize(maker, run_id, "failed", error_text=f"{type(exc).__name__}: {exc}")
            raise

        if documents:
            async with maker() as session:
                await LedgerWriter(session).append(
                    project_id=pid,
                    actor_id=owner,
                    actor_kind="aleph_agent",
                    action_kind="index.backfilled",
                    target_id=None,
                    target_kind="document_chunks",
                    payload={"documents": documents, "chunks": chunks},
                    trace_id=current_trace_id(),
                )
                await session.commit()

    await _finalize(maker, run_id, "succeeded", {"documents": documents, "chunks": chunks})
    _log.info(
        "worker.backfill_index.done",
        project_id=project_id_str,
        documents=documents,
        chunks=chunks,
    )
    return {"documents": documents, "chunks": chunks}
