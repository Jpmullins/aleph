"""The dense leg has to come back with what it asked for. WS-RS6.

pgvector's HNSW scan is bounded by `hnsw.ef_search` — the size of the candidate
list it keeps while walking the graph — and that bound is applied BEFORE the
`project_id` predicate. So the index hands Postgres its candidates, the filter
throws away the ones belonging to other projects, and a query that asked for
forty rows gets whatever survives. Nothing reports the shortfall: a short dense
ranking fuses into a result list that looks completely ordinary, and hybrid
search quietly becomes keyword search. Repo-wide, neither `ef_search` nor
`iterative_scan` appeared anywhere before this workstream.

**Measured on this stack** (Postgres 17.11, pgvector 0.8.6, 10,000 chunks in
`document_chunks`, one `ORDER BY embedding <=> … LIMIT 40`, HNSW path):

| projects | ef_search  | iterative_scan | rows returned |
|----------|------------|----------------|---------------|
| 5        | 40 (dflt)  | off (dflt)     | **7**         |
| 5        | 160 (RS6)  | off            | 24            |
| 5        | 160 (RS6)  | strict_order   | 40            |
| 25       | 40 (dflt)  | off (dflt)     | **1**         |
| 25       | 160 (RS6)  | off            | 6             |
| 25       | 160 (RS6)  | strict_order   | 40            |

So `ef_search` alone does not close it — at five projects it recovers 24 of 40 —
and `iterative_scan` is what actually makes the dense leg return its window.
Both are set.

**The fixture vectors are clustered, and that is load-bearing.** Written as
uniform random values the same table produced 40 rows on one run and 0 on the
next, at every setting, because a uniform vector cloud puts every pair at
almost the same cosine distance: the HNSW graph is then close to random and the
walk goes nowhere in particular. Real embeddings are not like that, so the
fixture draws each row from one of twelve topic centroids with noise. Topics
are assigned independently of project — a corpus where each project occupies
its own region of the space would make the filter free and there would be
nothing to measure. With clustered rows the numbers above are stable across
repeated seeds.

The planner is forced onto the index path in these tests
(`enable_sort`/`enable_bitmapscan` off) because at 10,000 rows it prefers a
btree scan on `project_id` plus a top-N sort, which has no `ef_search` and no
shortfall. That choice flips with corpus size, so an unforced test would be
green today, red at production scale, and green again for reasons nobody could
name. Forcing it makes the test about the index rather than about the planner's
current cost estimate.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from aleph_rks.models import EMBEDDING_DIM, DocumentChunk
from aleph_rks.retrieval import (
    EF_SEARCH_CEILING,
    EF_SEARCH_FLOOR,
    dense_candidates,
    ef_search_for,
    fetch_for,
    iterative_scan_supported,
    reset_scan_support_cache,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

#: 10,000 chunks across 5 projects, exactly as the criterion says.
TOTAL_CHUNKS = 10_000
PROJECTS = 5
#: Which of the five the assertions query. Any one; they are the same size.
TARGET = 3
#: Topic centroids the fixture rows are drawn from, assigned independently of
#: project. Twelve and five share no factor, so every topic reaches every
#: project and the nearest neighbours of a query span projects — which is the
#: whole situation `project_id` post-filtering has to survive.
TOPICS = 12
#: A `top_k` whose `fetch_for` is 40, matching the measured table above.
TOP_K = 10

_MARKER = "rs6-vector-scan"


# --- the pure parts, which need no database --------------------------------


def test_the_fetch_window_is_the_one_production_uses() -> None:
    assert fetch_for(10) == 40
    assert fetch_for(1) == 40  # the floor
    assert fetch_for(40) == 160


def test_ef_search_is_a_multiple_of_the_window_between_a_floor_and_a_ceiling() -> None:
    assert ef_search_for(40) == 160
    assert ef_search_for(1) == EF_SEARCH_FLOOR
    assert ef_search_for(10_000) == EF_SEARCH_CEILING


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("0.8.6", True),
        ("0.8.0", True),
        ("1.0.0", True),
        ("0.7.4", False),
        ("0.5.1", False),
        # An extension that is not installed at all, and a version string this
        # parser must not crash on. Setting `hnsw.iterative_scan` against a
        # loaded pgvector that predates it RAISES, and a raise inside a
        # transaction poisons the whole transaction — so a wrong answer here
        # takes down every search, not just the tuning.
        (None, False),
        ("", False),
        ("banana", False),
        ("0.8", True),
        ("0.8beta", True),
    ],
)
def test_the_version_gate_answers_for_every_shape_of_version(
    version: str | None, supported: bool
) -> None:
    assert iterative_scan_supported(version) is supported


# --- the database ----------------------------------------------------------


def _database_url() -> str:
    url = os.environ.get("ALEPH_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is required for the vector scan tests")
    return url


@pytest.fixture(scope="module")
def project_ids() -> list[UUID]:
    """Stable, obviously-synthetic ids so a leaked row is identifiable."""
    return [UUID(f"00000000-0000-4000-8000-{i:012d}") for i in range(PROJECTS)]


async def _write_rows(url: str, *, insert: bool) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await session.execute(
            text("delete from document_chunks where embedder_model = :m"), {"m": _MARKER}
        )
        if insert:
            await session.execute(
                text(
                    "with centroids as ("
                    "  select k, (select array_agg(random() - 0.5) "
                    "               from generate_series(1, :dim)) as c "
                    "  from generate_series(0, :topics - 1) k"
                    ") "
                    "insert into document_chunks (id, project_id, source_id, "
                    "normalized_document_id, ordinal, text, text_tsv, embedding, char_start, "
                    "char_end, token_count, embedder_model, section_path) "
                    "select gen_random_uuid(), "
                    "  ('00000000-0000-4000-8000-' || "
                    "     lpad((i % :projects)::text, 12, '0'))::uuid, "
                    "  gen_random_uuid(), gen_random_uuid(), i, 'rs6 scan probe ' || i, "
                    "  ''::tsvector, "
                    # `with ordinality` + `order by o`: array_agg over unnest has
                    # no guaranteed order, and a vector whose dimensions are
                    # shuffled per row is noise wearing the shape of a cluster.
                    "  (select array_agg(v + (random() - 0.5) * :noise order by o)"
                    "     ::real[]::vector "
                    "     from unnest(centroids.c) with ordinality as t(v, o)), "
                    "  0, 10, 3, :marker, null "
                    "from generate_series(1, :total) i "
                    "join centroids on centroids.k = i % :topics"
                ),
                {
                    "projects": PROJECTS,
                    "topics": TOPICS,
                    "noise": 0.6,
                    "dim": EMBEDDING_DIM,
                    "total": TOTAL_CHUNKS,
                    "marker": _MARKER,
                },
            )
        await session.commit()
    await engine.dispose()


@pytest.fixture(scope="module")
def seeded_rows() -> Iterator[str]:
    """10,000 real `document_chunks` rows, then remove every one of them.

    Rows are generated server-side. Ten thousand 1024-wide vectors is 40MB over
    the wire from Python and about two seconds inside Postgres, and the HNSW
    index on `document_chunks` is the index under test — so the rows have to
    live in the real table, not in a copy of it.

    Seeded from its own event loop rather than an async fixture: pytest-asyncio
    gives each test a fresh loop, and an asyncpg connection created on one loop
    and awaited on another fails with "another operation is in progress".
    """
    import asyncio

    url = _database_url()
    asyncio.run(_write_rows(url, insert=True))
    try:
        yield url
    finally:
        asyncio.run(_write_rows(url, insert=False))


@pytest_asyncio.fixture
async def maker(seeded_rows: str) -> AsyncIterator[Any]:
    """A session factory on this test's own loop."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(seeded_rows)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def _fresh_probe() -> Iterator[None]:
    """The pgvector version probe is cached per process; do not inherit it."""
    reset_scan_support_cache()
    yield
    reset_scan_support_cache()


