"""Retrieval recall — an eval that actually invokes Aleph.

The eval this replaces did not. Its scorers read ``expected`` and ``actual``
from the same JSONL line, so the system under test was never called and the CI
gate was green regardless of the code. That is how a broken central hypothesis
survived seven work packages.

This one seeds a scratch project with a real corpus, runs the real
``search_corpus`` against it, and reports recall@k as a number. Nothing is
pre-baked: ``actual`` comes from Postgres.

**Modes.** The dense leg needs an embedding model. When the gateway is
configured the run is `hybrid`; when it is not, the dense leg is skipped and the
run is `lexical` — reported in the output, never silently. A lexical-only number
is still a real measurement of a real code path; pretending it measured the
hybrid would not be.

Run it: ``python -m aleph_evals.retrieval_eval``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

# .../aleph-evals/src/aleph_evals/retrieval_eval.py
#   parents[0]=aleph_evals  [1]=src  [2]=aleph-evals  <- datasets live here
#: Where the labelled set lives.
#:
#: Overridable because the committed set is a 12-document toy — measured
#: saturated at recall@1 = 1.00, so it cannot resolve any change RS6 or RS10
#: might make. A real set is built from the operator's OWN ingested corpus
#: (`python -m aleph_evals.build_retrieval_set`), and is not committed here:
#: the material Aleph ingests is published papers, and shipping their full text
#: in this repository is a licensing decision that belongs to whoever owns the
#: repository, not to the eval.
DATASET_DIR = Path(
    os.environ.get(
        "ALEPH_RETRIEVAL_DATASET",
        str(Path(__file__).resolve().parents[2] / "datasets" / "retrieval"),
    )
)
EMBEDDING_DIM = 1024


#: Questions whose answer is deliberately NOT in the corpus.
#:
#: Without this category the eval cannot tell a system that says "I don't know"
#: from one that confidently returns the nearest irrelevant passage — and every
#: other metric here rewards the second. Scored on abstention, never on recall.
UNANSWERABLE = "unanswerable"

#: Cut-off for nDCG. Ten, because that is roughly what a person scans.
NDCG_K = 10

#: Recall cut-offs reported. @1 is what a single-answer surface shows; @20 is
#: the ceiling a reranker has to work with.
RECALL_KS = (1, 3, 8, 20)

#: Chunks any one source may contribute. Mirrors the production default — the
#: eval used to pass `None`, so the cap that stops one long document filling
#: every slot was never exercised.
PER_SOURCE_CAP = 3

#: The two indexes a question can be answered from.
#:
#: `chunks` is `aleph_rks.retrieval.search_corpus` over passages — what the
#: project COLLECTED. `claims` is `aleph_wiki.claim_search.search_claims` over
#: live beliefs — what the project CONCLUDED. `docs/decisions.md` D1 settles
#: that both stay, so this is a measurement and never a gate: there is no
#: `--min-claim-gain`, because a number that decided which plugin survives
#: would be re-litigating a decision the project has already made.
CHUNKS = "chunks"
CLAIMS = "claims"


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    expect: tuple[str, ...]
    phrasing: str
    #: `factual` or `unanswerable`. Defaulted so the existing 45-question set
    #: loads unchanged rather than needing a migration to be readable.
    category: str = "factual"


@dataclass
class Report:
    mode: str
    k: int
    total: int
    hits: int
    by_phrasing: dict[str, tuple[int, int]]
    misses: list[tuple[str, str, str]]
    #: `chunks` or `claims` — which index this run searched. On the report
    #: rather than only in the mode string because `--surface both` prints two
    #: of these and the reader has to be able to tell them apart at a glance.
    surface: str = CHUNKS
    #: Ranking quality, not just presence. Recall cannot tell "the answer was
    #: first" from "the answer was eighth", so a reranker that reorders the same
    #: eight results is worth exactly zero to it — which is the whole point of
    #: WS-RS6.
    ndcg: float = 0.0
    mrr: float = 0.0
    recall_at: dict[int, float] = field(default_factory=dict)
    #: Unanswerable questions, scored on declining rather than on retrieving.
    abstain_total: int = 0
    abstain_correct: int = 0
    #: Claim surface only. `graph_hop_results` counts results reached ONLY by
    #: walking `claim_edges`; `graph_hop_only_hits` counts questions whose ONLY
    #: correct result arrived that way. The second is the one that matters:
    #: WS-RS10 c6 asks whether the hop contributes, and a hop that returns
    #: plenty of neighbours which are never the answer contributes nothing.
    graph_hop_results: int = 0
    graph_hop_only_hits: int = 0
    #: Whether the hop was enabled at all, so "0" cannot be read as "measured
    #: and found worthless" when it means "switched off".
    graph_hop_enabled: bool = False
    #: Claims that were never seeded because the dataset carries none.
    claims_seeded: int = 0
    edges_seeded: int = 0
    #: How big the measured set actually is. WS-RS5 c1 puts a floor on it —
    #: ">= 300 documents and >= 150 questions" — and until 2026-08-22 the run
    #: printed neither, so the number the floor is about was invisible in the
    #: output that is supposed to report it. A saturated 12-document toy and a
    #: 740-document generated set rendered identically apart from the metrics,
    #: which is the difference between "retrieval got better" and "the ruler
    #: got shorter".
    corpus_documents: int = 0
    corpus_chunks: int = 0
    corpus_questions: int = 0
    #: Chunk arm only. How many returned passages carried a `section_path`, out
    #: of how many were returned. This is what a layout-aware PDF parser buys
    #: directly (WS-RS11): the chunker finds a section by looking for a markdown
    #: heading, so a flat parser makes this structurally 0 and no ranking metric
    #: notices — the passages are the same words either way, and only the reader
    #: loses the ability to see where in the paper the answer came from.
    labelled_hits: int = 0
    returned_hits: int = 0
    #: Where the set was read from. Printed because the path is an environment
    #: variable (`ALEPH_RETRIEVAL_DATASET`): two runs of the same command can
    #: legitimately measure two different sets, and the output has to say which.
    dataset_dir: str = ""

    @property
    def recall(self) -> float:
        return self.hits / self.total if self.total else 0.0

    @property
    def abstain_rate(self) -> float:
        return self.abstain_correct / self.abstain_total if self.abstain_total else 0.0

    @property
    def section_label_rate(self) -> float:
        return self.labelled_hits / self.returned_hits if self.returned_hits else 0.0

    def _corpus_line(self) -> str:
        """Documents, searchable units and questions — plus the set's path.

        Written as one line rather than three because it is context, not a
        result. `chunks` is omitted on the claim arm: that arm seeds no chunks
        and prints its own `seeded N claims` line, and reporting a chunk count
        of zero beside it would read as an empty index.
        """
        parts = [f"{self.corpus_documents} documents"]
        if self.surface == CHUNKS:
            parts.append(f"{self.corpus_chunks} chunks")
        parts.append(f"{self.corpus_questions} questions")
        return " · ".join(parts) + (f"  [{self.dataset_dir}]" if self.dataset_dir else "")

    def render(self) -> str:
        lines = [
            f"{self.surface} recall@{self.k} = {self.recall:.2f}  ({self.hits}/{self.total})",
            f"mode: {self.mode}",
            # The size of the ruler, before any number measured with it. The
            # chunk count is the honest one for the chunk arm — it is what
            # `search_corpus` searched — and the document count is what
            # WS-RS5 c1's ">= 300" floor is stated against, so both are here.
            f"  corpus    {self._corpus_line()}",
            f"  nDCG@{NDCG_K}  {self.ndcg:.3f}",
            f"  MRR       {self.mrr:.3f}",
        ]
        if self.surface == CLAIMS:
            lines.append(f"  seeded    {self.claims_seeded} claims, {self.edges_seeded} edges")
            if not self.graph_hop_enabled:
                lines.append("  graph hop off (--no-graph-hop)")
            else:
                # Both numbers, always. "reached" alone reads as a
                # contribution; the hop earns its keep only when a
                # graph-reached claim is the ONLY correct result for a
                # question, and on a graph with no traversable edges that is
                # structurally zero — which is WS-RS9 c4 showing up here.
                lines.append(
                    f"  graph hop {self.graph_hop_results} result(s) reached only via "
                    f"claim_edges; {self.graph_hop_only_hits} question(s) answered ONLY "
                    "by one"
                )
        if self.recall_at:
            lines.append(
                "  recall    "
                + "  ".join(f"@{n} {v:.2f}" for n, v in sorted(self.recall_at.items()))
            )
        if self.surface == CHUNKS:
            lines.append(
                f"  sections  {self.section_label_rate:.2f}  "
                f"({self.labelled_hits}/{self.returned_hits} retrieved passages "
                "carry a section label)"
            )
        if self.abstain_total:
            lines.append(
                f"  abstain   {self.abstain_rate:.2f}  "
                f"({self.abstain_correct}/{self.abstain_total} unanswerable declined)"
            )
        else:
            # Said rather than omitted. A metric that is silently absent reads
            # as a metric that passed, which is how a gate certifies something
            # it never measured.
            lines.append(f"  abstain   n/a — the set has no '{UNANSWERABLE}' questions (WS-RS5)")
        for phrasing, (hit, total) in sorted(self.by_phrasing.items()):
            share = hit / total if total else 0.0
            lines.append(f"  {phrasing:<14} {share:.2f}  ({hit}/{total})")
        if self.misses:
            lines.append(f"  misses ({len(self.misses)}):")
            lines.extend(f"    {qid}  expected {want}  — {q}" for qid, want, q in self.misses[:10])
        return "\n".join(lines)


def _ndcg_at(ordered: list[Any], wanted: set[Any], cutoff: int) -> float:
    """Binary-relevance nDCG. One graded scale, stated where it is computed.

    Relevance is 0/1 because the labels are 0/1 — inventing graded relevance
    from a binary set produces a number with more precision than the data.
    The ideal ranking puts every wanted source first, so the denominator is the
    DCG of `min(len(wanted), cutoff)` hits.

    **Each wanted source is credited once, at its best position.** `ordered` is
    a list of source ids taken from CHUNK hits, so one source appears once per
    chunk it contributed — and the first version of this function added a gain
    term for every one of them while the ideal denominator counted DISTINCT
    sources. That made the metric unbounded above:

        _ndcg_at(["src-A"] * 3,  {"src-A"}, 10) == 2.1309
        _ndcg_at(["src-A"] * 10, {"src-A"}, 10) == 4.5436

    A retrieval that returned the same correct source ten times scored 4.54 out
    of a possible 1.00, and it scored higher the more it repeated itself. The
    figure recorded as "nDCG@10 0.681" was produced by that function and was
    not an nDCG.

    Crediting the first occurrence is what binary relevance means when the
    ranked list is of chunks and the label is on the source: the source is
    either found or not, and finding it twice is not twice as good.
    """
    import math

    dcg = 0.0
    credited: set[Any] = set()
    for position, source in enumerate(ordered[:cutoff], start=1):
        if source in wanted and source not in credited:
            credited.add(source)
            dcg += 1.0 / math.log2(position + 1)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(wanted), cutoff) + 1))
    return dcg / ideal if ideal else 0.0


#: Chunk rows per INSERT+COMMIT while seeding.
#:
#: One flush for the whole corpus worked for twelve documents and drops the
#: asyncpg connection for a real one, losing several minutes of embedding with
#: nothing salvaged.
SEED_BATCH = 200


async def _seed_batch(
    maker: Any,
    batch: list[tuple[str, Any]],
    *,
    doc_to_source: dict[str, Any],
    vectors: list[list[float]] | None,
    project_id: Any,
    base: int,
) -> None:
    from aleph_core.ids import uuid7
    from aleph_rks.models import DocumentChunk

    async with maker() as session:
        for offset, (doc_id, chunk) in enumerate(batch):
            index = base + offset
            session.add(
                DocumentChunk(
                    id=uuid7(),
                    project_id=project_id,
                    source_id=doc_to_source[doc_id],
                    normalized_document_id=uuid7(),
                    ordinal=chunk.ordinal,
                    # The chunk's own text, so `char_start`/`char_end` index the
                    # document exactly — the invariant `test_chunk_offsets.py`
                    # pins on the production path.
                    text=chunk.text,
                    text_tsv="",  # trigger fills this
                    section_path=chunk.section_path,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    token_count=chunk.token_count,
                    embedding=(vectors[index] if vectors is not None else _zero_vector()),
                    embedder_model="eval-fixture",
                )
            )
        await session.commit()


def _title_of(corpus: list[dict[str, Any]], doc_id: str) -> str:
    for doc in corpus:
        if doc["doc_id"] == doc_id:
            return str(doc.get("title", ""))
    return ""


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _zero_vector() -> list[float]:
    """A vector that ranks nothing. Used to disable the dense leg honestly.

    All-zero means cosine distance is undefined/constant across rows, so the
    dense ranking carries no signal and RRF is driven entirely by the lexical
    leg. That is exactly "lexical only", achieved without a second code path
    that the real system does not have.
    """
    return [0.0] * EMBEDDING_DIM


#: Texts per embedding request.
#:
#: The eval sent the whole corpus in one call, which was fine for twelve short
#: documents and returns `413 Request Entity Too Large` for a real one. Nothing
#: about the old set could have surfaced this: the measurement was small enough
#: that the transport never mattered, which is its own argument for WS-RS5.
EMBED_BATCH = 64


def _gateway_embed(model: str, texts: list[str]) -> list[list[float]]:
    """Embed via the configured gateway, in batches.

    Raises on any failure — the caller decides whether to degrade, so a broken
    embedder can never be mistaken for a legitimately lexical run.
    """
    out: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        out.extend(_embed_one_batch(model, texts[start : start + EMBED_BATCH]))
    return out


def _embed_one_batch(model: str, texts: list[str]) -> list[list[float]]:
    import json
    import urllib.request

    base = os.environ["LITELLM_BASE_URL"].rstrip("/")
    req = urllib.request.Request(
        f"{base}/v1/embeddings",
        data=json.dumps({"model": model, "input": texts}).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['INSIGHTS_LITELLM_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = json.loads(resp.read())
    vectors = [row["embedding"] for row in sorted(body["data"], key=lambda r: r["index"])]
    bad = next((len(v) for v in vectors if len(v) != EMBEDDING_DIM), None)
    if bad is not None:
        msg = f"embedder {model!r} returned dim {bad}, expected {EMBEDDING_DIM}"
        raise RuntimeError(msg)
    return vectors


async def _embedding_model(maker: Any) -> tuple[str, str] | None:
    """The embedder the system would actually use, and the profile it came from.

    Read rather than configured, so the eval measures the deployed binding. An
    eval that embeds with a model the system does not use would report a number
    nothing else can reproduce.

    A profile that NAMES an embedder wins over one that does not, and only then
    does a template win over a project profile. That order matters now that
    Aleph ships no embedder name: the templates deliberately carry no
    `embedding` binding until autoconfigure fills one in from the gateway, so
    preferring the template unconditionally made this eval silently report
    `lexical` on a deployment whose projects were fully embedded — a measured
    number that describes half the system.

    The profile name is returned so the mode string can say where the binding
    came from. "hybrid" without "and I used THIS binding" is not reproducible.
    """
    from sqlalchemy import text as sql_text

    name = os.environ.get("ALEPH_DEFAULT_MODEL_PROFILE", "aleph-dev")
    async with maker() as session:
        row = (
            await session.execute(
                sql_text(
                    "select bindings_jsonb->'embedding'->>'model', name, is_template "
                    "from model_profiles "
                    "where bindings_jsonb->'embedding'->>'model' is not null "
                    "order by (name = :n) desc, is_template desc limit 1"
                ),
                {"n": name},
            )
        ).first()
    if not row or not row[0]:
        return None
    label = f"{row[1]}{' template' if row[2] else ''}"
    return str(row[0]), label


async def _rerank_model(maker: Any) -> tuple[Any, Any, UUID, dict[str, Any], str] | None:
    """Everything `reranker_for` needs, read off the deployed profile.

    Read rather than configured, for the same reason `_embedding_model` is: an
    eval that reranks with a model the system does not use reports a number
    nothing else can reproduce.
    """
    from sqlalchemy import text as sql_text

    from aleph_core.ids import uuid7
    from aleph_models.client import LiteLLMClient
    from aleph_models.discovery import discover_models
    from aleph_models.pricing import PricingTable
    from aleph_security.principal import Principal

    name = os.environ.get("ALEPH_DEFAULT_MODEL_PROFILE", "aleph-dev")
    async with maker() as session:
        row = (
            await session.execute(
                sql_text(
                    "select bindings_jsonb, name, is_template "
                    "from model_profiles "
                    "where bindings_jsonb->'rerank'->>'model' is not null "
                    "order by (name = :n) desc, is_template desc limit 1"
                ),
                {"n": name},
            )
        ).first()
    if not row or not row[0]:
        return None
    bindings = dict(row[0])
    label = f"{bindings['rerank']['model']} bound on {row[1]}{' template' if row[2] else ''}"
    import httpx

    # Its own client, and it outlives this function on purpose: the reranker
    # holds it for the whole run. A `with` block here would close the transport
    # before the first rerank call, which fails as "client has been closed" —
    # an error that reads like a gateway problem.
    # Priced from the gateway, not left empty. `PricingTable()` knows no rates,
    # so every `ModelCall` this writes lands with `pricing_source="unknown"` and
    # `cost_usd=0` — and these rows go into the SAME production ledger the
    # `uncosted_model_calls` number counts. A measurement run that reports its
    # own spend as free moves a health number in the wrong direction and is
    # exactly the "silent $0" the pricing module exists to prevent. Measured: 90
    # unpriced `rks.rerank.llm` rows from two eval runs.
    transport = httpx.AsyncClient(timeout=120.0)
    discovered = await discover_models(
        base_url=os.environ["LITELLM_BASE_URL"],
        api_key=os.environ["INSIGHTS_LITELLM_API_KEY"],
        client=transport,
    )
    client = LiteLLMClient(
        base_url=os.environ["LITELLM_BASE_URL"],
        api_key=os.environ["INSIGHTS_LITELLM_API_KEY"],
        # Held for the whole run on purpose: the reranker keeps this client, so
        # a `with` block here would close the transport before the first rerank
        # call and fail as "client has been closed" — an error that reads like a
        # gateway problem.
        http_client=transport,
        pricing=PricingTable.from_discovery(discovered),
        session_maker=maker,
    )
    principal = Principal(
        user_id=uuid7(),
        subject="retrieval-eval",
        email="eval@localhost",
        actor_kind="user",
    )
    return client, principal, uuid7(), bindings, label


async def _embedder(maker: Any) -> tuple[Any, str]:
    """Return (embed_fn, mode).

    Degrades to lexical-only rather than failing, but never silently: the mode
    string carries the reason, and a lexical number is not comparable to a
    hybrid one.
    """
    if not os.environ.get("INSIGHTS_LITELLM_API_KEY") or not os.environ.get("LITELLM_BASE_URL"):
        return (lambda _q: _zero_vector()), "lexical (no gateway configured)"

    bound = await _embedding_model(maker)
    if bound is None:
        return (
            lambda _q: _zero_vector(),
            "lexical (no ModelProfile binds an embedding model — run "
            "POST /v1/projects/{id}/model-profile/autoconfigure)",
        )
    model, profile_label = bound

    try:
        _gateway_embed(model, ["probe"])
    except Exception as exc:
        return (lambda _q: _zero_vector()), f"lexical ({model} unavailable: {exc})"

    def embed(q: str) -> list[float]:
        return _gateway_embed(model, [q])[0]

    return embed, f"hybrid (dense leg via {model}, bound on {profile_label})"


async def _reranker(maker: Any, *, enabled: bool) -> tuple[Any, str]:
    """The reranker for this run, and a label saying what it actually is.

    `--rerank off` must be a genuine control arm, so it returns a `NoReranker`
    rather than skipping the parameter: `search_corpus` reads a missing
    reranker and a stood-down one along different paths, and comparing two arms
    that took different paths measures the paths.

    The label goes into the report's `mode` line. "rerank on" that silently
    degraded to fused order is the failure this exists to make visible — it
    would otherwise print as an on-arm that happened to score the same.
    """
    from aleph_rks.rerank import NoReranker, reranker_for

    if not enabled:
        return NoReranker(skipped_reason="off (control arm)"), "off"
    if not os.environ.get("INSIGHTS_LITELLM_API_KEY") or not os.environ.get("LITELLM_BASE_URL"):
        return NoReranker(skipped_reason="no gateway configured"), "off (no gateway)"

    bound = await _rerank_model(maker)
    if bound is None:
        return (
            NoReranker(skipped_reason="no ModelProfile binds a rerank model"),
            "off (nothing binds Capability.RERANK — run autoconfigure)",
        )
    client, principal, project_id, bindings, label = bound
    built = reranker_for(
        client=client,
        principal=principal,
        project_id=project_id,
        profile_bindings=bindings,
    )
    if isinstance(built, NoReranker):
        return built, f"off ({built.skipped_reason})"
    return built, f"on ({label})"


class NoClaimLayer(SystemExit):
    """The dataset carries no claims, so there is nothing to search.

    A hard stop rather than a zeroed report. `--surface claims` returning
    "recall 0.00" for a set with no claims file is indistinguishable from a
    claim layer that retrieves nothing, and the second is a finding while the
    first is a missing input.
    """


async def _seed_claims(
    maker: Any,
    *,
    project_id: Any,
    mode: str,
    embedding_model: tuple[str, str] | None,
    dataset: Path,
) -> tuple[dict[UUID, str], int]:
    """Seed the claim surface from the dataset, and return `claim_id -> doc_id`.

    The claims are REAL — `build_retrieval_set` reads live `wiki_claims` whose
    citations are anchored to a chunk of the corpus it emitted, and assigns
    each to exactly one document. Nothing here invents a proposition, which is
    the whole reason the claim layer is worth measuring: a synthesized claim
    would just be the chunk text under a different table name, and comparing
    two spellings of the same string measures the SQL.

    Vectors are computed here rather than copied from `wiki_claims.embedding`.
    Not a shortcut — the opposite. The eval seeds a scratch project and embeds
    its corpus with the deployed embedder, so embedding the claims the same way
    is the only setting in which the chunks column and the claims column are
    measuring retrieval instead of measuring which of the two happened to have
    been embedded on this instance. (Which, as of WS-RS10, is neither: the
    column is NULL on every row.)

    `claim_key` is left NULL on purpose. Postgres treats NULLs as distinct in a
    unique index, so `uq_claims_project_key` cannot trip on two dataset claims
    that normalize to the same key — a collision in a throwaway project would
    abort the seed and read as a database problem.
    """
    from aleph_core.ids import uuid7
    from aleph_evals.build_retrieval_set import TRAVERSABLE_EDGE_KINDS
    from aleph_wiki.claim_search import TRAVERSABLE_EDGES
    from aleph_wiki.models import ClaimEdge, WikiClaim

    # The builder writes a data file; `search_claims` decides what it walks. If
    # the two lists drift, the dataset silently stops containing the edges the
    # search would have followed and the graph hop measures zero for a reason
    # that has nothing to do with the graph.
    if set(TRAVERSABLE_EDGE_KINDS) != set(TRAVERSABLE_EDGES):
        msg = (
            "the dataset builder and search_claims disagree about which edge kinds "
            f"are traversable: builder {sorted(TRAVERSABLE_EDGE_KINDS)} vs search "
            f"{sorted(TRAVERSABLE_EDGES)}"
        )
        raise SystemExit(msg)

    claims_path = dataset / "claims.jsonl"
    if not claims_path.exists():
        msg = (
            f"{claims_path} does not exist, so --surface claims has nothing to search. "
            "The committed 12-document set predates the claim layer; build one from "
            "your own corpus with `python -m aleph_evals.build_retrieval_set --out DIR` "
            "and point ALEPH_RETRIEVAL_DATASET at it."
        )
        raise NoClaimLayer(msg)

    rows = _load(claims_path)
    if not rows:
        msg = (
            f"{claims_path} is empty: no live claim in this instance has a citation "
            "anchored to a chunk of this corpus. That is a fact about the claim layer "
            "(WS-RS8), not a retrieval result, so it is not scored as one."
        )
        raise NoClaimLayer(msg)

    edge_path = dataset / "claim_edges.jsonl"
    edge_rows = _load(edge_path) if edge_path.exists() else []

    vectors: list[list[float]] | None = None
    if mode.startswith("hybrid") and embedding_model is not None:
        vectors = _gateway_embed(embedding_model[0], [str(r["text"]) for r in rows])

    # A dataset id is a string from a file; the row needs a UUID and the two
    # must agree, so the mapping is built from the ids actually inserted.
    claim_to_doc: dict[UUID, str] = {}
    by_dataset_id: dict[str, UUID] = {}
    # One page per document. `WikiClaim.page_id` is NOT NULL with no foreign
    # key, so a synthetic id is legal — and giving claims from one document a
    # shared page is what production looks like.
    page_of: dict[str, UUID] = {}
    author = uuid7()

    async with maker() as session:
        for index, row in enumerate(rows):
            doc_id = str(row["doc_id"])
            claim_id = uuid7()
            by_dataset_id[str(row["claim_id"])] = claim_id
            claim_to_doc[claim_id] = doc_id
            session.add(
                WikiClaim(
                    id=claim_id,
                    project_id=project_id,
                    page_id=page_of.setdefault(doc_id, uuid7()),
                    revision_id=None,
                    section_anchor=None,
                    text=str(row["text"])[:2048],
                    claim_key=None,
                    origin="agent",
                    evidence_tier="stated",
                    rationale="",
                    embedding=(vectors[index] if vectors is not None else None),
                    confidence="weakly_supported",
                    status="active",
                    created_by=author,
                )
            )
        await session.commit()

        edges = 0
        for edge in edge_rows:
            src = by_dataset_id.get(str(edge["src"]))
            dst = by_dataset_id.get(str(edge["dst"]))
            if src is None or dst is None or src == dst:
                continue
            session.add(
                ClaimEdge(
                    id=uuid7(),
                    project_id=project_id,
                    src_claim_id=src,
                    dst_claim_id=dst,
                    kind=str(edge["kind"])[:16],
                    weight=1.0,
                    rationale="",
                    created_by=author,
                )
            )
            edges += 1
        await session.commit()

    return claim_to_doc, edges


async def run(
    k: int = 8,
    *,
    rerank: bool = False,
    surface: str = CHUNKS,
    walk_graph: bool = True,
    dataset_dir: Path | None = None,
) -> Report:
    """One measurement over one set.

    `dataset_dir` overrides `ALEPH_RETRIEVAL_DATASET` for this call only. It is
    a parameter and not another environment read because WS-RS11 c5 needs TWO
    sets measured inside ONE process — the same corpus parsed by the layout
    normalizer and by the flat one — and re-importing this module per arm to
    pick up a changed environment variable is how a comparison ends up
    measuring the same set twice without saying so.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aleph_core.ids import uuid7
    from aleph_rks.chunking import Chunk, chunk_markdown
    from aleph_rks.indexing import embedding_text
    from aleph_rks.models import DocumentChunk
    from aleph_rks.retrieval import search_corpus
    from aleph_wiki.claim_search import search_claims

    url = os.environ.get("ALEPH_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        msg = "ALEPH_DATABASE_URL or DATABASE_URL is required"
        raise SystemExit(msg)

    dataset = dataset_dir or DATASET_DIR
    corpus = _load(dataset / "corpus.jsonl")
    questions = [
        Question(
            id=row["id"],
            question=row["question"],
            expect=tuple(row["expect"]),
            phrasing=row.get("phrasing", "unspecified"),
            category=row.get("category", "factual"),
        )
        for row in _load(dataset / "questions.jsonl")
    ]

    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    project_id = uuid7()
    doc_to_source: dict[str, UUID] = {}

    try:
        # Seed through the REAL chunker.
        #
        # This used to write one chunk per document, with `section_path=None`
        # and `char_start=0`, and then disable the diversity cap because there
        # was only one chunk per source anyway. So the eval measured retrieval
        # over a corpus that had never been chunked — no overlap, no section
        # paths, no competition between passages of the same document, and the
        # per-source cap (which exists to stop one document flooding the
        # results) never exercised at all. Every one of those is a thing
        # production does and the measurement did not.
        embed, mode = await _embedder(maker)
        reranker, rerank_mode = await _reranker(maker, enabled=rerank)
        mode = f"{mode} · rerank {rerank_mode}"

        for doc in corpus:
            doc_to_source[doc["doc_id"]] = uuid7()

        claim_to_doc: dict[UUID, str] = {}
        claims_seeded = 0
        edges_seeded = 0
        corpus_chunks = 0

        if surface == CHUNKS:
            seeded: list[tuple[str, Chunk]] = []
            for doc in corpus:
                for chunk in chunk_markdown(doc["text"]):
                    seeded.append((doc["doc_id"], chunk))
            corpus_chunks = len(seeded)

            # Embed the corpus in one batch. Seeding zero vectors while
            # querying a real one would leave the dense leg ranking nothing and
            # quietly report the lexical number as "hybrid".
            #
            # `embedding_text` is the SAME function the ingest path calls. The
            # eval used to embed `f"{title}. {text}"` while production embedded
            # the raw chunk, so every number ever reported was measured against
            # a better-conditioned corpus than the running system produces.
            bodies = [
                embedding_text(chunk_text=chunk.text, title=_title_of(corpus, doc_id))
                for doc_id, chunk in seeded
            ]
            doc_vectors: list[list[float]] | None = None
            if mode.startswith("hybrid"):
                bound_for_seed = await _embedding_model(maker)
                model = bound_for_seed[0] if bound_for_seed else None
                doc_vectors = _gateway_embed(str(model), bodies)

            # Committed in batches.
            #
            # This was one session and one flush for the whole corpus. Twelve
            # documents meant twelve rows; a real corpus means thousands, and
            # asyncpg drops the connection mid-`executemany` — `connection was
            # closed in the middle of operation`, after several minutes of
            # embedding, with nothing salvaged. Like the `413` on the embed
            # side, the old set was too small for the transport to matter.
            for offset in range(0, len(seeded), SEED_BATCH):
                await _seed_batch(
                    maker,
                    seeded[offset : offset + SEED_BATCH],
                    doc_to_source=doc_to_source,
                    vectors=doc_vectors,
                    project_id=project_id,
                    base=offset,
                )
        else:
            claim_to_doc, edges_seeded = await _seed_claims(
                maker,
                project_id=project_id,
                mode=mode,
                embedding_model=(
                    (await _embedding_model(maker)) if mode.startswith("hybrid") else None
                ),
                dataset=dataset,
            )
            claims_seeded = len(claim_to_doc)

        hits = 0
        by_phrasing: dict[str, list[int]] = {}
        misses: list[tuple[str, str, str]] = []
        ranks: list[int | None] = []
        gains: list[float] = []
        abstain_total = 0
        abstain_correct = 0
        graph_hop_results = 0
        graph_hop_only_hits = 0
        labelled_hits = 0
        returned_hits = 0

        async with maker() as session:
            for question in questions:
                # `ordered` is a ranked list of EVAL SOURCE IDS in both arms,
                # so every metric below is computed identically for chunks and
                # for claims and the two columns are comparable. A chunk hit
                # carries its source id directly; a claim hit is mapped through
                # the document its evidence is anchored to — one document per
                # claim, decided at build time, because crediting a claim to
                # every part of its paper would score the claims arm correct
                # for questions about passages it never mentions.
                via_graph: list[bool] = []
                if surface == CHUNKS:
                    found = await search_corpus(
                        session,
                        project_id=project_id,
                        query_text=question.question,
                        query_embedding=embed(question.question),
                        top_k=max(k, NDCG_K),
                        # The real cap, not `None`.
                        #
                        # It was disabled with the comment "one chunk per
                        # source anyway", which was true of the old
                        # one-chunk-per-document seeding and is not true now.
                        # The cap exists to stop a single long document filling
                        # every slot, and a measurement that switches it off
                        # cannot see it working — or failing.
                        per_source_cap=PER_SOURCE_CAP,
                        reranker=reranker,
                    )
                    ordered = [h.source_id for h in found]
                    non_empty = bool(found)
                    returned_hits += len(found)
                    labelled_hits += sum(1 for h in found if h.section_path)
                else:
                    claim_hits = await search_claims(
                        session,
                        project_id=project_id,
                        query_text=question.question,
                        query_embedding=embed(question.question),
                        top_k=max(k, NDCG_K),
                        walk_graph=walk_graph,
                    )
                    resolved = [
                        (doc_to_source[claim_to_doc[h.claim_id]], h.via_graph)
                        for h in claim_hits
                        if h.claim_id in claim_to_doc
                    ]
                    ordered = [src for src, _ in resolved]
                    via_graph = [flag for _, flag in resolved]
                    graph_hop_results += sum(via_graph)
                    non_empty = bool(claim_hits)

                wanted = {doc_to_source[d] for d in question.expect}

                # An unanswerable question has no correct source, so "did we
                # retrieve it" is meaningless. What matters is whether the
                # system declines — an eval that scores these like any other
                # question rewards confident retrieval of irrelevant passages.
                if question.category == UNANSWERABLE:
                    abstain_total += 1
                    abstain_correct += int(not non_empty)
                    continue

                got_sources = set(ordered)
                ok = bool(got_sources & wanted)
                hits += ok
                bucket = by_phrasing.setdefault(question.phrasing, [0, 0])
                bucket[0] += ok
                bucket[1] += 1
                if not ok:
                    misses.append((question.id, ",".join(question.expect), question.question))

                if ok and via_graph:
                    # The hop's actual contribution: a question whose ONLY
                    # correct result was reached by walking `claim_edges` is one
                    # a passage index could not have answered. Counting
                    # "neighbours returned" instead would credit the hop for
                    # filling slots with claims nobody wanted.
                    direct = any(
                        src in wanted
                        for src, flag in zip(ordered, via_graph, strict=True)
                        if not flag
                    )
                    graph_hop_only_hits += int(not direct)

                rank = next(
                    (i + 1 for i, src in enumerate(ordered) if src in wanted),
                    None,
                )
                ranks.append(rank)
                gains.append(_ndcg_at(ordered, wanted, NDCG_K))

        answerable = len(ranks)
        return Report(
            mode=mode,
            k=k,
            total=answerable,
            hits=hits,
            by_phrasing={p: (h, t) for p, (h, t) in by_phrasing.items()},
            misses=misses,
            surface=surface,
            ndcg=(sum(gains) / len(gains)) if gains else 0.0,
            mrr=(sum(1.0 / r for r in ranks if r is not None) / len(ranks) if ranks else 0.0),
            recall_at={
                n: sum(1 for r in ranks if r is not None and r <= n) / len(ranks) for n in RECALL_KS
            }
            if ranks
            else {},
            abstain_total=abstain_total,
            abstain_correct=abstain_correct,
            graph_hop_results=graph_hop_results,
            graph_hop_only_hits=graph_hop_only_hits,
            graph_hop_enabled=walk_graph and surface == CLAIMS,
            claims_seeded=claims_seeded,
            edges_seeded=edges_seeded,
            labelled_hits=labelled_hits,
            returned_hits=returned_hits,
            corpus_documents=len(corpus),
            corpus_chunks=corpus_chunks,
            corpus_questions=len(questions),
            dataset_dir=str(dataset),
        )
    finally:
        # The fixture project is scratch; leave nothing behind.
        #
        # Both surfaces are cleaned unconditionally, not just the one this run
        # seeded: a run that raised between seeding claims and switching
        # surfaces would otherwise leave `wiki_claims` rows behind under a
        # project id nothing else knows about, and they would show up forever
        # in the `claim_key is null` health count.
        #
        # Edges before claims — `claim_edges.src_claim_id` is a foreign key to
        # `wiki_claims.id`, so the other order fails on the constraint and the
        # cleanup silently leaves both tables dirty.
        from sqlalchemy import delete

        from aleph_wiki.models import ClaimEdge, WikiClaim

        async with maker() as session:
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.project_id == project_id)
            )
            await session.execute(delete(ClaimEdge).where(ClaimEdge.project_id == project_id))
            await session.execute(delete(WikiClaim).where(WikiClaim.project_id == project_id))
            await session.commit()
        await engine.dispose()


