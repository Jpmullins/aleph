"""Corpus-wide retrieval: across sources, inside one project, capped per source.

The defect this pins is a *missing* line rather than a wrong one. `document_chunks`
was documented "INTRA-SOURCE DESCENT ONLY", and the only entry point took a
`source_id` — so the store that held every ingested passage could be asked
"what else does document 47 say?" and could not be asked "which of my documents
talks about this?". The wiki index that was supposed to answer the second
question covered titles, summaries and aliases but never page bodies. Between
them the system had no way to find text by the words it was written in.

`search_corpus` removes the predicate. Removing it makes four things load-bearing
that were previously moot, and each has its own test below:

* **Across sources.** The result may span documents. A test that only asserts
  "hits came back" passes just as well against the old single-source search, so
  the assertion here counts *distinct* `source_id`s.
* **Inside one project.** Once a search is no longer pinned to a document the
  caller named, project scope is the only thing standing between two tenants'
  corpora. This is the security-relevant property.
* **Capped per source.** Nine matching chunks in one long document would
  otherwise fill an eight-slot window, and the answer would be built from one
  document's view of the question while looking like a corpus-wide search.
* **Degrading honestly.** `query_embedding=None` means "no dense leg", not "a
  dense leg with a meaningless vector".

Everything is seeded directly: chunk rows with hand-written vectors, no embedder
and no gateway. The fixture is deliberately small and its distances are
one-hot, so the ordering assertions are about the fusion and the filters, not
about whether a real embedding model happens to rank two sentences a particular
way. That is the limit of this file: it proves the retrieval *plumbing*, and
says nothing about retrieval quality. The quality number lives in
`aleph_evals.retrieval_eval`.

Two things here are deliberately **not** covered, so nobody reads a green run as
covering them. First, the OR'd `tsquery`: swapping `or_tsquery` back for
`plainto_tsquery` leaves every test below passing, because the dense leg returns
every embedded chunk in scope regardless of distance and carries the fixture on
its own. That property belongs to `test_retrieval_finds_body_text.py`, which
asks a natural-language question and expects an answer. Second, ranking order —
nothing asserts that hit *n* outranks hit *n+1*.
"""

from __future__ import annotations

import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aleph_rks.models import EMBEDDING_DIM, DocumentChunk
from aleph_rks.retrieval import DEFAULT_PER_SOURCE_CAP, descend_into_source, search_corpus

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
DEFAULT_URL = "postgresql+asyncpg://aleph:aleph@localhost:5432/aleph"

#: Two words that occur nowhere else in the database, so a hit on them is
#: evidence the seeded chunk was retrieved rather than evidence the query
#: matched something generic in whatever corpus this database already holds.
MARKER = "tardigrade cryobiosis"

#: A question, not a keyword list — `or_tsquery` has to widen it. Its stems are
#: `tardigrad | cryobiosi | surviv`; nothing in the distractor chunk shares one.
QUERY = "tardigrade cryobiosis survival"
QUERY_WORDS = ("tardigrade", "cryobiosis", "surviv")

#: Matches no chunk lexically, so a search using it sees only the dense leg.
#: That is the only way to observe the dense leg's filters from the outside.
NO_LEXICAL_MATCH = "xylophone quixotic zeppelin"