async def _query_vector(session: AsyncSession) -> list[float]:
    """A vector taken from the seeded corpus, so the query lands in a cluster.

    A freshly invented random vector is equidistant from every centroid, which
    is the degenerate case described in the module docstring. A real query
    embedding sits near the passages that answer it; borrowing one of the rows
    reproduces that without inventing a distribution.
    """
    row = (
        await session.execute(
            # Through the ORM column, not raw SQL: pgvector's type handler is
            # what turns the wire format into floats, and a `text()` query
            # hands back the literal string "[0.1,...]".
            select(DocumentChunk.embedding)
            .where(DocumentChunk.embedder_model == _MARKER)
            .order_by(DocumentChunk.ordinal)
            .limit(1)
        )
    ).scalar_one()
    return [float(x) for x in row]


async def _force_index_scan(session: AsyncSession) -> None:
    """Take the sort-and-filter plan away, leaving the HNSW index scan.

    See the module docstring: at 10,000 rows the planner prefers a btree scan on
    `project_id` and a top-N sort, which is not the plan whose row count
    `ef_search` governs.
    """
    await session.execute(select(func.set_config("enable_sort", "off", True)))
    await session.execute(select(func.set_config("enable_bitmapscan", "off", True)))


@pytest.mark.usefixtures("_fresh_probe")
async def test_dense_leg_returns_full_window(maker: Any, project_ids: list[UUID]) -> None:
    """The criterion: exactly `fetch` rows for the target project.

    Without the scan configuration this returns 15 of 40 — measured, and
    reproduced by `test_removing_the_scan_configuration_loses_rows` below.
    """
    async with maker() as session:
        seeded = (
            await session.execute(
                text("select count(*) from document_chunks where embedder_model = :m"),
                {"m": _MARKER},
            )
        ).scalar_one()
        await _force_index_scan(session)
        rows = await dense_candidates(
            session,
            project_id=project_ids[TARGET],
            embedding=await _query_vector(session),
            top_k=TOP_K,
        )
    assert seeded == TOTAL_CHUNKS, (
        "the fixture rows are gone, so this measured nothing — another suite "
        "sharing this database removed them"
    )
    assert len(rows) == fetch_for(TOP_K)
    assert all(r.distance is not None for r in rows), (
        "the distance is selected, not merely ordered by — it was computed and "
        "discarded once already"
    )


