"""wiki_ingest_job: NormalizedDocument → wiki commit via the LangGraph workflow."""

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
from aleph_rks.models import NormalizedDocument, Source
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal
from aleph_wiki.agent.workflow import WikiIngestState, WikiIngestWorkflow


async def wiki_ingest_job(
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
    asset_store = ctx["asset_store"]
    litellm = ctx["litellm_client"]

    with start_span(
        "worker.wiki_ingest",
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
            source = (
                await session.execute(
                    select(Source).where(Source.id == normalized.source_id)
                )
            ).scalar_one_or_none()
            if source is None:
                msg = f"source {normalized.source_id} not found"
                raise RuntimeError(msg)
            profile = (
                await session.execute(
                    select(ModelProfile).where(
                        ModelProfile.project_id == normalized.project_id
                    )
                )
            ).scalar_one_or_none()
            if profile is None:
                msg = "project has no model profile"
                raise RuntimeError(msg)

            # Create AgentRun row for this ingest.
            agent_run = AgentRun(
                id=uuid7(),
                project_id=normalized.project_id,
                agent_kind="wiki",
                correlation_id=claims.correlation_id or f"wiki-{normalized_id.hex[:8]}",
                status="running",
                started_at=utcnow(),
                input_payload={
                    "normalized_document_id": str(normalized_id),
                    "source_id": str(source.id),
                },
                created_by=principal.user_id,
                access_scope="project",
            )
            session.add(agent_run)
            await session.flush()
            agent_run_id = agent_run.id
            await session.commit()

        # Fetch markdown for the workflow.
        markdown = asset_store.get(normalized.markdown_uri).decode("utf-8")

        workflow = WikiIngestWorkflow(
            session_maker=maker, litellm=litellm, principal=principal
        )
        initial: WikiIngestState = {
            "agent_run_id": agent_run_id,
            "project_id": normalized.project_id,
            "source_id": source.id,
            "source_short_id": source.short_id,
            "source_title": source.title,
            "source_url": source.url,
            "connector_kind": source.connector_kind,
            "ingested_at_iso": utcnow().isoformat(),
            "normalized_document_id": normalized_id,
            "normalized_markdown": markdown,
            "profile_bindings": profile.bindings_jsonb,
        }
        try:
            result = await workflow.run(initial)
            status = "succeeded"
            error_text: str | None = None
            committed = result.get("committed_revision_ids") or []
        except Exception as exc:  # noqa: BLE001
            result = {}
            status = "failed"
            error_text = str(exc)[:4096]
            committed = []

        async with maker() as session:
            ledger = LedgerWriter(session)
            run = (
                await session.execute(
                    select(AgentRun).where(AgentRun.id == agent_run_id)
                )
            ).scalar_one_or_none()
            if run is not None:
                run.status = status
                run.completed_at = utcnow()
                run.result_payload = {
                    "committed_revision_ids": [str(r) for r in committed],
                }
                run.error_text = error_text
            src = (
                await session.execute(
                    select(Source).where(Source.id == source.id)
                )
            ).scalar_one_or_none()
            if src is not None:
                if status == "succeeded":
                    src.status = "wiki_done"
                else:
                    src.status = "wiki_failed"
                    if error_text:
                        src.failure_reason = error_text[:2048]
            await ledger.append(
                project_id=normalized.project_id,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                action_kind=f"wiki_ingest.{status}",
                target_id=source.id,
                target_kind="source",
                payload={
                    "agent_run_id": str(agent_run_id),
                    "committed_revisions": [str(r) for r in committed],
                    "error": error_text,
                },
                trace_id=current_trace_id(),
            )
            await session.commit()

    return {
        "ok": status == "succeeded",
        "agent_run_id": str(agent_run_id),
        "committed_revision_ids": [str(r) for r in committed],
        "error": error_text,
    }