def _unit_vector(axis: int) -> list[float]:
    """A one-hot vector of the column's width.

    Cosine distance between two different axes is exactly 1 and between the same
    axis exactly 0, so "which chunk is nearest" is a fact about the fixture
    rather than a fact about floating point.
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[axis] = 1.0
    return vector


QUERY_VECTOR = _unit_vector(0)
OFF_AXIS_VECTOR = _unit_vector(1)


@dataclass
class Corpus:
    """Ids of everything the fixture seeded, so tests can name rows exactly."""

    project_a: uuid.UUID
    project_b: uuid.UUID
    alpha: uuid.UUID
    beta: uuid.UUID
    long: uuid.UUID
    unembedded: uuid.UUID
    distractor: uuid.UUID
    leak: uuid.UUID
    long_chunk_count: int
    unembedded_chunk: uuid.UUID = field(default_factory=uuid.uuid4)
    distractor_chunk: uuid.UUID = field(default_factory=uuid.uuid4)
    leak_chunk: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def embedded_chunks_in_a(self) -> int:
        """alpha + beta + every chunk of `long` + the distractor. Not the
        un-embedded one — that is the point of it."""
        return 2 + self.long_chunk_count + 1


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test.

    Per-test rather than session-scoped for the same reason as
    `tests/integration/conftest.py`: a session-scoped async engine binds its
    asyncpg pool to the first test's event loop, and every later test then fails
    with "attached to a different loop".
    """
    eng = create_async_engine(os.environ.get("DATABASE_URL", DEFAULT_URL), poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def maker(engine: AsyncEngine) -> Callable[[], AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _add_source(session: AsyncSession, project_id: uuid.UUID, title: str) -> uuid.UUID:
    """Raw SQL, matching `tests/integration/test_chunk_embed_degrades.py`.

    Retrieval never joins `sources` — it reads `document_chunks` alone — so these
    rows are realism rather than a requirement. They are here so the fixture is a
    corpus a person could have ingested, and so a future join has something to
    land on.
    """
    source_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO sources (id, project_id, connector_kind, title, short_id, status,"
            " source_metadata_jsonb, created_by)"
            " VALUES (:id, :pid, 'upload', :title, :short, 'normalized', '{}', :actor)"
        ),
        {
            "id": source_id,
            "pid": project_id,
            "title": title,
            "short": f"s{uuid.uuid4().hex[:8]}",
            "actor": ACTOR,
        },
    )
    return source_id


def _chunk(
    *,
    chunk_id: uuid.UUID,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    ordinal: int,
    body: str,
    embedding: list[float] | None,
) -> DocumentChunk:
    return DocumentChunk(
        id=chunk_id,
        project_id=project_id,
        source_id=source_id,
        normalized_document_id=uuid.uuid4(),
        ordinal=ordinal,
        text=body,
        text_tsv="",  # the trigger on document_chunks fills this
        embedding=embedding,
        section_path=None,
        char_start=0,
        char_end=len(body),
        token_count=len(body.split()),
        embedder_model="fixture" if embedding is not None else None,
    )


@pytest.fixture
async def corpus(maker: Callable[[], AsyncSession]) -> AsyncIterator[Corpus]:
    """Six sources across two projects, all matching the same query.

    Shaped so each property below can fail on its own:

    * `alpha` and `beta` are two short documents that both match — the
      across-sources fact needs at least two.
    * `long` is one document with six matching chunks — more than
      `DEFAULT_PER_SOURCE_CAP`, so an uncapped search visibly floods.
    * `unembedded` holds a chunk with a NULL vector, the state every chunk is in
      between being written and being embedded.
    * `distractor` matches nothing lexically but carries a real vector, so it is
      reachable only through the dense leg.
    * `leak` lives in the other project and matches the query perfectly. It must
      never come back.
    """
    corpus = Corpus(
        project_a=uuid.uuid4(),
        project_b=uuid.uuid4(),
        alpha=uuid.uuid4(),
        beta=uuid.uuid4(),
        long=uuid.uuid4(),
        unembedded=uuid.uuid4(),
        distractor=uuid.uuid4(),
        leak=uuid.uuid4(),
        long_chunk_count=6,
    )
    async with maker() as session:
        corpus.alpha = await _add_source(session, corpus.project_a, "Alpha field notes")
        corpus.beta = await _add_source(session, corpus.project_a, "Beta field notes")
        corpus.long = await _add_source(session, corpus.project_a, "The long review")
        corpus.unembedded = await _add_source(session, corpus.project_a, "Just ingested")
        corpus.distractor = await _add_source(session, corpus.project_a, "Budget memo")
        corpus.leak = await _add_source(session, corpus.project_b, "Another tenant's notes")

        session.add(
            _chunk(
                chunk_id=uuid.uuid4(),
                project_id=corpus.project_a,
                source_id=corpus.alpha,
                ordinal=0,
                body=f"Alpha reports {MARKER} after eleven weeks of desiccation.",
                embedding=QUERY_VECTOR,
            )
        )
        session.add(
            _chunk(
                chunk_id=uuid.uuid4(),
                project_id=corpus.project_a,
                source_id=corpus.beta,
                ordinal=0,
                body=f"Beta disputes the {MARKER} result and reports no survival.",
                embedding=QUERY_VECTOR,
            )
        )
        for ordinal in range(corpus.long_chunk_count):
            session.add(
                _chunk(
                    chunk_id=uuid.uuid4(),
                    project_id=corpus.project_a,
                    source_id=corpus.long,
                    ordinal=ordinal,
                    body=f"Section {ordinal} of the long review restates {MARKER} again.",
                    embedding=QUERY_VECTOR,
                )
            )
        session.add(
            _chunk(
                chunk_id=corpus.unembedded_chunk,
                project_id=corpus.project_a,
                source_id=corpus.unembedded,
                ordinal=0,
                body=f"Newly ingested, not yet embedded: {MARKER} survival curves.",
                embedding=None,
            )
        )
        session.add(
            _chunk(
                chunk_id=corpus.distractor_chunk,
                project_id=corpus.project_a,
                source_id=corpus.distractor,
                ordinal=0,
                body="Quarterly budget allocations, itemised by department.",
                embedding=OFF_AXIS_VECTOR,
            )
        )
        session.add(
            _chunk(
                chunk_id=corpus.leak_chunk,
                project_id=corpus.project_b,
                source_id=corpus.leak,
                ordinal=0,
                body=f"Another tenant's private note about {MARKER} survival.",
                embedding=QUERY_VECTOR,
            )
        )
        await session.commit()

    yield corpus

    # Explicit, project-scoped teardown. Named tables rather than a truncate:
    # DATABASE_URL is documented to point at the running compose Postgres, and a
    # tidy-everything teardown there is a data-loss bug waiting for its first
    # victim. `action_ledger_events` is not touched — it carries an append-only
    # trigger, and nothing here writes to it anyway.
    async with maker() as session:
        for project_id in (corpus.project_a, corpus.project_b):
            await session.execute(
                text("DELETE FROM document_chunks WHERE project_id = :pid"), {"pid": project_id}
            )
            await session.execute(
                text("DELETE FROM sources WHERE project_id = :pid"), {"pid": project_id}
            )
        await session.commit()


async def test_one_query_returns_chunks_from_more_than_one_source(
    maker: Callable[[], AsyncSession], corpus: Corpus
) -> None:
    """The single-source predicate is gone: one query spans documents.

    Counting distinct sources is the whole assertion. "Some hits came back" was
    true of the old intra-source search too, so it would not have failed.
    """
    async with maker() as session:
        hits = await search_corpus(
            session,
            project_id=corpus.project_a,
            query_text=QUERY,
            query_embedding=QUERY_VECTOR,
            top_k=8,
        )

    assert hits, "corpus-wide search returned nothing at all"
    sources = {hit.source_id for hit in hits}
    assert len(sources) >= 2, (
        f"every hit came from one source ({sources}) — this is the intra-source "
        "search wearing a corpus-wide name"
    )
    assert corpus.alpha in sources
    assert corpus.beta in sources


async def test_a_chunk_in_another_project_is_never_returned(
    maker: Callable[[], AsyncSession], corpus: Corpus
) -> None:
    """Project scope, on both legs independently.

    Once a search is not pinned to a document the caller named, this predicate
    is the only thing separating two tenants' corpora. The leaked chunk is
    seeded to match the query *better* than anything in project A — same marker,
    same vector — so if scope were dropped from either leg it would rank near
    the top rather than hide in the tail.

    Three searches, because one would not do it: a hybrid search, a lexical-only
    search (`query_embedding=None`, which never touches the dense leg), and a
    dense-only search (a query text that matches nothing, so the lexical leg is
    empty). Dropping the scope from either leg alone fails at least one of them.
    """
    async with maker() as session:
        hybrid = await search_corpus(
            session,
            project_id=corpus.project_a,
            query_text=QUERY,
            query_embedding=QUERY_VECTOR,
            top_k=20,
            per_source_cap=None,
        )
        lexical_only = await search_corpus(
            session,
            project_id=corpus.project_a,
            query_text=QUERY,
            query_embedding=None,
            top_k=20,
            per_source_cap=None,
        )
        dense_only = await search_corpus(
            session,
            project_id=corpus.project_a,
            query_text=NO_LEXICAL_MATCH,
            query_embedding=QUERY_VECTOR,
            top_k=20,
            per_source_cap=None,
        )
        # The same row, searched from its own project. Without this the three
        # assertions above would also pass against a fixture that never wrote
        # the leaked chunk at all.
        from_own_project = await search_corpus(
            session,
            project_id=corpus.project_b,
            query_text=QUERY,
            query_embedding=QUERY_VECTOR,
            top_k=20,
            per_source_cap=None,
        )

    for label, hits in (
        ("hybrid", hybrid),
        ("lexical-only", lexical_only),
        ("dense-only", dense_only),
    ):
        assert hits, f"the {label} search returned nothing, so it proves nothing"
        assert corpus.leak_chunk not in {hit.chunk_id for hit in hits}, (
            f"the {label} leg returned another project's chunk"
        )
        assert corpus.leak not in {hit.source_id for hit in hits}

    assert corpus.leak_chunk in {hit.chunk_id for hit in from_own_project}, (
        "the leaked chunk is not findable from its own project either — the "
        "fixture is broken, not the scoping"
    )


async def test_a_single_long_source_cannot_fill_the_result_window(
    maker: Callable[[], AsyncSession], corpus: Corpus
) -> None:
    """`per_source_cap` caps, and it is the cap doing it rather than the corpus.

    The uncapped search is half the test: without it, a fixture where the long
    source simply never won would pass the capped assertion while proving
    nothing.
    """
    async with maker() as session:
        uncapped = await search_corpus(
            session,
            project_id=corpus.project_a,
            query_text=QUERY,
            query_embedding=QUERY_VECTOR,
            top_k=8,
            per_source_cap=None,
        )
        defaulted = await search_corpus(
            session,
            project_id=corpus.project_a,
            query_text=QUERY,
            query_embedding=QUERY_VECTOR,
            top_k=8,
        )
        capped_at_one = await search_corpus(
            session,
            project_id=corpus.project_a,
            query_text=QUERY,
            query_embedding=QUERY_VECTOR,
            top_k=8,
            per_source_cap=1,
        )

    assert Counter(hit.source_id for hit in uncapped)[corpus.long] > DEFAULT_PER_SOURCE_CAP, (
        "the long source does not flood an uncapped window, so the capped "
        "assertions below would hold for the wrong reason"
    )
    assert Counter(hit.source_id for hit in defaulted)[corpus.long] <= DEFAULT_PER_SOURCE_CAP
    assert all(count <= 1 for count in Counter(hit.source_id for hit in capped_at_one).values())
    # Project A has five sources and a cap of one, so the eight-slot window comes
    # back holding five hits, one per source — filled by diversity rather than by
    # one document. Five and not four: the distractor matches nothing lexically
    # but carries a vector, so the dense leg contributes it too.
    assert len(capped_at_one) == 5
    assert len({hit.source_id for hit in capped_at_one}) == 5


async def test_descending_into_a_source_stays_in_it_and_is_not_capped(
    maker: Callable[[], AsyncSession], corpus: Corpus
) -> None:
    """The other entry point: pinned to one document, and deliberately uncapped.

    A per-source cap inside a single-source search would cap the search itself,
    which is why `descend_into_source` passes `per_source_cap=None`. Asking for
    more chunks than the cap and getting them is what proves it.
    """
    async with maker() as session:
        hits = await descend_into_source(
            session,
            project_id=corpus.project_a,
            source_id=corpus.long,
            query_text=QUERY,
            query_embedding=QUERY_VECTOR,
            top_k=corpus.long_chunk_count,
        )

    assert {hit.source_id for hit in hits} == {corpus.long}, "descent leaked into other sources"
    assert len(hits) == corpus.long_chunk_count > DEFAULT_PER_SOURCE_CAP, (
        "descent was truncated at the per-source cap, which would make it "
        "impossible to read more than three chunks of any document"
    )


async def test_no_query_embedding_runs_the_lexical_leg_only(
    maker: Callable[[], AsyncSession], corpus: Corpus
) -> None:
    """The degraded mode a caller with no embedder uses — and it is not a zero vector.

    `query_embedding=None` must mean "skip the dense leg". Passing a zero vector
    instead looks equivalent: it is not. Cosine distance to the zero vector is
    degenerate, so the dense leg comes back with an arbitrary page of rows and
    RRF fuses that noise as if it were a ranking. The distractor chunk shares no
    term with the query and is reachable only through the dense leg, so it is
    the witness: absent under `None`, present under a zero vector.
    """
    zero_vector = [0.0] * EMBEDDING_DIM
    async with maker() as session:
        lexical_only = await search_corpus(
            session,
            project_id=corpus.project_a,
            query_text=QUERY,
            query_embedding=None,
            top_k=20,
            per_source_cap=None,
        )
        zeroed = await search_corpus(
            session,
            project_id=corpus.project_a,
            query_text=QUERY,
            query_embedding=zero_vector,
            top_k=20,
            per_source_cap=None,
        )

    assert lexical_only, "keyword search returned nothing without an embedder"
    assert len({hit.source_id for hit in lexical_only}) >= 2, (
        "the degraded mode is still corpus-wide, not one source"
    )
    for hit in lexical_only:
        assert any(word in hit.text.lower() for word in QUERY_WORDS), (
            f"lexical-only returned a chunk matching no query term: {hit.text!r}"
        )
    assert corpus.distractor_chunk not in {hit.chunk_id for hit in lexical_only}

    assert corpus.distractor_chunk in {hit.chunk_id for hit in zeroed}, (
        "a zero vector behaved like no vector here, so this fixture cannot tell "
        "the two apart — the contrast below is what makes `None` meaningful"
    )


async def test_an_unembedded_chunk_is_lexically_findable_and_absent_from_the_dense_leg(
    maker: Callable[[], AsyncSession], corpus: Corpus
) -> None:
    """A chunk is written before it is embedded, so NULL vectors are normal.

    Both halves matter. It must be *findable*, because the lexical leg needs no
    model and an embedder outage must not empty the index. And it must be
    *excluded from the dense leg*, because ordering by cosine distance over NULL
    is undefined rather than merely unhelpful — without the `IS NOT NULL`
    predicate a degraded index makes the dense leg an arbitrary page of rows.

    The dense-only half is observed with a query text that matches nothing, so
    every hit that comes back came from the dense leg. `top_k` is larger than the
    corpus on purpose: NULLs sort last, so a small window would hide the bug by
    accident rather than exclude it on purpose.
    """
    async with maker() as session:
        lexical = await search_corpus(
            session,
            project_id=corpus.project_a,
            query_text=QUERY,
            query_embedding=None,
            top_k=20,
            per_source_cap=None,
        )
        dense_only = await search_corpus(
            session,
            project_id=corpus.project_a,
            query_text=NO_LEXICAL_MATCH,
            query_embedding=QUERY_VECTOR,
            top_k=20,
            per_source_cap=None,
        )

    assert corpus.unembedded_chunk in {hit.chunk_id for hit in lexical}, (
        "a chunk with no embedding was unfindable — an embedder outage would "
        "take keyword search down with it"
    )
    assert corpus.unembedded_chunk not in {hit.chunk_id for hit in dense_only}, (
        "a NULL vector was ranked by cosine distance"
    )
    assert len(dense_only) == corpus.embedded_chunks_in_a, (
        "the dense leg returned a different number of rows than project A has "
        "embedded chunks, so the exclusion above may be an accident of the window"
    )
