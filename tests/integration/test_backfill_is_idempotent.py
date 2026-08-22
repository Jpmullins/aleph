"""The repair pass can be run twice — WS-RS1 criterion 6, against Postgres.

`backfill_unindexed_for_project` exists because 45 normalized documents on the
deployed stack had no chunks at all. Its docstring states the contract in one
sentence — *"Running it a second time with nothing new to do returns (0, 0)"* —
and nothing checked it. A repair you cannot run twice is a repair nobody runs
at all: the operator has no way to tell "already done" from "about to duplicate
every chunk in the corpus", so they do not run it.

The second property is the one that is easy to get wrong. A document that came
out ``lexical_only`` — chunks written, embedder unreachable — is DONE as far as
this pass is concerned, because the selection is "has no chunk rows". Repairing
the dense leg belongs to ``reembed_for_project``. A backfill that re-attempted
those would re-index the whole corpus on every run for as long as the gateway
was down, which is exactly when someone reaches for it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_db.models.model_profile import ModelProfile
from aleph_rks.backfill import backfill_unindexed_for_project, unindexed_document_ids
from aleph_rks.models import DocumentChunk, NormalizedDocument
from aleph_security.principal import Principal

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
EMBEDDER = "bedrock-titan-embed-text"
DIM = 1024

MARKDOWN = """# Survey notes

The winter transect recorded eleven separate returns, none of which repeated.

## Method

Samples were collected weekly over a period of eleven weeks, then pooled.
"""


class _AssetStore:
    def get(self, _uri: str) -> bytes:
        return MARKDOWN.encode()


class _Embedder:
    """Stands in for LiteLLMClient. Counts calls so a second pass that
    re-indexes is visible as spend, not only as rows."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, **kw: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(
            embeddings=[[0.01] * DIM for _ in kw["input"]],
            model=EMBEDDER,
            input_tokens=8,
            cost_usd="0",
        )


class _DeadEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, **_kw: Any) -> Any:
        self.calls += 1
        msg = "litellm.APIConnectionError: the embedder is down"
        raise RuntimeError(msg)


def _principal() -> Principal:
    return Principal(
        user_id=ACTOR, subject="backfill", email="backfill@example.test", actor_kind="aleph_agent"
    )


BINDINGS: dict[str, Any] = {"embedding": {"model": EMBEDDER, "provider": "litellm"}}


async def _seed(session: AsyncSession, project_id: uuid.UUID, title: str) -> uuid.UUID:
    """One source and one normalized document with NO chunks — the shape the
    repair pass exists for."""
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
    normalized = NormalizedDocument(
        id=uuid.uuid4(),
        project_id=project_id,
        source_id=source_id,
        source_version_id=uuid.uuid4(),
        markdown_uri=f"fixture://{title}.md",
        parser="fixture",
        parser_version="1",
        char_count=len(MARKDOWN),
        token_count=len(MARKDOWN) // 4,
        created_by=ACTOR,
    )
    session.add(normalized)
    await session.commit()
    return normalized.id


async def _run(
    maker: Callable[[], AsyncSession], project_id: uuid.UUID, embedder: Any
) -> tuple[int, int]:
    async with maker() as s:
        s.add(
            ModelProfile(
                id=uuid.uuid4(),
                name=f"backfill-{uuid.uuid4().hex[:6]}",
                project_id=project_id,
                is_template=False,
                bindings_jsonb=BINDINGS,
                created_by=ACTOR,
            )
        )
        await s.commit()
    return await backfill_unindexed_for_project(
        maker=maker,
        project_id=project_id,
        asset_store=_AssetStore(),
        litellm=embedder,
        principal=_principal(),
        profile_bindings=BINDINGS,
    )


async def _chunk_count(maker: Callable[[], AsyncSession], project_id: uuid.UUID) -> int:
    async with maker() as s:
        return int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.project_id == project_id)
                )
            ).scalar_one()
        )


async def test_a_second_backfill_does_nothing_and_says_so(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The contract in the docstring, measured."""
    async with maker() as s:
        await _seed(s, committed_project, "alpha")
        await _seed(s, committed_project, "beta")

    embedder = _Embedder()
    documents, chunks = await _run(maker, committed_project, embedder)
    assert documents == 2, "the first pass did not index the documents it was given"
    assert chunks > 0
    after_first = await _chunk_count(maker, committed_project)
    assert after_first == chunks
    calls_after_first = embedder.calls

    assert await _run(maker, committed_project, embedder) == (0, 0), (
        "the second pass re-indexed documents that already had chunks"
    )
    assert await _chunk_count(maker, committed_project) == after_first, (
        "the second pass changed the corpus"
    )
    assert embedder.calls == calls_after_first, (
        "the second pass spent money on an embedder it had no work for"
    )


async def test_a_document_left_lexical_only_is_not_re_attempted(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Chunks written, embedder down. That document is DONE for this pass.

    Re-attempting it would mean a backfill run while the gateway is unreachable
    re-indexes the entire corpus on every invocation — and that is precisely
    when an operator runs it. Repairing the dense leg is `reembed_for_project`.
    """
    async with maker() as s:
        await _seed(s, committed_project, "gamma")

    dead = _DeadEmbedder()
    documents, chunks = await _run(maker, committed_project, dead)
    assert documents == 1
    assert chunks > 0, "the embed failure emptied the index instead of degrading"
    assert dead.calls >= 1

    async with maker() as s:
        rows = list(
            (
                await s.execute(
                    select(DocumentChunk).where(DocumentChunk.project_id == committed_project)
                )
            )
            .scalars()
            .all()
        )
    assert rows and all(r.embedding is None for r in rows)

    async with maker() as s:
        assert await unindexed_document_ids(s, project_id=committed_project) == [], (
            "a lexical_only document still counts as unindexed, so every backfill "
            "run re-indexes it for as long as the embedder is down"
        )
    assert await _run(maker, committed_project, dead) == (0, 0)
