"""Retrieval over claims — WS-RS10.

The question is real and could honestly go either way: does searching what a
project CONCLUDED beat searching what it COLLECTED? A claim is short,
deduplicated and evidence-anchored, which should help; it is also a lossy
restatement of a passage, which should hurt.

Nobody could ask it before. `wiki_claims.embedding` is NULL on all 1,325 rows
and the HNSW index has never had anything to index — a vector index over an
empty column, which looks exactly like a working one.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_core.ids import uuid7
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.principal import Principal
from aleph_wiki.belief_service import BeliefService, ClaimUpsert
from aleph_wiki.claim_search import search_claims
from aleph_wiki.models import ClaimEdge, WikiClaim

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
DIM = 1024


def _agent() -> Principal:
    return Principal(
        user_id=ACTOR, subject="rs10", email="rs10@example.test", actor_kind="aleph_agent"
    )


def _vector(seed: int) -> list[float]:
    """A deterministic unit-ish vector. Distinct seeds are far apart."""
    v = [0.0] * DIM
    v[seed % DIM] = 1.0
    return v


async def _page(session: AsyncSession, project_id: uuid.UUID) -> uuid.UUID:
    page_id = uuid7()
    await session.execute(
        sql_text(
            "INSERT INTO wiki_pages (id, project_id, title, slug, page_kind, status, created_by)"
            " VALUES (:id, :pid, :t, :s, 'topic', 'draft', :a)"
        ),
        {
            "id": page_id,
            "pid": project_id,
            # FULL hex, not a prefix. uuid7's leading bits are a millisecond
            # timestamp, so `hex[:6]` collides for two pages created in the same
            # millisecond — which is exactly what a loop in one test does, and
            # exactly the trap `chat_runs.py` documents for correlation ids.
            "t": f"Page {page_id.hex}",
            "s": f"page-{page_id.hex}",
            "a": ACTOR,
        },
    )
    return page_id


async def _claim(
    maker: Callable[[], AsyncSession],
    project_id: uuid.UUID,
    text: str,
    *,
    seed: int | None = None,
) -> uuid.UUID:
    async with maker() as session:
        page_id = await _page(session, project_id)
        result = await BeliefService(session).upsert_claim(
            principal=_agent(),
            ledger=LedgerWriter(session),
            project_id=project_id,
            draft=ClaimUpsert(text=text, page_id=page_id),
            embed=(lambda _t: _vector(seed)) if seed is not None else None,
        )
        await session.commit()
    return result.claim_id


async def test_a_claim_is_embedded_at_write_time(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Criterion 2. All 1,325 existing rows are NULL."""
    claim_id = await _claim(
        maker, committed_project, "Sedimentation rates rose after the 8.2 ka event", seed=3
    )
    async with maker() as session:
        row = (
            await session.execute(
                sql_text("select embedding is not null as has_vec from wiki_claims where id = :i"),
                {"i": claim_id},
            )
        ).one()
    assert row.has_vec is True


