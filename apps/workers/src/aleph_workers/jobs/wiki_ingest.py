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
from aleph_security.agent_token import mint_agent_token, verify_agent_token
from aleph_security.principal import Principal
from aleph_wiki.agent.workflow import WikiIngestState, WikiIngestWorkflow
from aleph_wiki.models import WikiRevision


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
                await session.execute(select(Source).where(Source.id == normalized.source_id))
            ).scalar_one_or_none()
            if source is None:
                msg = f"source {normalized.source_id} not found"
                raise RuntimeError(msg)
            profile = (
                await session.execute(
                    select(ModelProfile).where(ModelProfile.project_id == normalized.project_id)
                )
            ).scalar_one_or_none()
            if profile is None:
                msg = "project has no model profile"
                raise RuntimeError(msg)

            # Create AgentRun row for this ingest. Correlation_id MUST
            # be unique across all AgentRuns (DB unique constraint), so
            # we always derive a fresh one even when the upstream token
            # carries one — the upstream run (normalize) already owns
            # that id.
            new_agent_run_id = uuid7()
            agent_run = AgentRun(
                id=new_agent_run_id,
                project_id=normalized.project_id,
                agent_kind="wiki",
                correlation_id=f"wiki-{new_agent_run_id.hex}",
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

        workflow = WikiIngestWorkflow(session_maker=maker, litellm=litellm, principal=principal)
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
        except Exception as exc:
            result = {}
            status = "failed"
            error_text = str(exc)[:4096]
            committed = []

        async with maker() as session:
            ledger = LedgerWriter(session)
            run = (
                await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
            ).scalar_one_or_none()
            if run is not None:
                run.status = status
                run.completed_at = utcnow()
                run.result_payload = {
                    "committed_revision_ids": [str(r) for r in committed],
                }
                run.error_text = error_text
            src = (
                await session.execute(select(Source).where(Source.id == source.id))
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

    # Auto-enqueue MechanicalReviewer on every successfully committed
    # revision. Spec §6.1: "MechanicalReviewer — Auto-runs on every wiki
    # revision."
    if status == "succeeded" and committed:
        redis_pool = ctx.get("redis_pool")
        agent_secret: str = ctx["agent_token_secret"]
        if redis_pool is not None:
            async with maker() as session:
                for rev_id in committed:
                    rev = (
                        await session.execute(select(WikiRevision).where(WikiRevision.id == rev_id))
                    ).scalar_one_or_none()
                    if rev is None:
                        continue
                    # Mint a fresh AgentRun + token for the review.
                    # Kind must match what mechanical_review_job uses
                    # (so the row can be looked up by agent_run_id and
                    # transitioned to running).
                    review_run_id = uuid7()
                    review_run = AgentRun(
                        id=review_run_id,
                        project_id=normalized.project_id,
                        agent_kind="mechanical_reviewer",
                        # UUIDv7 timestamp prefix is shared across runs
                        # in the same millisecond, so use the full hex.
                        correlation_id=f"mech-{review_run_id.hex}",
                        status="pending",
                        input_payload={
                            "revision_id": str(rev_id),
                            "page_id": str(rev.page_id),
                        },
                        created_by=principal.user_id,
                        access_scope="project",
                    )
                    session.add(review_run)
                await session.commit()
                for rev_id in committed:
                    rev = (
                        await session.execute(select(WikiRevision).where(WikiRevision.id == rev_id))
                    ).scalar_one_or_none()
                    if rev is None:
                        continue
                    pending = (
                        (
                            await session.execute(
                                select(AgentRun)
                                .where(
                                    AgentRun.project_id == normalized.project_id,
                                    AgentRun.agent_kind == "mechanical_reviewer",
                                    AgentRun.input_payload["revision_id"].astext == str(rev_id),
                                    AgentRun.status == "pending",
                                )
                                .order_by(AgentRun.created_at.desc())
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if pending is None:
                        continue
                    review_token = mint_agent_token(
                        secret=agent_secret,
                        user_id=principal.user_id,
                        project_id=normalized.project_id,
                        agent_run_id=pending.id,
                        actor_kind="aleph_agent",
                        correlation_id=pending.correlation_id,
                    )
                    await redis_pool.enqueue_job(
                        "mechanical_review_job",
                        str(normalized.project_id),
                        str(rev_id),
                        str(rev.page_id),
                        review_token,
                    )
                    # Knit the wiki graph now that this page exists: repair
                    # broken links project-wide + register its title alias.
                    await redis_pool.enqueue_job(
                        "curate_page_job",
                        str(normalized.project_id),
                        str(rev.page_id),
                    )

    return {
        "ok": status == "succeeded",
        "agent_run_id": str(agent_run_id),
        "committed_revision_ids": [str(r) for r in committed],
        "error": error_text,
    }
