"""What ingest STORES must be the exact slice its offsets name.

`packages/aleph-rks/tests/test_chunk_offsets.py` asserts
`markdown[c.char_start:c.char_end] == c.text` over the objects `chunk_markdown`
returns. That is a property of the chunker, and it was the only place the claim
was checked. Nothing looked at what `_write_chunks` actually put in the
database.

The gap is not theoretical. Injecting a title prefix into the STORED text —

    text=embedding_text(chunk_text=c.text, title="MUTANT TITLE", contextual=True)

— is precisely the defect WS-RS6's contextual-embedding work risks: every
`char_start`/`char_end` out by the prefix length, so every grounding highlight
in the product points at the wrong words while looking entirely confident.
That mutation left **1,485 unit tests and 246 integration tests green.**

The distinction this file pins is narrow and load-bearing: the string sent to
the EMBEDDER may carry context (that is the whole point of contextual
embedding), and the string STORED may not, because the offsets are into the
source document. `embedding_text` is the seam, and it must not leak into the
row.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from aleph_rks.indexing import _write_chunks
from aleph_rks.models import DocumentChunk, NormalizedDocument

pytestmark = pytest.mark.integration

#: Sections, a table, a code fence and a quote — the shapes whose offsets are
#: easiest to get wrong, and the ones a real paper contains.
MARKDOWN = """# Attention Is All You Need

## Abstract

We propose a new simple network architecture, the Transformer, based solely on
attention mechanisms, dispensing with recurrence and convolutions entirely.

## Model Architecture

### Encoder

The encoder is composed of a stack of N = 6 identical layers.

| head | dim |
|---|---|
| 8 | 64 |

```python
def attention(q, k, v):
    return softmax(q @ k.T / sqrt(d)) @ v
```

> Self-attention, sometimes called intra-attention, relates different positions
> of a single sequence.

### Decoder

The decoder is also composed of a stack of N = 6 identical layers, with a third
sub-layer performing multi-head attention over the encoder output.
"""


def _database_url() -> str:
    import os

    url = os.environ.get("ALEPH_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("ALEPH_TEST_DATABASE_URL or DATABASE_URL is required")
    return url


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(_database_url())
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def written(maker: Any) -> AsyncIterator[tuple[uuid.UUID, list[DocumentChunk]]]:
    """Chunks written by the PRODUCTION path, read back from the database."""
    from aleph_core.ids import uuid7

    project_id = uuid7()
    source_id = uuid7()
    # The markdown lives in the asset store, not on the row — `_write_chunks`
    # is handed it separately. Everything here is a real column.
    normalized = NormalizedDocument(
        id=uuid7(),
        project_id=project_id,
        source_id=source_id,
        source_version_id=uuid7(),
        markdown_uri="file:///dev/null",
        parser="test",
        parser_version="0",
        char_count=len(MARKDOWN),
        token_count=len(MARKDOWN) // 4,
        created_by=uuid.UUID(int=0),
    )
    async with maker() as session:
        # The source and project rows are not needed: `document_chunks` carries
        # no FK to either, and inventing them would test the fixtures.
        session.add(normalized)
        await session.flush()
        count = await _write_chunks(session, normalized=normalized, markdown=MARKDOWN)
        await session.commit()
    # More than one, so the offsets differ and a test that only ever sees
    # chunk zero cannot pass by accident.
    assert count >= 3, f"the fixture produced {count} chunks — too few to test spans over"

    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.normalized_document_id == normalized.id)
                    .order_by(DocumentChunk.ordinal)
                )
            )
            .scalars()
            .all()
        )
    try:
        yield project_id, rows
    finally:
        async with maker() as session:
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.normalized_document_id == normalized.id)
            )
            await session.execute(
                text("delete from normalized_documents where id = :i"), {"i": normalized.id}
            )
            await session.commit()


async def test_every_stored_chunk_is_the_exact_slice_its_offsets_name(
    written: tuple[uuid.UUID, list[DocumentChunk]],
) -> None:
    """THE invariant. Asserted on the row, not on the chunker's return value."""
    _project_id, rows = written
    assert rows, "no chunks were written at all"
    wrong: list[str] = []
    for row in rows:
        sliced = MARKDOWN[row.char_start : row.char_end]
        if sliced != row.text:
            wrong.append(
                f"ordinal {row.ordinal}: stored {row.text[:40]!r} but "
                f"markdown[{row.char_start}:{row.char_end}] is {sliced[:40]!r}"
            )
    assert not wrong, (
        f"{len(wrong)} of {len(rows)} stored chunks do not match the source at "
        "their own offsets, so every grounding highlight over them points at the "
        "wrong words:\n  " + "\n  ".join(wrong[:5])
    )


async def test_no_stored_chunk_carries_the_contextual_prefix(
    written: tuple[uuid.UUID, list[DocumentChunk]],
) -> None:
    """The narrow claim, stated separately from the offsets.

    Asserted by asking `embedding_text` what a prefixed string LOOKS like and
    checking no row is one, rather than by guessing at its shape. A first
    version compared against `title + "\n\n"` and missed the real prefix,
    which is `title + "\n" + section_path + "\n\n"` — a test for a defect
    whose shape it had assumed instead of read.

    Scope, stated because it is narrower than it looks: this catches a prefix
    built from THIS document's title, which is the realistic shape. A prefix
    from some other string slips past it. The slice test above is the general
    guard and catches any prefix at all; this one exists because "the row was
    stored with the embedding prefix" is a far more legible failure message
    than "the row does not match its offsets".
    """
    from aleph_rks.indexing import embedding_text

    _project_id, rows = written
    title_line = MARKDOWN.splitlines()[0]
    for row in rows:
        prefixed = embedding_text(
            chunk_text=row.text,
            title=title_line,
            section_path=row.section_path,
            contextual=True,
        )
        prefix = prefixed[: len(prefixed) - len(row.text)]
        if not prefix:
            continue  # nothing would be added for this row anyway
        assert not row.text.startswith(prefix), (
            f"ordinal {row.ordinal} was stored carrying the embedding prefix "
            f"{prefix!r}; that string belongs to the embedder, not to the row, "
            "and every char_start/char_end over it is out by its length"
        )
        # And the general form: the row must not begin with the document title
        # by any route.
        assert not row.text.startswith(title_line), (
            f"ordinal {row.ordinal} begins with the document title"
        )


async def test_the_offsets_are_monotonic_and_within_the_document(
    written: tuple[uuid.UUID, list[DocumentChunk]],
) -> None:
    """A span outside the document, or one that runs backwards, is not a span.

    Without this, storing `char_start=char_end=0` and `text=""` for every chunk
    would satisfy the slice test above.
    """
    _project_id, rows = written
    for row in rows:
        assert 0 <= row.char_start < row.char_end <= len(MARKDOWN), (
            f"ordinal {row.ordinal} spans [{row.char_start}, {row.char_end}) of a "
            f"{len(MARKDOWN)}-character document"
        )
        assert row.text.strip(), f"ordinal {row.ordinal} stored an empty chunk"
    starts = [r.char_start for r in rows]
    assert starts == sorted(starts), f"chunks are not in document order: {starts}"