async def test_an_embedder_failure_does_not_lose_the_claim(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A claim with no vector is still a claim.

    Refusing to record a belief because a model was unreachable would lose the
    thing in order to protect the index of it. The lexical leg still finds it,
    `claim_key` still identifies it, and `rebuild` can fill the column later.
    """

    def _boom(_t: str) -> list[float]:
        msg = "gateway down"
        raise RuntimeError(msg)

    async with maker() as session:
        page_id = await _page(session, committed_project)
        result = await BeliefService(session).upsert_claim(
            principal=_agent(),
            ledger=LedgerWriter(session),
            project_id=committed_project,
            draft=ClaimUpsert(text="Radiocarbon dates bracket the transition", page_id=page_id),
            embed=_boom,
        )
        await session.commit()
    async with maker() as session:
        row = (
            await session.execute(
                sql_text("select embedding is null as no_vec from wiki_claims where id = :i"),
                {"i": result.claim_id},
            )
        ).one()
    assert row.no_vec is True


async def test_the_lexical_leg_finds_a_claim_with_no_vector(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Which is what makes swallowing the embed failure survivable."""
    await _claim(maker, committed_project, "Permafrost thaw accelerated through the interval")
    async with maker() as session:
        hits = await search_claims(
            session,
            project_id=committed_project,
            query_text="permafrost thaw",
            query_embedding=None,
        )
    assert any("Permafrost" in h.text for h in hits)


async def test_a_superseded_claim_is_never_returned(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """What the project USED to think is not an answer.

    Returning it is worse than returning nothing: it is a statement the project
    has withdrawn, presented as a current one.
    """
    old = await _claim(maker, committed_project, "Salinity fell sharply across the boundary")
    # A REAL successor. `superseded_by` carries a foreign key, so pointing it at
    # an invented uuid does not test supersession — it tests that the database
    # refuses nonsense, which it already does.
    successor = await _claim(
        maker, committed_project, "Salinity fell gradually across the boundary"
    )
    async with maker() as session:
        claim = await session.get(WikiClaim, old)
        assert claim is not None
        claim.superseded_by = successor
        await session.commit()
    async with maker() as session:
        hits = await search_claims(
            session,
            project_id=committed_project,
            query_text="salinity boundary",
            query_embedding=None,
        )
    assert all(h.claim_id != old for h in hits)


async def test_the_graph_hop_reaches_a_claim_no_passage_index_could(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The one thing a claim layer can do that a chunk index cannot.

    The neighbour shares NO vocabulary with the query, so nothing lexical or
    dense will find it. It is reachable only by walking an edge from something
    that did match — and it is flagged `via_graph` so the hop's contribution can
    be measured rather than assumed.
    """
    matched = await _claim(
        maker, committed_project, "Obliquity forcing dominates the eccentricity signal"
    )
    neighbour = await _claim(maker, committed_project, "Tephra layers constrain the age model")
    async with maker() as session:
        session.add(
            ClaimEdge(
                id=uuid7(),
                project_id=committed_project,
                src_claim_id=matched,
                dst_claim_id=neighbour,
                kind="relates_to",
                weight=1.0,
                created_by=ACTOR,
            )
        )
        await session.commit()

    async with maker() as session:
        hits = await search_claims(
            session,
            project_id=committed_project,
            query_text="obliquity forcing eccentricity",
            query_embedding=None,
            top_k=8,
        )
    reached = {h.claim_id: h for h in hits}
    assert matched in reached
    assert neighbour in reached, "the graph hop returned nothing a direct match would not"
    assert reached[neighbour].via_graph is True
    assert reached[matched].via_graph is False


async def test_the_graph_hop_can_be_turned_off_so_its_effect_is_measurable(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """An improvement you cannot switch off is an improvement you cannot measure."""
    matched = await _claim(maker, committed_project, "Insolation drives the monsoon intensity")
    neighbour = await _claim(maker, committed_project, "Varve counts anchor the chronology")
    async with maker() as session:
        session.add(
            ClaimEdge(
                id=uuid7(),
                project_id=committed_project,
                src_claim_id=matched,
                dst_claim_id=neighbour,
                kind="relates_to",
                weight=1.0,
                created_by=ACTOR,
            )
        )
        await session.commit()
    async with maker() as session:
        hits = await search_claims(
            session,
            project_id=committed_project,
            query_text="insolation monsoon intensity",
            query_embedding=None,
            walk_graph=False,
        )
    assert all(not h.via_graph for h in hits)
    assert neighbour not in {h.claim_id for h in hits}


async def test_a_superseding_edge_is_not_walked(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """`supersedes` points at a belief the project withdrew.

    Walking it would surface a retracted statement as a related result, which is
    the one direction this graph must not help in.
    """
    from aleph_wiki.claim_search import TRAVERSABLE_EDGES

    assert "supersedes" not in TRAVERSABLE_EDGES


async def test_hits_carry_the_absolute_scores_not_only_the_fused_rank(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Same reason as `ChunkHit`: RRF says nothing about whether the top hit is
    any good."""
    await _claim(maker, committed_project, "Advection explains the residual heat flux", seed=11)
    async with maker() as session:
        hits = await search_claims(
            session,
            project_id=committed_project,
            query_text="advection residual heat flux",
            query_embedding=_vector(11),
        )
    assert hits
    direct = [h for h in hits if not h.via_graph]
    assert any(h.cosine_distance is not None for h in direct)
    assert any(h.lexical_rank is not None for h in direct)


def test_claim_search_does_not_live_in_aleph_belief() -> None:
    """The plan says to put it there; that would be a dependency cycle.

    `aleph-wiki` imports `aleph_belief.patch` and `aleph_belief.reconcile`, so
    `aleph_belief` cannot import `WikiClaim`. Recorded as a test rather than a
    comment because the plan still says otherwise, and the next person will
    reach for it.
    """
    import pathlib

    belief = pathlib.Path("packages/aleph-belief/src/aleph_belief")
    offenders = [p.name for p in belief.glob("*.py") if "aleph_wiki" in p.read_text()]
    assert offenders == [], f"aleph-belief imports aleph-wiki: {offenders}"


def _unused(_: Any) -> None:  # pragma: no cover - keeps the import list honest
    pass
