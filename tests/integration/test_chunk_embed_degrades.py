"""A dead embedder degrades to keyword-only search, loudly.

This is the regression test for the single largest live defect this repository
has had: 75 ingested sources, 45 normalized documents, **0** searchable chunks,
and 45 index jobs sitting in ``running`` with no error recorded — all caused by
one model name being wrong by one word.

Three properties, each of which was false:

1. **Chunks exist even when embedding fails.** The lexical leg needs no model,
   so an embedder outage must not be able to empty the index.
2. **The failure is visible.** The run ends ``failed`` with the reason in
   ``error_text``, and the source's index record says ``lexical_only`` and why.
3. **Nothing is billed for a mismatch.** A model whose output width cannot fit
   the column is refused before it is called.

Each is written so it fails against the old ordering.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_db.models.agent import AgentRun
from aleph_db.models.model_profile import ModelProfile
from aleph_rks.embedding import KNOWN_EMBEDDING_DIMS
from aleph_rks.models import DocumentChunk, NormalizedDocument, RetrievalIndexRecord
from aleph_rks.retrieval import search_corpus
from aleph_security.agent_token import mint_agent_token
from aleph_workers.jobs.chunk_embed import chunk_embed_job

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
SECRET = "test-secret-key-that-is-at-least-32-bytes-long"

#: A phrase no other document in the fixture contains, so a lexical hit on it is
#: proof the chunk is really in the index rather than proof the query matched
#: something generic.
MARKER = "quokka photosynthesis anomaly"

MARKDOWN = f"""# Field notes

The {MARKER} was first recorded in the spring survey, and has not been
reproduced since. Three follow-up attempts are described below.

## Method

Samples were collected weekly over a period of eleven weeks.
"""


class _AssetStore:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def get(self, _uri: str) -> bytes:
        return self._payload


class _DeadEmbedder:
    """Stands in for LiteLLMClient. Counts calls; every one fails."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, **_kw: Any) -> Any:
        self.calls += 1
        msg = "litellm.BadRequestError: model 'titan-embed-v2' not found"
        raise RuntimeError(msg)


class _RefusingEmbedder:
    """Must never be called at all."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, **_kw: Any) -> Any:  # pragma: no cover - must not run
        self.calls += 1
        msg = "embed must not be called when the model's width cannot fit the column"
        raise AssertionError(msg)


async def _seed(
    session: AsyncSession, project_id: uuid.UUID, *, embed_model: str | None
) -> uuid.UUID:
    """One source, one normalized document, one model profile. Committed."""
    source_id = uuid.uuid4()
    bindings: dict[str, Any] = {
        "synthesis": {"model": "some-chat-model", "provider": "litellm"},
    }
    if embed_model is not None:
        bindings["embedding"] = {"model": embed_model, "provider": "litellm"}
    session.add(
        ModelProfile(
            id=uuid.uuid4(),
            name="fixture",
            project_id=project_id,
            is_template=False,
            bindings_jsonb=bindings,
            created_by=ACTOR,
        )
    )
    await session.execute(
        text(
            "INSERT INTO sources (id, project_id, connector_kind, title, short_id, status,"
            " source_metadata_jsonb, created_by)"
            " VALUES (:id, :pid, 'upload', 'Field notes', :short, 'normalized', '{}', :actor)"
        ),
        {
            "id": source_id,
            "pid": project_id,
            "short": f"s{uuid.uuid4().hex[:8]}",
            "actor": ACTOR,
        },
    )
    normalized = NormalizedDocument(
        id=uuid.uuid4(),
        project_id=project_id,
        source_id=source_id,
        source_version_id=uuid.uuid4(),
        markdown_uri="fixture://field-notes.md",
        parser="fixture",
        parser_version="1",
        char_count=len(MARKDOWN),
        token_count=len(MARKDOWN) // 4,
        created_by=ACTOR,
    )
    session.add(normalized)
    await session.commit()
    return normalized.id


def _token(project_id: uuid.UUID) -> str:
    return mint_agent_token(
        secret=SECRET,
        user_id=ACTOR,
        project_id=project_id,
        agent_run_id=uuid.uuid4(),
        actor_kind="aleph_agent",
        correlation_id=f"corr-{uuid.uuid4().hex}",
    )


class _OneGateway:
    """Stands in for `aleph_workers.gateway.WorkerGateways`.

    WS-MEP-4 moved the job's model client from `ctx["litellm_client"]` — one
    client for every project — to a per-project resolution, so the seam a test
    injects at moved with it. Substituting the RESOLVER rather than the client
    keeps this file about embedding degradation: which gateway a project talks
    to is `test_worker_gateway_endpoints.py`, and it uses two real fakes.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.asked_for: list[uuid.UUID] = []

    async def litellm(self, project_id: uuid.UUID) -> Any:
        self.asked_for.append(project_id)
        return self._client


