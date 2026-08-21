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

from aleph_db.models.model_profile import ModelProfile
from aleph_db.models.project import Project
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability.tracing import current_trace_id, start_span
from aleph_rks.backfill import backfill_unindexed_for_project
from aleph_security.principal import Principal

_log = structlog.get_logger(__name__)


async def backfill_index_job(ctx: dict[str, Any], project_id_str: str) -> dict[str, Any]:
    maker = ctx["session_maker"]
    litellm = ctx["litellm_client"]
    asset_store = ctx["asset_store"]
    pid = UUID(project_id_str)

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

        principal = Principal(user_id=owner, subject="agent", email="", actor_kind="aleph_agent")
        documents, chunks = await backfill_unindexed_for_project(
            maker=maker,
            project_id=pid,
            asset_store=asset_store,
            litellm=litellm,
            principal=principal,
            profile_bindings=bindings,
        )

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

    _log.info(
        "worker.backfill_index.done",
        project_id=project_id_str,
        documents=documents,
        chunks=chunks,
    )
    return {"documents": documents, "chunks": chunks}
