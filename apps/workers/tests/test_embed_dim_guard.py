"""Embedding-dimension guard (WP-5 A1): a dimension mismatch must be caught
*before* any billed embed call, on both the initial-ingest path
(`chunk_embed_job`) and the re-embed path (`reembed_for_project`).

These are unit tests (no compose stack). The `LiteLLMClient` — the only site
that writes a `ModelCall` / `CostLedgerEvent` — is a spy; the guarantee we
assert is that it is *never invoked* on a mismatch, i.e. zero model spend.
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
    # titan-embed-v2 is 1024-dim == the column width → no mismatch.
    assert embedding_dim_mismatch("titan-embed-v2") is None


def test_embedding_dim_mismatch_unknown_model_is_none() -> None:
    assert embedding_dim_mismatch("some-model-we-have-never-heard-of") is None


def test_embedding_dim_mismatch_known_wrong_dim() -> None:
    # text-embedding-3-small is 1536-dim != 1024 column → mismatch reported.
    assert embedding_dim_mismatch("text-embedding-3-small") == 1536
    assert EMBEDDING_DIM == 1024


def test_is_known_embedding_model() -> None:
    from aleph_rks.embedding import is_known_embedding_model

    assert is_known_embedding_model("titan-embed-v2") is True
    assert is_known_embedding_model("some-model-we-have-never-heard-of") is False


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


# ---- initial ingest --------------------------------------------------------


async def test_initial_ingest_dim_mismatch_rejects_before_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aleph_security.agent_token import mint_agent_token
    from aleph_workers.jobs.chunk_embed import chunk_embed_job

    monkeypatch.setitem(KNOWN_EMBEDDING_DIMS, "wrong-dim-model", 256)

    user_id, project_id, agent_run_id = uuid4(), uuid4(), uuid4()
    secret = "test-secret-key-that-is-at-least-32-bytes-long"
    token = mint_agent_token(
        secret=secret,
        user_id=user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id="corr-1",
    )

    source_id = uuid4()
    normalized = SimpleNamespace(id=uuid4(), source_id=source_id, project_id=project_id)
    source = SimpleNamespace(id=source_id, status="normalized")
    profile = SimpleNamespace(
        bindings_jsonb={"embedding": {"model": "wrong-dim-model", "provider": "litellm"}}
    )
    agent_run = SimpleNamespace(status="running", completed_at=None, error_text=None)

    # execute() order: normalized, source, profile (session2); source (session3);
    # agent_run (session4 / _finalize).
    queue: list[Any] = [normalized, source, profile, source, agent_run]
    counts = {"add": 0, "commit": 0, "flush": 0, "execute": 0}

    def maker() -> _Session:
        return _Session(queue, counts)

    spy = _EmbedSpy()
    ctx: dict[str, Any] = {
        "agent_token_secret": secret,
        "session_maker": maker,
        "asset_store": SimpleNamespace(get=lambda _uri: b""),
        "litellm_client": spy,
    }

    out = await chunk_embed_job(ctx, str(normalized.id), token)

    assert out["ok"] is False
    assert spy.calls == 0  # zero embed calls → zero ModelCall / CostLedgerEvent
    assert source.status == "failed"
    assert agent_run.status == "failed"
    assert agent_run.error_text is not None
    assert "wrong-dim-model" in agent_run.error_text


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
