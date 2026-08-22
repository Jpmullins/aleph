"""`ocr-required` reaches the index record — WS-RS11 criterion 6.

Three normalizers raise the flag. It lands in
`normalized_documents.quality_flags_jsonb`, one HTTP response repeats it, and
**no code in the tree has ever branched on it**: `grep -rn 'ocr-required'
--include='*.py' .` outside `normalization.py` returned nothing. A producer with
no consumer, which is the defect class `CLAUDE.md` names as this codebase's
dominant one.

The consequence was concrete and worse than a missing feature. A scan produces
no text, so it chunks to zero passages — and the zero-chunk branch wrote
`state="embedded", chunk_count=0, degraded_reason=NULL`. A source nothing can
ever find, recorded as a fully indexed one, sitting green on the pipeline
strip and indistinguishable from a working document.

These tests drive the real `index_normalized_document` and read the real
`retrieval_index_records` row.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_rks.indexing import index_normalized_document
from aleph_rks.models import NormalizedDocument, RetrievalIndexRecord
from aleph_rks.normalization import OCR_REQUIRED
from aleph_security.principal import Principal

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000dd")
BINDINGS: dict[str, Any] = {
    "embedding": {"model": "bedrock-titan-embed-text", "provider": "litellm"}
}


class _AssetStore:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def get(self, _uri: str) -> bytes:
        return self._payload


class _Embedder:
    """Must never be reached: a document with no passages has nothing to
    embed, and calling the gateway for it is billed nothing-for-nothing."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, **_kw: Any) -> Any:  # pragma: no cover - must not run
        self.calls += 1
        msg = "the embedder was called for a document with no chunks"
        raise AssertionError(msg)


def _principal() -> Principal:
    return Principal(
        user_id=ACTOR, subject="ocr", email="ocr@example.test", actor_kind="aleph_agent"
    )


async def _seed(
    session: AsyncSession, project_id: uuid.UUID, *, flags: list[str]
) -> tuple[uuid.UUID, uuid.UUID]:
    source_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO sources (id, project_id, connector_kind, title, short_id, status,"
            " source_metadata_jsonb, created_by)"
            " VALUES (:id, :pid, 'upload', 'Scanned paper', :short, 'normalized', '{}', :actor)"
        ),
        {"id": source_id, "pid": project_id, "short": f"s{uuid.uuid4().hex[:8]}", "actor": ACTOR},
    )
    normalized = NormalizedDocument(
        id=uuid.uuid4(),
        project_id=project_id,
        source_id=source_id,
        source_version_id=uuid.uuid4(),
        markdown_uri="fixture://scan.md",
        parser="pypdf",
        parser_version="pypdf@5",
        char_count=0,
        token_count=0,
        quality_flags_jsonb=flags,
        created_by=ACTOR,
    )
    session.add(normalized)
    await session.commit()
    return normalized.id, source_id


async def _index(maker: Callable[[], AsyncSession], normalized_id: uuid.UUID) -> Any:
    return await index_normalized_document(
        maker=maker,
        normalized_id=normalized_id,
        asset_store=_AssetStore(b""),
        litellm=_Embedder(),
        principal=_principal(),
        profile_bindings=BINDINGS,
        agent_run_id=None,
    )


async def _record(maker: Callable[[], AsyncSession], source_id: uuid.UUID) -> RetrievalIndexRecord:
    async with maker() as s:
        return (
            await s.execute(
                select(RetrievalIndexRecord).where(RetrievalIndexRecord.source_id == source_id)
            )
        ).scalar_one()


async def test_a_scan_is_not_recorded_as_a_finished_index(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The reader, and the reason it is worth having."""
    async with maker() as s:
        normalized_id, source_id = await _seed(s, committed_project, flags=[OCR_REQUIRED])

    outcome = await _index(maker, normalized_id)
    assert outcome.chunk_count == 0
    assert outcome.ok is False, "a document with no passages reported a successful index"

    record = await _record(maker, source_id)
    assert record.state == "lexical_only"
    assert record.degraded_reason is not None
    assert OCR_REQUIRED in record.degraded_reason, (
        "the index record does not say WHY there are no passages, so a scan and "
        f"an empty file look identical: {record.degraded_reason!r}"
    )


async def test_an_empty_document_without_the_flag_says_something_else(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The other half. If every zero-chunk document blamed OCR the flag would
    be decoration — the reason has to distinguish the two causes."""
    async with maker() as s:
        normalized_id, source_id = await _seed(s, committed_project, flags=[])

    outcome = await _index(maker, normalized_id)
    assert outcome.ok is False

    record = await _record(maker, source_id)
    assert record.degraded_reason is not None
    assert OCR_REQUIRED not in record.degraded_reason, (
        "an empty file is blamed on OCR, which is a guess wearing the parser's "
        f"authority: {record.degraded_reason!r}"
    )
