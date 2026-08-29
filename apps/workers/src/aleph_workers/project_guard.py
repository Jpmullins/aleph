"""Refuse to do background work for a project somebody deleted.

Measured on this instance, 2026-08-29: **$141.43 and 1,766 model calls in
sixty minutes**, every dollar of it against `[e2e]` fixture projects whose
status was already `deleted`. $70.00 on "[e2e] settings shot" alone. The
topics being researched were "Playwright Test Framework Configuration" and
"e2e Screenshot Capture Configuration" — the browser suite's own fixtures,
searched against arXiv and OpenAlex as though they were research questions.

The mechanism, and it is not a race:

  1. The e2e suite creates a project and drives a real workflow.
  2. `deleteProject` sets `status = 'deleted'` and leaves the rows — correct,
     because someone who deletes a project by mistake should get it back.
  3. Nothing tells the QUEUE. Work already enqueued keeps running, and the
     wiki chain fans out as it goes: an ingest mints topic-page stubs, each
     stub is composed, each composition enqueues a curate and a review. The
     queue grew from 323 to 866 jobs while draining at full concurrency.

So a deleted project was not merely finishing its outstanding work; it was
generating more, indefinitely, at roughly two dollars a minute. `docs/plan.md`
number 5 counts uncosted calls and would report all of these as perfectly
attributed, because they were: correctly recorded, correctly priced, and spent
on a project that no longer exists.

`purge_deleted_projects` already existed and is the other half — it reclaims
the ROWS behind a dead project. Nothing refused to WORK for one. A purge that
runs after the spend is not a substitute for a guard that runs before it.

The check is one indexed primary-key read at the top of a job that is about to
make model calls costing dollars, so its cost is not worth discussing.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from aleph_db.models.project import Project

_log = structlog.get_logger(__name__)

#: What a refused job returns. A skip is a RESULT, not an exception: arq retries
#: a raising job, and retrying a job whose project is deleted would reproduce
#: exactly the loop this module exists to stop — 111 failed research runs for
#: one topic is what that looks like.
SKIPPED_REASON = "project is deleted"


async def refuse_if_project_is_gone(
    maker: Any, project_id: UUID | str | None
) -> dict[str, Any] | None:
    """``None`` to proceed; a result dict to stop.

    Returns the skip for a project that is deleted **or absent**. Absent
    matters on its own: `purge_deleted_projects` hard-deletes the rows behind a
    dead project, so a job queued before the purge and run after it finds no
    row at all rather than a `deleted` one. Treating "no such project" as
    permission to continue would leave the exact window the purge creates.

    A `None` project_id proceeds, because a job that is not project-scoped has
    nothing to check and this must not become a reason for one to fail.
    """
    if project_id is None:
        return None
    pid = UUID(str(project_id)) if not isinstance(project_id, UUID) else project_id
    async with maker() as session:
        status = (
            await session.execute(select(Project.status).where(Project.id == pid))
        ).scalar_one_or_none()
    if status is not None and status != "deleted":
        return None
    _log.info(
        "worker.refused_deleted_project",
        project_id=str(pid),
        status=status or "(no such project)",
    )
    return {"ok": False, "skipped": SKIPPED_REASON, "project_id": str(pid)}
