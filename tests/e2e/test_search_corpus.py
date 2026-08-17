"""Corpus-wide hybrid retrieval — the replacement for wiki-first search.

`descend_into_source` was the only entry point: the same dense+lexical retriever,
pinned to one document by a single predicate, because embeddings were forbidden
as first-line retrieval. `search_corpus` removes the predicate. These pin the
properties that makes it first-line-usable.

Embeddings are supplied directly rather than computed, so nothing here calls a
model — the behaviour under test is Postgres and the fusion, both of which are
where the defect lived.
"""

from __future__ import annotations

import pytest

from aleph_core.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DIM = 1024


def vec(*, lead: float = 0.0) -> list[float]:
    """A unit-ish vector whose first component carries the signal."""
    v = [0.0] * DIM
    v[0] = lead
    v[1] = 1.0 - lead
    return v


async def _seed(maker, project_id, rows: list[tuple[str, str, float]]) -> dict[str, object]:
    """rows = [(source_label, chunk_text, embedding_lead)] -> {label: source_id}."""
    from aleph_rks.models import DocumentChunk

    sources: dict[str, object] = {}
    async with maker() as session:
        for ordinal, (label, text, lead) in enumerate(rows):
            source_id = sources.setdefault(label, uuid7())
            session.add(
                DocumentChunk(
                    id=uuid7(),
                    project_id=project_id,
                    source_id=source_id,
                    normalized_document_id=uuid7(),
                    ordinal=ordinal,
                    text=text,
                    text_tsv="",  # the document_chunks_tsvector trigger overrides
                    section_path=None,
                    char_start=0,
                    char_end=len(text),
                    token_count=max(1, len(text.split())),
                    embedding=vec(lead=lead),
                    embedder_model="test-embedder",
                )
            )
        await session.commit()
    return sources


async def test_search_spans_sources(asgi_app) -> None:
    """THE change. One query returns chunks from more than one document."""
    from aleph_rks.retrieval import search_corpus

    maker = asgi_app.state.session_maker
    project_id = uuid7()
    await _seed(
        maker,
        project_id,
        [
            ("alpha", "The vibracorer recovered a sediment core from the shelf.", 0.9),
            ("beta", "Sediment transport on the shelf is storm dominated.", 0.8),
            ("gamma", "Unrelated notes about municipal budgeting.", 0.0),
        ],
    )

    async with maker() as session:
        hits = await search_corpus(
            session,
            project_id=project_id,
            query_text="sediment on the shelf",
            query_embedding=vec(lead=0.85),
            top_k=8,
        )

    found_sources = {h.source_id for h in hits}
    assert len(found_sources) >= 2, (
        f"corpus search returned chunks from {len(found_sources)} source(s); "
        f"it is still pinned to one document"
    )


async def test_a_partial_question_still_retrieves(asgi_app) -> None:
    """Every Postgres tsquery parser ANDs unquoted terms; retrieval must not."""
    from aleph_rks.retrieval import search_corpus

    maker = asgi_app.state.session_maker
    project_id = uuid7()
    await _seed(
        maker,
        project_id,
        [("alpha", "Hummocky cross-stratification indicates storm deposition.", 0.9)],
    )

    async with maker() as session:
        hits = await search_corpus(
            session,
            project_id=project_id,
            # Only some of these words appear in the chunk. Under AND semantics
            # this matches nothing at all.
            query_text="what does hummocky stratification tell us about the depositional setting",
            query_embedding=vec(lead=0.1),  # deliberately a poor dense match
            top_k=8,
        )

    assert hits, "a question sharing only some terms with the text retrieved nothing"


async def test_project_scoping_holds(asgi_app) -> None:
    """A corpus-wide search must not become a cross-tenant search."""
    from aleph_rks.retrieval import search_corpus

    maker = asgi_app.state.session_maker
    mine, theirs = uuid7(), uuid7()
    await _seed(maker, mine, [("a", "sediment core analysis", 0.9)])
    await _seed(maker, theirs, [("b", "sediment core analysis", 0.9)])

    async with maker() as session:
        hits = await search_corpus(
            session,
            project_id=mine,
            query_text="sediment core",
            query_embedding=vec(lead=0.9),
            top_k=8,
        )

    assert hits, "own-project search returned nothing"
    async with maker() as session:
        from sqlalchemy import select

        from aleph_rks.models import DocumentChunk

        other_ids = set(
            (
                await session.execute(
                    select(DocumentChunk.id).where(DocumentChunk.project_id == theirs)
                )
            )
            .scalars()
            .all()
        )
    assert not ({h.chunk_id for h in hits} & other_ids), "search leaked across projects"


async def test_one_source_cannot_fill_the_window(asgi_app) -> None:
    """A long document with many similar chunks must not crowd out every other view."""
    from aleph_rks.retrieval import search_corpus

    maker = asgi_app.state.session_maker
    project_id = uuid7()
    rows = [("hog", f"sediment shelf core passage number {i}", 0.9) for i in range(10)]
    rows.append(("other", "sediment shelf core from an independent survey", 0.88))
    await _seed(maker, project_id, rows)

    async with maker() as session:
        hits = await search_corpus(
            session,
            project_id=project_id,
            query_text="sediment shelf core",
            query_embedding=vec(lead=0.9),
            top_k=6,
        )

    per_source: dict[object, int] = {}
    for hit in hits:
        per_source[hit.source_id] = per_source.get(hit.source_id, 0) + 1
    assert max(per_source.values()) <= 3, f"one source contributed {max(per_source.values())} hits"
    assert len(per_source) >= 2, "the diversity cap did not admit the second source"


async def test_descent_still_pins_to_one_source(asgi_app) -> None:
    """The narrow entry point must stay narrow — it is how a cited doc is read."""
    from aleph_rks.retrieval import descend_into_source

    maker = asgi_app.state.session_maker
    project_id = uuid7()
    sources = await _seed(
        maker,
        project_id,
        [
            ("alpha", "sediment core from the shelf", 0.9),
            ("beta", "sediment core from the basin", 0.9),
        ],
    )

    async with maker() as session:
        hits = await descend_into_source(
            session,
            project_id=project_id,
            source_id=sources["alpha"],  # type: ignore[arg-type]
            query_text="sediment core",
            query_embedding=vec(lead=0.9),
            top_k=8,
        )

    assert hits
    assert {h.source_id for h in hits} == {sources["alpha"]}
