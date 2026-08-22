"""The chain the project claims to have, end to end, against a real database.

claim → citation → chunk → exact character span in the source document.

Every link existed and the chain had never been assembled: 786 claims with no
`claim_key`, 786 citations with no quote and no chunk, zero edges. This is
WS-RS8 criteria 2 and 3 — that a claim written by ingest carries a quote and a
chunk, and that re-ingesting the same source does not double the claims.

The spine tests already cover `BeliefService` in isolation with fixture text.
What is new here is REAL rows: chunks with real `char_start` offsets, and the
assertion that `document[char_start:char_end] == quote` on the document those
chunks were cut from.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_db.repos.ledger import LedgerWriter
from aleph_rks.models import DocumentChunk, NormalizedDocument
from aleph_security.principal import Principal
from aleph_wiki.belief_service import BeliefService
from aleph_wiki.claim_extraction import ChunkRef, extract_claims
from aleph_wiki.models import Citation, WikiClaim

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

DOCUMENT = (
    "# Field notes\n\n"
    "Sedimentation rates rose sharply after the 8.2 ka event across the basin.\n\n"
    "Radiocarbon dates bracket the transition to within a few decades.\n\n"
    "Salinity fell over the same interval, though the record is sparse.\n"
)

QUOTE_A = "Sedimentation rates rose sharply after the 8.2 ka event across the basin."
QUOTE_B = "Radiocarbon dates bracket the transition to within a few decades."


def _agent() -> Principal:
    return Principal(
        user_id=ACTOR,
        subject="claim-extraction-agent",
        email="claim-extraction@example.test",
        actor_kind="aleph_agent",
    )


async def _seed(session: AsyncSession, project_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """One source, one document, and chunks whose offsets are REAL.

    `char_start` is computed with `DOCUMENT.index(...)` rather than hardcoded,
    so the fixture cannot drift from the text it describes — which is the way
    an offset test quietly stops testing offsets.
    """
    source_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO sources (id, project_id, connector_kind, title, short_id, status,"
            " source_metadata_jsonb, created_by)"
            " VALUES (:id, :pid, 'upload', 'Field notes', :short, 'normalized', '{}', :actor)"
        ),
        {"id": source_id, "pid": project_id, "short": f"s{uuid.uuid4().hex[:8]}", "actor": ACTOR},
    )
    normalized_id = uuid.uuid4()
    session.add(
        NormalizedDocument(
            id=normalized_id,
            project_id=project_id,
            source_id=source_id,
            source_version_id=uuid.uuid4(),
            markdown_uri="fixture://field-notes.md",
            parser="fixture",
            parser_version="1",
            char_count=len(DOCUMENT),
            token_count=len(DOCUMENT) // 4,
            created_by=ACTOR,
        )
    )
    for ordinal, quote in enumerate((QUOTE_A, QUOTE_B)):
        start = DOCUMENT.index(quote)
        session.add(
            DocumentChunk(
                id=uuid.uuid4(),
                project_id=project_id,
                source_id=source_id,
                normalized_document_id=normalized_id,
                ordinal=ordinal,
                text=quote,
                text_tsv=func.to_tsvector("english", quote),
                char_start=start,
                char_end=start + len(quote),
                token_count=len(quote) // 4,
            )
        )

    page_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO wiki_pages (id, project_id, title, slug, page_kind, status,"
            " created_by) VALUES (:id, :pid, 'Field notes', :slug, 'source', 'draft', :actor)"
        ),
        {
            "id": page_id,
            "pid": project_id,
            "slug": f"field-notes-{uuid.uuid4().hex[:6]}",
            "actor": ACTOR,
        },
    )
    await session.commit()
    return source_id, page_id


async def _chunks(session: AsyncSession, source_id: uuid.UUID) -> list[ChunkRef]:
    rows = list(
        (
            await session.execute(
                select(DocumentChunk.id, DocumentChunk.text, DocumentChunk.char_start)
                .where(DocumentChunk.source_id == source_id)
                .order_by(DocumentChunk.ordinal)
            )
        ).all()
    )
    return [ChunkRef(chunk_id=cid, text=t, char_start=cs or 0) for cid, t, cs in rows]


def _model(claims: list[dict[str, Any]]) -> Any:
    async def call(*, system_prompt: str, user_payload: str, purpose: str) -> dict[str, Any]:
        return {"claims": claims}

    return call


async def _run(
    maker: Callable[[], AsyncSession],
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    page_id: uuid.UUID,
    claims: list[dict[str, Any]],
) -> tuple[int, int]:
    """Exactly the two calls `_node_claim_extraction` makes, in the same order."""
    async with maker() as session:
        chunks = await _chunks(session, source_id)
    drafts = await extract_claims(
        chunks, source_id=source_id, page_id=page_id, call_json=_model(claims)
    )
    written = rejected = 0
    async with maker() as session:
        svc = BeliefService(session)
        ledger = LedgerWriter(session)
        for draft in drafts:
            result = await svc.upsert_claim(
                principal=_agent(), ledger=ledger, project_id=project_id, draft=draft
            )
            written += result.citations_written
            rejected += len(result.citations_rejected)
        await session.commit()
    return written, rejected


async def test_an_ingested_claim_carries_a_quote_a_chunk_and_an_exact_span(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Criterion 2, on real rows."""
    async with maker() as session:
        source_id, page_id = await _seed(session, committed_project)

    written, rejected = await _run(
        maker,
        committed_project,
        source_id,
        page_id,
        [
            {"text": "Rates rose after the 8.2 ka event", "passage": 1, "quote": QUOTE_A},
            {"text": "The transition is dated to within decades", "passage": 2, "quote": QUOTE_B},
        ],
    )
    assert written == 2
    assert rejected == 0

    async with maker() as session:
        rows = list(
            (await session.execute(select(Citation).where(Citation.source_id == source_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 2
    for row in rows:
        assert row.verbatim is True
        assert row.quote
        assert row.chunk_id is not None
        assert row.chunk_ids, "chunk_ids is the wire format the grounding surface reads"
        assert row.char_start is not None and row.char_end is not None
        # The whole point: the span addresses the DOCUMENT, not the chunk.
        assert DOCUMENT[row.char_start : row.char_end] == row.quote


async def test_a_fabricated_quote_writes_no_citation(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Criterion 5, through the ingest path rather than in isolation.

    The model's quote is plausible and close — a paraphrase of a sentence that
    IS in the source. That is the realistic failure, and it is the one a
    substring check catches and a similarity threshold would not.
    """
    async with maker() as session:
        source_id, page_id = await _seed(session, committed_project)

    written, rejected = await _run(
        maker,
        committed_project,
        source_id,
        page_id,
        [
            {
                "text": "Rates rose after the 8.2 ka event",
                "passage": 1,
                "quote": "Sedimentation rates increased sharply after the 8.2 ka event.",
            }
        ],
    )
    assert written == 0
    assert rejected == 1

    async with maker() as session:
        count = len(
            list(
                (await session.execute(select(Citation).where(Citation.source_id == source_id)))
                .scalars()
                .all()
            )
        )
    assert count == 0


async def test_re_ingesting_the_same_source_does_not_double_the_claims(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Criterion 3: claims have a stable identity, so a re-run converges.

    All 786 live claims have `claim_key = NULL`, which means every re-ingest
    appends. A knowledge base that grows on re-reading the same document is
    counting its own passes.
    """
    async with maker() as session:
        source_id, page_id = await _seed(session, committed_project)

    payload = [{"text": "Rates rose after the 8.2 ka event", "passage": 1, "quote": QUOTE_A}]
    await _run(maker, committed_project, source_id, page_id, payload)
    async with maker() as session:
        first = list(
            (
                await session.execute(
                    select(WikiClaim).where(WikiClaim.project_id == committed_project)
                )
            )
            .scalars()
            .all()
        )
    assert len(first) == 1
    assert first[0].claim_key, "a claim with no key cannot be re-identified"

    await _run(maker, committed_project, source_id, page_id, payload)
    async with maker() as session:
        second = list(
            (
                await session.execute(
                    select(WikiClaim).where(WikiClaim.project_id == committed_project)
                )
            )
            .scalars()
            .all()
        )
    assert len(second) == 1, "re-ingesting doubled the claims"
    assert second[0].id == first[0].id
