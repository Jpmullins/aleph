"""The belief layer's repair path has a caller, and the caller reaches it.

`BeliefService.rebuild` had no non-test caller for the whole life of the belief
layer, and the reason was an interface that excluded its own implementation:
`Extractor` was typed sync while the only real extractor, `extract_claims`, is
`async def`. So the tests here are mostly about that seam — an async extractor
must work, a sync one must keep working, and the route has to actually enqueue
the job rather than merely answer 200.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.principal import Principal
from aleph_wiki.belief_service import BeliefService, ClaimUpsert, SourceText
from aleph_wiki.models import WikiClaim

pytestmark = pytest.mark.integration


def _principal(project_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        subject="rebuild-test",
        email="rebuild@test.invalid",
        actor_kind="aleph_agent",
        project_id=project_id,
    )


def _draft(text: str, page_id: uuid.UUID) -> ClaimUpsert:
    return ClaimUpsert(text=text, page_id=page_id, evidence=[])


async def _claim_texts(maker: Any, project_id: uuid.UUID) -> set[str]:
    async with maker() as s:
        return set(
            (await s.execute(select(WikiClaim.text).where(WikiClaim.project_id == project_id)))
            .scalars()
            .all()
        )


async def test_rebuild_accepts_an_async_extractor(maker: Any, committed_project: uuid.UUID) -> None:
    """The defect, stated as a test.

    `extract_claims` is `async def`. Before the `Extractor` union this raised
    `TypeError: 'coroutine' object is not iterable` inside `rebuild`'s loop —
    which is to say the designated repair could not be handed the only thing in
    the tree that does the deriving.
    """
    page_id = uuid7()

    async def extract(source: SourceText) -> list[ClaimUpsert]:
        return [_draft(f"async claim from {source.title}", page_id)]

    async with maker() as session:
        result = await BeliefService(session).rebuild(
            principal=_principal(committed_project),
            ledger=LedgerWriter(session),
            project_id=committed_project,
            extract=extract,
            sources=[SourceText(source_id=uuid7(), text="", title="S1")],
        )
        await session.commit()

    assert result.claims_after >= 1
    assert "async claim from S1" in await _claim_texts(maker, committed_project)


async def test_rebuild_still_accepts_a_sync_extractor(
    maker: Any, committed_project: uuid.UUID
) -> None:
    """Widening the type must not drop the fixture extractors that used it.

    The rebuild machinery has to stay runnable with no gateway at all — that is
    why `extract` is injected in the first place — so a plain callable returning
    a list is still a valid extractor.
    """
    page_id = uuid7()

    def extract(source: SourceText) -> list[ClaimUpsert]:
        return [_draft(f"sync claim from {source.title}", page_id)]

    async with maker() as session:
        await BeliefService(session).rebuild(
            principal=_principal(committed_project),
            ledger=LedgerWriter(session),
            project_id=committed_project,
            extract=extract,
            sources=[SourceText(source_id=uuid7(), text="", title="S2")],
        )
        await session.commit()

    assert "sync claim from S2" in await _claim_texts(maker, committed_project)


async def test_rebuild_is_idempotent_across_the_async_seam(
    maker: Any, committed_project: uuid.UUID
) -> None:
    """Twice over the same sources is the same graph.

    Identity is `claim_key`, so a re-run must not fork every claim into a
    second copy. Worth asserting through the async path specifically: awaiting
    inside the loop is new, and a second `await` on an already-consumed
    coroutine is exactly the kind of thing that silently yields nothing.
    """
    page_id = uuid7()
    sources = [SourceText(source_id=uuid7(), text="", title="S3")]

    async def extract(_source: SourceText) -> list[ClaimUpsert]:
        return [_draft("a stable proposition about S3", page_id)]

    async def run() -> Any:
        async with maker() as session:
            out = await BeliefService(session).rebuild(
                principal=_principal(committed_project),
                ledger=LedgerWriter(session),
                project_id=committed_project,
                extract=extract,
                sources=sources,
            )
            await session.commit()
            return out

    first = await run()
    second = await run()
    assert second.new_claims == 0
    assert second.claims_after == first.claims_after


async def test_the_job_is_registered_under_the_name_the_route_enqueues() -> None:
    """The two halves have to agree on a string.

    `enqueue_job` takes the function's NAME. A job registered under a different
    one, or not registered at all, leaves the route answering `enqueued: true`
    while arq drops the message — a dispatch that reports success and does
    nothing, which is this codebase's most expensive failure shape.
    """
    from aleph_workers.arq import WorkerSettings

    assert "belief_rebuild_job" in {f.__name__ for f in WorkerSettings.functions}
