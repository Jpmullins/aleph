"""curate_page_job: knit the wiki graph after a page commit.

Enqueued best-effort by the authoring paths (AIQ synthesis + wiki ingest) after
a page is committed. Two stages, isolated transactions:

1. Deterministic knit (always commits): repair broken wikilinks project-wide +
   register the page's title alias, so the overview and sibling pages link to
   the new page.
2. LLM overview recuration (best-effort, separate transaction): fold the new
   topic into the project overview page. A gateway failure here never undoes
   the deterministic knit.

See ``docs/superpowers/specs/2026-06-25-wiki-curator-design.md``.
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
from aleph_observability.tracing import start_span
from aleph_security.principal import Principal
from aleph_wiki.curator_service import CuratorService

_log = structlog.get_logger(__name__)


async def curate_page_job(ctx: dict[str, Any], project_id: str, page_id: str) -> dict[str, Any]:
    maker = ctx["session_maker"]
    litellm = ctx["litellm_client"]
    pid = UUID(project_id)
    page = UUID(page_id)

    with start_span(
        "worker.curate_page",
        **{"aleph.project_id": project_id, "aleph.page_id": page_id},
    ):
        # Resolve owner + model profile and mint the curator AgentRun (used for
        # cost attribution on the overview-recuration LLM call).
        async with maker() as session:
            project = (
                await session.execute(select(Project).where(Project.id == pid))
            ).scalar_one_or_none()
            if project is None:
                return {"links_repaired": 0, "aliases_registered": 0, "overview_recurated": False}
            profile = (
                await session.execute(select(ModelProfile).where(ModelProfile.project_id == pid))
            ).scalar_one_or_none()
            owner = project.created_by
            run_id = uuid7()
            corr = f"curator-{run_id.hex}"
            session.add(
                AgentRun(
                    id=run_id,
                    project_id=pid,
                    agent_kind="curator",
                    correlation_id=corr,
                    status="running",
                    input_payload={"page_id": page_id},
                    created_by=owner,
                    access_scope="project",
                )
            )
            await session.commit()

        # Stage 1: deterministic knit — always commits.
        async with maker() as session:
            result = await CuratorService(session).curate(project_id=pid, page_id=page)
            await session.commit()

        # Stage 2: LLM overview recuration — best-effort, isolated transaction.
        recurated = False
        if profile is not None:
            principal = Principal(
                user_id=owner,
                subject="agent",
                email="",
                actor_kind="aleph_agent",
                agent_run_id=run_id,
                correlation_id=corr,
            )
            try:
                async with maker() as session:
                    recurated = await CuratorService(session).recurate_overview(
                        project_id=pid,
                        new_page_id=page,
                        litellm=litellm,
                        profile_bindings=profile.bindings_jsonb,
                        principal=principal,
                        agent_run_id=run_id,
                    )
                    await session.commit()
            except Exception as exc:  # best-effort: recuration must not fail the job
                _log.warning("wiki.curate.recurate_failed", page_id=page_id, error=str(exc))

            # Stage 3: near-duplicate detection — proposes a (human-gated) merge.
            try:
                async with maker() as session:
                    merge_proposal_id = await CuratorService(session).dedup_detect(
                        project_id=pid,
                        page_id=page,
                        litellm=litellm,
                        profile_bindings=profile.bindings_jsonb,
                        principal=principal,
                        agent_run_id=run_id,
                    )
                    await session.commit()
                if merge_proposal_id is not None:
                    _log.info(
                        "wiki.curate.merge_proposed",
                        page_id=page_id,
                        proposal_id=str(merge_proposal_id),
                    )
            except Exception as exc:  # best-effort: dedup must not fail the job
                _log.warning("wiki.curate.dedup_failed", page_id=page_id, error=str(exc))

        async with maker() as session:
            run = (
                await session.execute(select(AgentRun).where(AgentRun.id == run_id))
            ).scalar_one_or_none()
            if run is not None:
                run.status = "succeeded"
                run.completed_at = utcnow()
                await session.commit()

    _log.info(
        "wiki.curate.done",
        project_id=project_id,
        page_id=page_id,
        links_repaired=result.links_repaired,
        aliases_registered=result.aliases_registered,
        overview_recurated=recurated,
    )
    return {
        "links_repaired": result.links_repaired,
        "aliases_registered": result.aliases_registered,
        "overview_recurated": recurated,
    }
