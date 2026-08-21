"""Embedding-dimension guard: a width mismatch must cost nothing.

A model whose output width cannot fit `document_chunks.embedding` can never
produce a writable vector, so calling it is pure waste. The guard rejects on
metadata alone, before any billed call, on the re-embed path here and on the
initial-ingest path in `tests/integration/test_chunk_embed_degrades.py`.

The initial-ingest half moved to an integration test deliberately. It used to
live here against a hand-built fake session, and it asserted the *old* ordering
— that a mismatch stopped chunks from being written at all. That ordering is the
defect: chunks are written before embedding now, so a mismatch costs the dense
leg and nothing else. A fake session cannot express that, because the property
is about transaction boundaries.

The `LiteLLMClient` — the only site that writes a `ModelCall` /
`CostLedgerEvent` — is a spy; the guarantee asserted is that it is *never
invoked* on a mismatch, i.e. zero model spend.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aleph_rks.embedding import KNOWN_EMBEDDING_DIMS, embedding_dim_mismatch
from aleph_rks.models import EMBEDDING_DIM

# ---- pure helper -----------------------------------------------------------


def test_embedding_dim_mismatch_matches_column() -> None:
    # titan-embed-text-v2 is 1024-dim == the column width → no mismatch.
    assert embedding_dim_mismatch("titan-embed-text-v2") is None


def test_embedding_dim_mismatch_unknown_model_is_none() -> None:
    assert embedding_dim_mismatch("some-model-we-have-never-heard-of") is None


def test_embedding_dim_mismatch_known_wrong_dim() -> None:
    # text-embedding-3-small is 1536-dim != 1024 column → mismatch reported.
    assert embedding_dim_mismatch("text-embedding-3-small") == 1536
    assert EMBEDDING_DIM == 1024


def test_is_known_embedding_model() -> None:
    from aleph_rks.embedding import is_known_embedding_model

    assert is_known_embedding_model("titan-embed-text-v2") is True
    assert is_known_embedding_model("some-model-we-have-never-heard-of") is False


def test_the_registry_names_no_model_that_is_not_served() -> None:
    """`titan-embed-v2` was in this registry and no gateway serves it.

    An entry here can only ever *reject* a model, so a fictional name is a
    rejection that can never fire — and its presence read as evidence the name
    was correct. The one it displaced, `titan-embed-text-v2`, is what the
    gateway actually reports.
    """
    assert "titan-embed-v2" not in KNOWN_EMBEDDING_DIMS
    assert KNOWN_EMBEDDING_DIMS["titan-embed-text-v2"] == EMBEDDING_DIM


# ---- fakes -----------------------------------------------------------------


class _Result:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def scalar_one_or_none(self) -> Any:
        return self._payload

    def scalars(self) -> Any:
        return SimpleNamespace(all=lambda: self._payload)


class _Session:
    def __init__(self, queue: list[Any], counts: dict[str, int]) -> None:
        self._queue = queue
        self._counts = counts

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    def add(self, _obj: object) -> None:
        self._counts["add"] += 1

    async def commit(self) -> None:
        self._counts["commit"] += 1

    async def flush(self) -> None:
        self._counts["flush"] += 1

    async def execute(self, _stmt: object) -> _Result:
        self._counts["execute"] += 1
        return _Result(self._queue.pop(0))


class _EmbedSpy:
    """Stands in for LiteLLMClient — records if `embed` is ever awaited."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, **_kw: Any) -> Any:  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("embed must not be called on a dimension mismatch")


# ---- re-embed --------------------------------------------------------------


async def test_reembed_dim_mismatch_marked_and_skipped_no_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aleph_rks.retrieval import reembed_for_project

    monkeypatch.setitem(KNOWN_EMBEDDING_DIMS, "wrong-dim-model", 256)

    project_id = uuid4()
    rec = SimpleNamespace(source_id=uuid4(), embedder_model="old-embed-model")
    chunk = SimpleNamespace(
        source_id=rec.source_id,
        ordinal=0,
        text="chunk 0",
        embedding=[0.0] * EMBEDDING_DIM,
        embedder_model="old-embed-model",
    )
    queue: list[Any] = [[rec], [chunk]]
    counts = {"add": 0, "commit": 0, "flush": 0, "execute": 0}
    session = _Session(queue, counts)
    spy = _EmbedSpy()

    sources_done, chunks_done = await reembed_for_project(
        session,  # type: ignore[arg-type]
        project_id=project_id,
        client=spy,
        principal=SimpleNamespace(user_id=uuid4(), actor_kind="aleph_agent"),
        profile_bindings={"embedding": {"model": "wrong-dim-model", "provider": "litellm"}},
        purpose="rks.reembed",
    )

    assert (sources_done, chunks_done) == (0, 0)  # skipped, nothing written
    assert spy.calls == 0  # zero embed calls → zero ModelCall / CostLedgerEvent
    assert counts["flush"] == 0  # never reached the write/flush
    # The stale record was left untouched (still old model) — marked, not re-billed.
    assert rec.embedder_model == "old-embed-model"