def _both_arms(args: argparse.Namespace) -> int:
    """Control and rerank arms, over the same set, printed together.

    WS-RS6 criterion 1 asks for the two numbers "printed side by side by one
    command". The first pass produced 0.567 → 0.645 from a script in a
    session-scoped temp directory that no longer exists, so nothing committed
    could re-measure it and nothing would notice it regressing — which is
    exactly the property Part 0 exists to remove.

    Two caveats are printed with the delta rather than left to the reader,
    because both were found by adversarial review of the first measurement:

    * **Depth is part of it.** `search_corpus` fetches
      `max(rerank_window, top_k)` candidates when a reranker is present and
      `top_k` when it is not, so the rerank arm's pool strictly contains the
      control arm's. Some of the gain is seeing more, not ordering better. The
      split is not separated here; it is named so the number is not
      over-claimed.
    * **The arms share a seeding pass, not a corpus embedding.** Each arm
      re-seeds, so run-to-run variance in the corpus is not controlled for.
      Treat a delta smaller than that variance as noise.
    """
    k = args.k if args.k is not None else max(RECALL_KS)
    print("control arm (no reranking)")
    off = asyncio.run(run(k=k, rerank=False))
    print(off.render())
    print()
    print("rerank arm")
    on = asyncio.run(run(k=k, rerank=True))
    print(on.render())

    gain = on.ndcg - off.ndcg
    print()
    print(f"  {'metric':<12} {'off':>8} {'on':>8} {'delta':>8}")
    print(f"  {'nDCG@' + str(NDCG_K):<12} {off.ndcg:>8.3f} {on.ndcg:>8.3f} {gain:>+8.3f}")
    print(f"  {'MRR':<12} {off.mrr:>8.3f} {on.mrr:>8.3f} {on.mrr - off.mrr:>+8.3f}")
    for cut in sorted(set(off.recall_at) | set(on.recall_at)):
        a = off.recall_at.get(cut, 0.0)
        b = on.recall_at.get(cut, 0.0)
        print(f"  {'recall@' + str(cut):<12} {a:>8.2f} {b:>8.2f} {b - a:>+8.2f}")

    if "rerank on" not in on.mode:
        print(
            f"\n  NOTE: the rerank arm did not rerank — {on.mode}. "
            "The delta below is between two identical runs."
        )
    print(
        "\n  Part of any gain is DEPTH, not judgement: the rerank arm fetches "
        f"max(rerank_window, {k}) candidates and the control arm fetches {k}, "
        "so the rerank arm's pool contains the control arm's plus hits the "
        "control arm can never surface."
    )

    if args.min_rerank_gain is not None and gain < args.min_rerank_gain:
        print(
            f"\nFAIL: reranking gained {gain:+.3f} nDCG@{NDCG_K}, "
            f"required {args.min_rerank_gain:+.3f}"
        )
        return 1
    return 0


