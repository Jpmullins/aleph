"""curate_page_job: knit the wiki graph after a page commit.

Enqueued best-effort by the authoring paths (AIQ synthesis + wiki ingest) after
a page is committed. Runs the deterministic ``CuratorService`` — repair broken
wikilinks project-wide + register the page's title alias — so the overview and
sibling pages link to the newly-created page. Idempotent; safe to re-run.

See ``docs/superpowers/specs/2026-06-25-wiki-curator-design.md``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from aleph_observability.tracing import start_span
from aleph_wiki.curator_service import CuratorService

_log = structlog.get_logger(__name__)


async def curate_page_job(ctx: dict[str, Any], project_id: str, page_id: str) -> dict[str, int]:
    maker = ctx["session_maker"]
    pid = UUID(project_id)
    page = UUID(page_id)
    with start_span(
        "worker.curate_page",
        **{"aleph.project_id": project_id, "aleph.page_id": page_id},
    ):
        async with maker() as session:
            result = await CuratorService(session).curate(project_id=pid, page_id=page)
            await session.commit()
    _log.info(
        "wiki.curate.done",
        project_id=project_id,
        page_id=page_id,
        links_repaired=result.links_repaired,
        aliases_registered=result.aliases_registered,
    )
    return {
        "links_repaired": result.links_repaired,
        "aliases_registered": result.aliases_registered,
    }
