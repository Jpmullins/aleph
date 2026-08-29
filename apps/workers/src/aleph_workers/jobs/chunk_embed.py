"""chunk_embed_job: NormalizedDocument → DocumentChunk rows.

The work itself lives in :func:`aleph_rks.indexing.index_normalized_document`,
which writes the chunks *before* it embeds them so a dead embedder degrades to
keyword-only search instead of no search at all. This job is the lifecycle
around it: an ``AgentRun`` the UI can watch, and a ledger event.

Two rules this file used to break, both of which produced the same live outage
(75 sources, 45 normalized documents, 0 chunks, 45 runs stuck in ``running``):

* **A degraded capability is not a silent one.** When the dense leg cannot be
  built the run finishes ``failed`` with the reason in ``error_text``, even
  though the source is searchable lexically. Reporting ``succeeded`` there is
  how a total retrieval outage went unnoticed for seven work packages.
* **A run that stops is a run that reports.** Every exit path — including the
  unexpected ones — finalizes the row. Previously an exception left it at
  ``running`` forever.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.models.model_profile import ModelProfile
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability.tracing import current_trace_id, start_span
from aleph_rks.indexing import index_normalized_document
from aleph_rks.models import NormalizedDocument, Source
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal
from aleph_workers.gateway import gateways
from aleph_workers.project_guard import refuse_if_project_is_gone


async def chunk_embed_job(
    ctx: dict[str, Any], normalized_id_str: str, agent_token: str
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
    normalized_id = UUID(normalized_id_str)

    maker = ctx["session_maker"]

    # Before anything that costs money. See `project_guard`: a deleted
    # project's queued work kept running AND kept enqueueing more. The
    # project comes from the token's signed claim, which is the same one
    # every write below is scoped to.
    refusal = await refuse_if_project_is_gone(maker, claims.project_id)
    if refusal is not None:
        return refusal
    asset_store = ctx["asset_store"]
    litellm = await gateways(ctx).litellm(claims.project_id)

    # AgentRun lifecycle so progress is visible to the UI Activity card.
    # Unique correlation_id per worker run (uq_agent_runs_correlation_id).
    chunk_run_id = uuid7()
    async with maker() as session:
        session.add(
            AgentRun(
                id=chunk_run_id,
                project_id=claims.project_id,
                agent_kind="chunk_embed",
                correlation_id=f"chunk-{chunk_run_id.hex}",
                status="running",
                started_at=utcnow(),
                input_payload={"normalized_document_id": str(normalized_id)},
                created_by=principal.user_id,
            )
        )
        await session.commit()

    async def _finalize(
        status: str, result_payload: dict[str, Any] | None = None, error_text: str | None = None
    ) -> None:
        async with maker() as session:
            run = (
                await session.execute(select(AgentRun).where(AgentRun.id == chunk_run_id))
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

    try:
        with start_span(
            "worker.chunk_embed",
            **{
                "aleph.normalized_document_id": str(normalized_id),
                "aleph.project_id": str(claims.project_id),
            },
        ):
            async with maker() as session:
                normalized = (
                    await session.execute(
                        select(NormalizedDocument).where(NormalizedDocument.id == normalized_id)
                    )
                ).scalar_one_or_none()
                if normalized is None:
                    msg = f"normalized document {normalized_id} not found"
                    raise RuntimeError(msg)
                profile = (
                    await session.execute(
                        select(ModelProfile).where(ModelProfile.project_id == normalized.project_id)
                    )
                ).scalar_one_or_none()
                if profile is None:
                    msg = "project has no model profile"
                    raise RuntimeError(msg)
                project_id = normalized.project_id
                source_id = normalized.source_id
                bindings = dict(profile.bindings_jsonb)

            outcome = await index_normalized_document(
                maker=maker,
                normalized_id=normalized_id,
                asset_store=asset_store,
                litellm=litellm,
                principal=principal,
                profile_bindings=bindings,
                agent_run_id=claims.agent_run_id,
                purpose="rks.embed",
            )

            async with maker() as session:
                ledger = LedgerWriter(session)
                await ledger.append(
                    project_id=project_id,
                    actor_id=principal.user_id,
                    actor_kind=principal.actor_kind,
                    action_kind="embeddings.completed" if outcome.ok else "embeddings.degraded",
                    target_id=normalized_id,
                    target_kind="normalized_document",
                    payload={
                        "source_id": str(source_id),
                        "chunk_count": outcome.chunk_count,
                        "embedder_model": outcome.embedder_model,
                        "state": outcome.state,
                        "reason": outcome.reason,
                    },
                    trace_id=current_trace_id(),
                )
                await session.commit()
    except Exception as exc:
        await _finalize("failed", error_text=f"{type(exc).__name__}: {exc}")
        raise

    result = {
        "ok": outcome.ok,
        "chunk_count": outcome.chunk_count,
        "embedder": outcome.embedder_model,
        "state": outcome.state,
        "reason": outcome.reason,
    }
    if outcome.ok:
        await _finalize("succeeded", result)
    else:
        # Searchable, but not as designed. `failed` is the honest status: it is
        # what puts the reason in front of an operator instead of in a log line
        # nobody reads.
        await _finalize("failed", result, error_text=outcome.reason)
        # A source with no text at all is not a degradation, it is an empty
        # document; only mark the source failed when chunks exist but the dense
        # leg does not.
        if outcome.chunk_count:
            async with maker() as session:
                src = (
                    await session.execute(select(Source).where(Source.id == source_id))
                ).scalar_one_or_none()
                if src is not None:
                    src.failure_reason = (outcome.reason or "")[:2048]
                await session.commit()
    return result