def _ctx(maker: Callable[[], AsyncSession], embedder: Any) -> dict[str, Any]:
    return {
        "agent_token_secret": SECRET,
        "session_maker": maker,
        "asset_store": _AssetStore(MARKDOWN.encode()),
        "gateways": _OneGateway(embedder),
    }


async def test_dead_embedder_still_writes_chunks(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """The lexical leg needs no model, so an embed failure must not empty it."""
    async with maker() as s:
        normalized_id = await _seed(s, committed_project, embed_model="an-unknown-embedder")

    embedder = _DeadEmbedder()
    result = await chunk_embed_job(
        _ctx(maker, embedder), str(normalized_id), _token(committed_project)
    )

    assert result["ok"] is False
    assert embedder.calls >= 1, "the embedder was never actually attempted"

    async with maker() as s:
        chunks = list(
            (
                await s.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.normalized_document_id == normalized_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert chunks, "an embed failure emptied the index — this is the whole defect"
        assert all(c.embedding is None for c in chunks)

        # Searchable, right now, with a vector that carries no information.
        hits = await search_corpus(
            s,
            project_id=committed_project,
            query_text=MARKER,
            query_embedding=None,  # the degraded mode: lexical leg only
            top_k=5,
        )
    assert hits, "no lexical hit — keyword search went down with the embedder"
    assert any(MARKER in h.text for h in hits)


async def test_a_failed_index_is_visible_not_stuck(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """`running` forever is how 45 identical failures went unreported."""
    async with maker() as s:
        normalized_id = await _seed(s, committed_project, embed_model="an-unknown-embedder")

    await chunk_embed_job(
        _ctx(maker, _DeadEmbedder()), str(normalized_id), _token(committed_project)
    )

    async with maker() as s:
        run = (
            await s.execute(
                select(AgentRun).where(
                    AgentRun.project_id == committed_project,
                    AgentRun.agent_kind == "chunk_embed",
                )
            )
        ).scalar_one()
        assert run.status == "failed"
        assert run.error_text
        assert "did not answer" in run.error_text

        record = (
            await s.execute(
                select(RetrievalIndexRecord).where(
                    RetrievalIndexRecord.project_id == committed_project
                )
            )
        ).scalar_one()
        assert record.state == "lexical_only"
        assert record.degraded_reason
        assert record.chunk_count > 0


async def test_an_unbound_embedder_is_a_stated_reason_not_a_crash(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Templates now ship no embedding binding, so "unbound" is the normal state
    between project creation and autoconfigure. It must degrade, not explode."""
    async with maker() as s:
        normalized_id = await _seed(s, committed_project, embed_model=None)

    embedder = _RefusingEmbedder()
    result = await chunk_embed_job(
        _ctx(maker, embedder), str(normalized_id), _token(committed_project)
    )

    assert result["ok"] is False
    assert embedder.calls == 0
    assert result["reason"] and "no embedding capability" in result["reason"]

    async with maker() as s:
        count = await s.scalar(
            select(RetrievalIndexRecord.chunk_count).where(
                RetrievalIndexRecord.project_id == committed_project
            )
        )
    assert count and count > 0


async def test_a_width_mismatch_costs_nothing(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejecting before billing is the property; it survives the reordering."""
    monkeypatch.setitem(KNOWN_EMBEDDING_DIMS, "wrong-width-model", 256)
    async with maker() as s:
        normalized_id = await _seed(s, committed_project, embed_model="wrong-width-model")

    embedder = _RefusingEmbedder()
    result = await chunk_embed_job(
        _ctx(maker, embedder), str(normalized_id), _token(committed_project)
    )

    assert embedder.calls == 0, "a known width mismatch must never reach the gateway"
    assert result["ok"] is False
    assert "wrong-width-model" in (result["reason"] or "")

    async with maker() as s:
        chunks = await s.scalar(
            select(RetrievalIndexRecord.chunk_count).where(
                RetrievalIndexRecord.project_id == committed_project
            )
        )
    assert chunks and chunks > 0, "a bad width should cost the dense leg, not the whole index"


async def test_reindexing_replaces_rather_than_duplicates(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """Repair passes must be safe to run twice; this is what makes them so."""
    async with maker() as s:
        normalized_id = await _seed(s, committed_project, embed_model="an-unknown-embedder")

    for _ in range(2):
        await chunk_embed_job(
            _ctx(maker, _DeadEmbedder()), str(normalized_id), _token(committed_project)
        )

    async with maker() as s:
        total = await s.scalar(
            select(RetrievalIndexRecord.chunk_count).where(
                RetrievalIndexRecord.project_id == committed_project
            )
        )
        actual = len(
            list(
                (
                    await s.execute(
                        select(DocumentChunk.id).where(
                            DocumentChunk.normalized_document_id == normalized_id
                        )
                    )
                ).all()
            )
        )
    assert actual == total
