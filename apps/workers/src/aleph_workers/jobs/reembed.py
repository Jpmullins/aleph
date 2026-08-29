"""reembed_job: re-embed a project's stale chunks after an embedder-model change.

Enqueued by the model-profile switch route when the new profile's `embedding`
binding differs from the old one. Idempotent + bounded (only sources whose
`RetrievalIndexRecord.embedder_model` is stale are touched). Re-embedding goes
through `LiteLLMClient.embed`, so it writes `ModelCall` + `CostLedgerEvent`; on
success it writes one `embeddings.reembedded` ledger event for the project.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from aleph_db.models.model_profile import ModelProfile
from aleph_db.models.project import Project
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability.tracing import current_trace_id, start_span
from aleph_rks.retrieval import reembed_for_project
from aleph_security.principal import Principal
from aleph_workers.gateway import gateways
from aleph_workers.project_guard import refuse_if_project_is_gone

_log = structlog.get_logger(__name__)


async def reembed_job(ctx: dict[str, Any], project_id_str: str) -> dict[str, Any]:
    maker = ctx["session_maker"]
    pid = UUID(project_id_str)
    # Before anything that costs money. See `project_guard`: a deleted
    # project's queued work kept running AND kept enqueueing more.
    refusal = await refuse_if_project_is_gone(maker, pid)
    if refusal is not None:
        return refusal
    litellm = await gateways(ctx).litellm(pid)
    with start_span("worker.reembed", **{"aleph.project_id": project_id_str}):
        async with maker() as session:
            project = (
                await session.execute(select(Project).where(Project.id == pid))
            ).scalar_one_or_none()
            profile = (
                await session.execute(select(ModelProfile).where(ModelProfile.project_id == pid))
            ).scalar_one_or_none()
            if project is None or profile is None:
                return {"sources_reembedded": 0, "chunks_reembedded": 0}
            owner = project.created_by
            principal = Principal(
                user_id=owner, subject="agent", email="", actor_kind="aleph_agent"
            )
            sources, chunks = await reembed_for_project(
                session,
                project_id=pid,
                client=litellm,
                principal=principal,
                profile_bindings=profile.bindings_jsonb,
                purpose="rks.reembed",
            )
            if chunks:
                await LedgerWriter(session).append(
                    project_id=pid,
                    actor_id=owner,
                    actor_kind="aleph_agent",
                    action_kind="embeddings.reembedded",
                    target_id=None,
                    target_kind="document_chunks",
                    payload={"sources": sources, "chunks": chunks},
                    trace_id=current_trace_id(),
                )
            await session.commit()
    _log.info("worker.reembed.done", project_id=project_id_str, sources=sources, chunks=chunks)
    return {"sources_reembedded": sources, "chunks_reembedded": chunks}
