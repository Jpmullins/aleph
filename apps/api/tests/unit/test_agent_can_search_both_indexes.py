"""The agent can read the two knowledge plugins directly — WS-RS6 c6, WS-RS10 c5.

CLAUDE.md opens by describing two knowledge plugins and says both are fully
accessible. They were not accessible to the agent. `grep -rn 'search_corpus'
apps/` returned **0** across three audits: the RAG over 3,451 embedded chunks
had no agent tool, and `aleph_wiki.claim_search.search_claims` had no caller
anywhere in `apps/` at all — a vector index, a graph hop and an HNSW index, all
reachable only from the eval harness and its own tests.

These tests drive the real tools. What they pin is not "a tool exists" — that is
a grep — but the three things the tool has to get right for the answer to be
usable:

* the passage TEXT comes back, not a list of ids the model cannot read;
* an empty result SAYS the corpus has nothing, because a model handed an empty
  string answers from memory;
* a dead embedder degrades to `query_embedding=None`, never to a zero vector.
  The zero vector is not equivalent — cosine distance to it is degenerate, so
  the dense leg returns an arbitrary page of rows and RRF fuses that noise in as
  a ranking (WS-RS1). That defect shipped once already.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from aleph_api import copilot_agent
from aleph_rks.retrieval import ChunkHit
from aleph_wiki.claim_search import ClaimHit

SOURCE_ID = uuid.uuid4()
CHUNK_ID = uuid.uuid4()
CLAIM_ID = uuid.uuid4()
PAGE_ID = uuid.uuid4()

CHUNKS = [
    ChunkHit(
        chunk_id=CHUNK_ID,
        ordinal=3,
        text="Retrieval-augmented generation grounds each answer in a retrieved passage.",
        # A STRING, which is what `ChunkHit.section_path` is. The first version
        # of the formatter did `" / ".join(...)` over it and rendered
        # `i / i / i / - / r / e / l ...` — one separator per character —
        # against the live corpus. A list here would have hidden that.
        section_path="ii-method.retrieval",
        score=0.83,
        source_id=SOURCE_ID,
    )
]

CLAIMS = [
    ClaimHit(
        claim_id=CLAIM_ID,
        text="Hybrid retrieval outperforms lexical-only retrieval on this corpus.",
        confidence="weakly_supported",
        page_id=PAGE_ID,
        score=0.71,
    )
]


class _Session:
    """Just enough of an AsyncSession to be entered and to answer the short-id
    lookup. Nothing here is asserted on; the tools' *inputs* to retrieval are."""

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    async def execute(self, *_a: object, **_kw: object) -> Any:
        class _Result:
            @staticmethod
            def all() -> list[Any]:
                class _Row:
                    id = SOURCE_ID
                    short_id = "s7"

                return [_Row()]

        return _Result()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Scope, principal and bindings resolved; retrieval itself is captured."""
    project_id = uuid.uuid4()
    seen: dict[str, Any] = {}

    monkeypatch.setitem(copilot_agent._runtime, "session_maker", lambda: _Session())
    monkeypatch.setitem(copilot_agent._runtime, "litellm", None)

    async def _project(_config: Any) -> uuid.UUID:
        return project_id

    async def _authorized(pid: uuid.UUID | None) -> uuid.UUID | None:
        return pid

    async def _bindings(_pid: uuid.UUID | None) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(copilot_agent, "_project_id_from_config", _project)
    monkeypatch.setattr(copilot_agent, "_authorized", _authorized)
    monkeypatch.setattr(copilot_agent, "bindings_for_project", _bindings)
    monkeypatch.setattr(
        copilot_agent, "_acting_principal", lambda _pid: object.__new__(_FakePrincipal)
    )

    async def _corpus(_session: Any, **kw: Any) -> list[ChunkHit]:
        seen["corpus"] = kw
        return list(seen.get("corpus_hits", CHUNKS))

    async def _claims(_session: Any, **kw: Any) -> list[ClaimHit]:
        seen["claims"] = kw
        return list(seen.get("claim_hits", CLAIMS))

    monkeypatch.setattr("aleph_rks.retrieval.search_corpus", _corpus)
    monkeypatch.setattr("aleph_wiki.claim_search.search_claims", _claims)
    return seen


class _FakePrincipal:
    user_id = uuid.uuid4()


async def test_both_indexes_are_registered_as_orchestrator_tools() -> None:
    """A tool the graph is never given is a tool the agent cannot call."""
    names = {
        getattr(t, "name", getattr(t, "__name__", "")) for t in copilot_agent._ORCHESTRATOR_TOOLS
    }
    assert "search_corpus" in names
    assert "search_claims" in names


async def test_corpus_search_returns_the_passage_text_not_only_ids(
    wired: dict[str, Any],
) -> None:
    """The whole point of the RAG half: the model reads the actual sentence."""
    out = await copilot_agent.search_corpus.ainvoke({"query": "rag", "config": {}})
    assert "grounds each answer in a retrieved passage" in out
    assert "s7" in out, "the passage cannot be cited without its source's short id"
    assert str(CHUNK_ID) in out
    assert "ii-method.retrieval" in out, (
        "the section label came back mangled — `section_path` is a string, and "
        "the first formatter joined it as though it were a list of headings"
    )


async def test_an_empty_corpus_result_says_so_instead_of_returning_nothing(
    wired: dict[str, Any],
) -> None:
    """An empty string reads to a model as permission to answer from memory."""
    wired["corpus_hits"] = []
    out = await copilot_agent.search_corpus.ainvoke({"query": "rag", "config": {}})
    assert "nothing" in out.lower()
    assert "memory" in out.lower()


async def test_a_dead_embedder_passes_none_and_never_a_zero_vector(
    wired: dict[str, Any],
) -> None:
    """`None` means lexical-only. A zero vector means a degenerate dense leg
    whose arbitrary page of rows gets fused in as though it were a ranking."""
    await copilot_agent.search_corpus.ainvoke({"query": "rag", "config": {}})
    assert wired["corpus"]["query_embedding"] is None


async def test_a_degraded_corpus_search_labels_itself(wired: dict[str, Any]) -> None:
    out = await copilot_agent.search_corpus.ainvoke({"query": "rag", "config": {}})
    assert "degraded" in out.lower()


async def test_claim_search_returns_the_claim_and_its_confidence(
    wired: dict[str, Any],
) -> None:
    """A belief without its confidence is an assertion, which is the thing the
    claim layer exists to stop the system making."""
    out = await copilot_agent.search_claims.ainvoke({"query": "hybrid", "config": {}})
    assert "Hybrid retrieval outperforms" in out
    assert "weakly_supported" in out
    assert str(CLAIM_ID) in out


async def test_an_empty_claim_result_points_at_the_other_index(
    wired: dict[str, Any],
) -> None:
    """ "We hold no belief on this" is not "there is nothing on this" — the raw
    sources are a different index and the model has to be told which it hit."""
    wired["claim_hits"] = []
    out = await copilot_agent.search_claims.ainvoke({"query": "hybrid", "config": {}})
    assert "search_corpus" in out


async def test_top_k_is_bounded_so_one_tool_call_cannot_flood_the_context(
    wired: dict[str, Any],
) -> None:
    await copilot_agent.search_corpus.ainvoke({"query": "rag", "config": {}, "top_k": 500})
    assert wired["corpus"]["top_k"] == 20
    await copilot_agent.search_claims.ainvoke({"query": "rag", "config": {}, "top_k": 0})
    assert wired["claims"]["top_k"] == 1