def _both_surfaces(args: argparse.Namespace) -> int:
    """Chunks and claims over the same questions, printed side by side.

    WS-RS10 c1/c3. The question is real and its answer is not known: a claim is
    short, deduplicated and evidence-anchored, which should help retrieval; it
    is also a lossy restatement of a passage, which should hurt. Until this
    existed there was no number either way, and `docs/acceptance.md` was asking
    for a comparison no command could produce.

    **It is a measurement and it gates nothing.** `docs/decisions.md` D1 keeps
    both knowledge plugins, so a threshold here would re-open a decision the
    project has closed. The only non-zero exit is a missing claim layer, which
    is a broken input rather than a result.

    Three things the columns do not say on their own, printed with them:

    * **The claim layer is smaller by construction.** It holds one row per
      belief the project reached, against one row per ~1,900-character passage.
      A lower recall on a hundredth of the rows is not the same failure as a
      lower recall on the same rows.
    * **Only chunk-anchored claims are in it.** A claim whose citations carry
      no `chunk_id` cannot be attributed to a document, so it is dropped at
      build time rather than credited to its whole source. On an instance with
      many pre-RS8 thin citations that is most of the claim layer.
    * **Reranking is applied to chunks only.** `search_claims` takes no
      reranker, so `--rerank on --surface both` compares a reranked chunk arm
      against an unreranked claim arm. Named rather than silently allowed.
    """
    k = args.k if args.k is not None else max(RECALL_KS)
    print("chunk surface (search_corpus over passages)")
    chunks = asyncio.run(run(k=k, rerank=args.rerank == "on", surface=CHUNKS))
    print(chunks.render())
    print()
    print("claim surface (search_claims over live beliefs)")
    try:
        claims = asyncio.run(run(k=k, surface=CLAIMS, walk_graph=not args.no_graph_hop))
    except NoClaimLayer as exc:
        print(f"  n/a — {exc}")
        return 1
    print(claims.render())

    print()
    print(f"  {'metric':<12} {'chunks':>8} {'claims':>8} {'delta':>8}")
    rows: list[tuple[str, float, float]] = [
        (f"nDCG@{NDCG_K}", chunks.ndcg, claims.ndcg),
        ("MRR", chunks.mrr, claims.mrr),
    ]
    for cut in sorted(set(chunks.recall_at) | set(claims.recall_at)):
        rows.append(
            (f"recall@{cut}", chunks.recall_at.get(cut, 0.0), claims.recall_at.get(cut, 0.0))
        )
    for label, a, b in rows:
        print(f"  {label:<12} {a:>8.3f} {b:>8.3f} {b - a:>+8.3f}")

    print(
        f"\n  The claim arm searched {claims.claims_seeded} claims and "
        f"{claims.edges_seeded} edges; the chunk arm searched the whole corpus. "
        "A claim layer this much smaller is not a like-for-like index, and the "
        "delta should be read as 'can the conclusions answer it at all', not as "
        "'which retriever is better'."
    )
    if args.rerank == "on":
        print(
            "  NOTE: --rerank on applies to the CHUNK arm only. `search_claims` "
            "takes no reranker, so this comparison is reranked-vs-unreranked."
        )
    if claims.graph_hop_enabled and claims.graph_hop_only_hits == 0:
        print(
            "  The graph hop answered 0 questions that were not already answered "
            f"directly ({claims.graph_hop_results} neighbour(s) returned). On this "
            "instance `claim_edges` holds no traversable rows at all, because "
            "nothing in production writes `derived_from` — WS-RS9 c4. The hop "
            "cannot be judged until it has a graph to walk."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieval recall against the real retriever")
    parser.add_argument(
        "-k",
        type=int,
        default=None,
        help="a single top-k. Omit for the default sweep, which is more informative: "
        "recall@8 over a 12-document corpus is nearly free, and recall@1 is where "
        "the ranking is actually tested.",
    )
    parser.add_argument(
        "--min-recall",
        type=float,
        default=None,
        help=(
            "exit non-zero below this recall@1 — a gate, only once a baseline "
            "is known. @1, not top-k: the top-k rate is 1.00 on the committed "
            "set whatever retrieval does, so a floor on it cannot fail."
        ),
    )
    parser.add_argument(
        "--rerank",
        choices=("off", "on", "both"),
        default="off",
        help=(
            "second-stage reranking. `both` runs the control arm and the "
            "rerank arm over the same seeded corpus and prints them side by "
            "side, which is the only form in which the delta means anything."
        ),
    )
    parser.add_argument(
        "--min-rerank-gain",
        type=float,
        default=None,
        help=(
            "with --rerank both, exit non-zero if reranking does not improve "
            f"nDCG@{NDCG_K} by at least this much. WS-RS6 asks for 0.05."
        ),
    )
    parser.add_argument(
        "--surface",
        choices=(CHUNKS, CLAIMS, "both"),
        default=CHUNKS,
        help=(
            "which index to search. `chunks` is search_corpus over passages — "
            "what the project collected. `claims` is search_claims over live "
            "beliefs — what it concluded. `both` prints the two side by side. "
            "There is deliberately no floor on the comparison: decisions.md D1 "
            "keeps both knowledge plugins, so this is a measurement."
        ),
    )
    parser.add_argument(
        "--no-graph-hop",
        action="store_true",
        help=(
            "with --surface claims, do not walk `claim_edges` after fusion. The "
            "control arm for WS-RS10 c6: a hop that changes nothing is a hop to "
            "remove, and that cannot be established without running without it."
        ),
    )
    args = parser.parse_args()

    if args.surface == "both":
        return _both_surfaces(args)

    if args.rerank == "both":
        return _both_arms(args)

    # ONE run, not three.
    #
    # This used to call `run(k=k)` for k in (1, 3, 8) — three complete
    # seed-and-embed cycles over the whole corpus to read three numbers off the
    # same ranking. On twelve tiny documents that was free. On a real corpus it
    # is 4,216 chunks embedded three times, which is most of an hour and three
    # times the gateway spend for information one pass already has: `recall_at`
    # is computed from the rank of the first hit, so every cut-off comes out of
    # a single retrieval.
    report = asyncio.run(
        run(
            k=args.k if args.k is not None else max(RECALL_KS),
            rerank=args.rerank == "on",
            surface=args.surface,
            walk_graph=not args.no_graph_hop,
        )
    )
    print(report.render())
    print()
    # The breakdown is the useful part: a gap between verbatim and paraphrase is
    # a vocabulary-mismatch gap, which is what the dense leg is for.
    for phrasing, (hit, total) in sorted(report.by_phrasing.items()):
        print(f"  {phrasing:<14} {hit / total if total else 0:.2f}  ({hit}/{total})")

    # The floor gates recall@1, not the top-k hit rate.
    #
    # It used to compare `report.recall` — hits anywhere in the returned window.
    # At the default k that is 1.00 on the committed set whatever retrieval
    # does, so the gate could not fail. Measured: flipping `or_tsquery` back to
    # `plainto_tsquery` (AND every term, the defect this repo already fixed
    # once) moves lexical recall@1 from 0.62 to 0.36 and recall@3 from 0.96 to
    # 0.47 — while the top-k rate stays 1.00 and the gate stays green.
    #
    # @1 is where a single-answer surface lives and where a ranking regression
    # shows up first.
    gated = report.recall_at.get(1, report.recall)
    if args.min_recall is not None and gated < args.min_recall:
        print(f"\nFAIL: recall@1 {gated:.2f} < required {args.min_recall:.2f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
