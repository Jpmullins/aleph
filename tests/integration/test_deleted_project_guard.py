"""A deleted project stops costing money.

Deleting a project sets `status = 'deleted'` and leaves the rows — correct,
because someone who deletes one by mistake should get it back. Nothing told the
QUEUE, so work already enqueued kept running, and the wiki chain kept enqueueing
more as it went: an ingest mints topic-page stubs, each stub is composed, each
composition enqueues a curate and a review.

Measured on this instance before the guard: **$141.43 and 1,766 model calls in
sixty minutes**, every dollar against `[e2e]` fixture projects whose status was
already `deleted`. $70.00 on "[e2e] settings shot" alone, researching
"Playwright Test Framework Configuration" against arXiv.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from aleph_core.ids import uuid7
from aleph_db.models.project import Project
from aleph_workers.project_guard import SKIPPED_REASON, refuse_if_project_is_gone

pytestmark = pytest.mark.integration


async def _create(maker: Any, project_id: uuid.UUID, status: str = "active") -> None:
    """A real `projects` row.

    `committed_project` yields an id and creates nothing — the fixture exists to
    scope teardown, not to seed. The guard reads the row, so these tests have to
    put one there.
    """
    async with maker() as s:
        s.add(
            Project(
                id=project_id,
                title="Deleted Project Guard Test",
                description="",
                status=status,
                model_profile_id=uuid7(),
                created_by=uuid.uuid4(),
            )
        )
        await s.commit()


async def _set_status(maker: Any, project_id: uuid.UUID, status: str) -> None:
    async with maker() as s:
        await s.execute(
            text("UPDATE projects SET status = :st WHERE id = :id"),
            {"st": status, "id": project_id},
        )
        await s.commit()


async def test_a_live_project_proceeds(maker: Any, committed_project: uuid.UUID) -> None:
    await _create(maker, committed_project)
    assert await refuse_if_project_is_gone(maker, committed_project) is None


async def test_a_deleted_project_is_refused(maker: Any, committed_project: uuid.UUID) -> None:
    await _create(maker, committed_project)
    await _set_status(maker, committed_project, "deleted")
    refusal = await refuse_if_project_is_gone(maker, committed_project)
    assert refusal is not None
    assert refusal["skipped"] == SKIPPED_REASON
    assert refusal["ok"] is False


async def test_a_project_that_no_longer_exists_is_refused(maker: Any) -> None:
    """The window `purge_deleted_projects` opens.

    The purge hard-deletes the rows behind a dead project, so a job enqueued
    before it and run after finds NO row rather than a `deleted` one. Treating
    "no such project" as permission to continue would leave exactly the gap the
    purge creates — and it is the gap that stays open longest, because the row
    that would have said `deleted` is the row that was removed.
    """
    refusal = await refuse_if_project_is_gone(maker, uuid.uuid4())
    assert refusal is not None
    assert refusal["skipped"] == SKIPPED_REASON


async def test_no_project_id_proceeds(maker: Any) -> None:
    """A job that is not project-scoped must not be broken by the guard."""
    assert await refuse_if_project_is_gone(maker, None) is None


async def test_the_refusal_is_a_result_and_not_an_exception(
    maker: Any, committed_project: uuid.UUID
) -> None:
    """Raising would rebuild the loop this exists to stop.

    arq retries a raising job. A job that raises because its project is deleted
    would be retried against a project that is still deleted, which is the
    runaway with an extra step — 111 failed research runs for one topic is what
    that looked like in practice. So the refusal is a RETURN VALUE: arq records
    it, and the job is done.
    """
    await _create(maker, committed_project)
    await _set_status(maker, committed_project, "deleted")
    refusal = await refuse_if_project_is_gone(maker, committed_project)
    assert isinstance(refusal, dict)