@pytest.mark.usefixtures("_fresh_probe")
async def test_removing_the_scan_configuration_loses_rows(
    maker: Any, project_ids: list[UUID]
) -> None:
    """The same query at the server's defaults, which is the pre-RS6 state.

    This is the mutation as a test rather than as a note. It runs the identical
    statement with no GUCs set, so if a future change quietly stops configuring
    the scan, `test_dense_leg_returns_full_window` fails and this one says why.
    """
    fetch = fetch_for(TOP_K)

    async with maker() as session:
        await _force_index_scan(session)
        distance = DocumentChunk.embedding.cosine_distance(await _query_vector(session))  # type: ignore[attr-defined]
        untuned = list(
            (
                await session.execute(
                    select(DocumentChunk.id)
                    .where(
                        DocumentChunk.project_id == project_ids[TARGET],
                        DocumentChunk.embedding.isnot(None),
                    )
                    .order_by(distance)
                    .limit(fetch)
                )
            ).all()
        )
    assert len(untuned) < fetch, (
        f"the untuned scan returned {len(untuned)} of {fetch} rows — if this is now "
        f"{fetch}, the post-filtering shortfall no longer reproduces at "
        f"{TOTAL_CHUNKS} chunks over {PROJECTS} projects and the tuning above is "
        f"no longer measuring anything. Raise PROJECTS or say so."
    )


@pytest.mark.usefixtures("_fresh_probe")
async def test_ef_search_is_set_in_the_transaction_that_runs_the_query(
    maker: Any, project_ids: list[UUID]
) -> None:
    """`SHOW hnsw.ef_search` inside the same transaction, as the criterion says.

    The expected value is computed by production's own `ef_search_for`, not
    restated here: a constant copied into the test would keep passing after
    production stopped setting anything, since a literal `160` also matches a
    server whose default happens to be 160.
    """
    async with maker() as session:
        await dense_candidates(
            session,
            project_id=project_ids[TARGET],
            embedding=await _query_vector(session),
            top_k=TOP_K,
        )
        ef = (await session.execute(text("show hnsw.ef_search"))).scalar_one()
        iterative = (await session.execute(text("show hnsw.iterative_scan"))).scalar_one()
        max_tuples = (await session.execute(text("show hnsw.max_scan_tuples"))).scalar_one()
    assert int(ef) == ef_search_for(fetch_for(TOP_K))
    assert iterative == "strict_order", (
        "relaxed_order returns rows slightly out of distance order, and the dense "
        "leg's only contribution to RRF is its ORDER"
    )
    assert int(max_tuples) == 20_000


@pytest.mark.usefixtures("_fresh_probe")
async def test_the_setting_is_local_and_does_not_leak_to_the_next_transaction(
    maker: Any, project_ids: list[UUID]
) -> None:
    """`is_local => true`. A search must not reconfigure the pooled connection.

    A session-level `SET` survives the checkin and every later query on that
    connection runs at a scan width it never asked for — including writes,
    including other projects.

    The transaction is **committed**, not rolled back, and that is the whole
    test: Postgres reverts a session-level `SET` when its transaction rolls
    back, so a rollback here would make the transaction-local and session-level
    forms indistinguishable and the assertion would hold either way. Verified —
    with `is_local => false` and a rollback this test still passed.
    """
    async with maker() as session:
        await dense_candidates(
            session,
            project_id=project_ids[TARGET],
            embedding=await _query_vector(session),
            top_k=TOP_K,
        )
        await session.commit()
        ef = (await session.execute(text("show hnsw.ef_search"))).scalar_one()
    assert int(ef) != ef_search_for(fetch_for(TOP_K)), (
        "the search's ef_search outlived its transaction, so the pooled "
        "connection now runs every later query — writes and other projects "
        "included — at a scan width nobody asked for"
    )


async def test_the_lexical_leg_needs_no_vector_configuration(maker: Any) -> None:
    """A lexical-only search must not touch the scan GUCs at all.

    `search_corpus(query_embedding=None)` is the degraded mode a caller with no
    embedder uses. Setting `hnsw.*` there would make keyword search depend on
    the vector extension being installed — which is exactly the coupling that
    took the lexical leg down with the embedder in WS-RS1.
    """
    from aleph_rks.retrieval import search_corpus

    async with maker() as session:
        hits = await search_corpus(
            session,
            project_id=uuid4(),
            query_text="rs6 scan probe",
            query_embedding=None,
            top_k=5,
        )
        ef = (await session.execute(text("show hnsw.ef_search"))).scalar_one()
    assert hits == []
    assert int(ef) == 40, "the lexical leg configured a vector scan it never runs"
