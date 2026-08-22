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

    @property
    def recall(self) -> float:
        return self.hits / self.total if self.total else 0.0

    @property
    def abstain_rate(self) -> float:
        return self.abstain_correct / self.abstain_total if self.abstain_total else 0.0

    def render(self) -> str:
        lines = [
            f"retrieval recall@{self.k} = {self.recall:.2f}  ({self.hits}/{self.total})",
            f"mode: {self.mode}",
            f"  nDCG@{NDCG_K}  {self.ndcg:.3f}",
            f"  MRR       {self.mrr:.3f}",
        ]
        if self.recall_at:
            lines.append(
                "  recall    "
                + "  ".join(f"@{n} {v:.2f}" for n, v in sorted(self.recall_at.items()))
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
    client = LiteLLMClient(
        base_url=os.environ["LITELLM_BASE_URL"],
        api_key=os.environ["INSIGHTS_LITELLM_API_KEY"],
        http_client=httpx.AsyncClient(timeout=120.0),
        pricing=PricingTable(),
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


async def run(k: int = 8, *, rerank: bool = False) -> Report:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aleph_core.ids import uuid7
    from aleph_rks.chunking import Chunk, chunk_markdown
    from aleph_rks.indexing import embedding_text
    from aleph_rks.models import DocumentChunk
    from aleph_rks.retrieval import search_corpus

    url = os.environ.get("ALEPH_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        msg = "ALEPH_DATABASE_URL or DATABASE_URL is required"
        raise SystemExit(msg)

    corpus = _load(DATASET_DIR / "corpus.jsonl")
    questions = [
        Question(
            id=row["id"],
            question=row["question"],
            expect=tuple(row["expect"]),
            phrasing=row.get("phrasing", "unspecified"),
            category=row.get("category", "factual"),
        )
        for row in _load(DATASET_DIR / "questions.jsonl")
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

        seeded: list[tuple[str, Chunk]] = []
        for doc in corpus:
            for chunk in chunk_markdown(doc["text"]):
                seeded.append((doc["doc_id"], chunk))

        # Embed the corpus in one batch. Seeding zero vectors while querying a
        # real one would leave the dense leg ranking nothing and quietly report
        # the lexical number as "hybrid".
        #
        # `embedding_text` is the SAME function the ingest path calls. The eval
        # used to embed `f"{title}. {text}"` while production embedded the raw
        # chunk, so every number ever reported was measured against a
        # better-conditioned corpus than the running system produces.
        bodies = [
            embedding_text(chunk_text=chunk.text, title=_title_of(corpus, doc_id))
            for doc_id, chunk in seeded
        ]
        doc_vectors: list[list[float]] | None = None
        if mode.startswith("hybrid"):
            bound_for_seed = await _embedding_model(maker)
            model = bound_for_seed[0] if bound_for_seed else None
            doc_vectors = _gateway_embed(str(model), bodies)

        for doc in corpus:
            doc_to_source[doc["doc_id"]] = uuid7()

        # Committed in batches.
        #
        # This was one session and one flush for the whole corpus. Twelve
        # documents meant twelve rows; a real corpus means thousands, and
        # asyncpg drops the connection mid-`executemany` — `connection was
        # closed in the middle of operation`, after several minutes of
        # embedding, with nothing salvaged. Like the `413` on the embed side,
        # the old set was too small for the transport to matter.
        for offset in range(0, len(seeded), SEED_BATCH):
            await _seed_batch(
                maker,
                seeded[offset : offset + SEED_BATCH],
                doc_to_source=doc_to_source,
                vectors=doc_vectors,
                project_id=project_id,
                base=offset,
            )

        hits = 0
        by_phrasing: dict[str, list[int]] = {}
        misses: list[tuple[str, str, str]] = []
        ranks: list[int | None] = []
        gains: list[float] = []
        abstain_total = 0
        abstain_correct = 0

        async with maker() as session:
            for question in questions:
                found = await search_corpus(
                    session,
                    project_id=project_id,
                    query_text=question.question,
                    query_embedding=embed(question.question),
                    top_k=max(k, NDCG_K),
                    # The real cap, not `None`.
                    #
                    # It was disabled with the comment "one chunk per source
                    # anyway", which was true of the old one-chunk-per-document
                    # seeding and is not true now. The cap exists to stop a
                    # single long document filling every slot, and a measurement
                    # that switches it off cannot see it working — or failing.
                    per_source_cap=PER_SOURCE_CAP,
                    reranker=reranker,
                )
                wanted = {doc_to_source[d] for d in question.expect}

                # An unanswerable question has no correct source, so "did we
                # retrieve it" is meaningless. What matters is whether the
                # system declines — an eval that scores these like any other
                # question rewards confident retrieval of irrelevant passages.
                if question.category == UNANSWERABLE:
                    abstain_total += 1
                    abstain_correct += int(not found)
                    continue

                ordered = [h.source_id for h in found]
                got_sources = set(ordered)
                ok = bool(got_sources & wanted)
                hits += ok
                bucket = by_phrasing.setdefault(question.phrasing, [0, 0])
                bucket[0] += ok
                bucket[1] += 1
                if not ok:
                    misses.append((question.id, ",".join(question.expect), question.question))

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
            ndcg=(sum(gains) / len(gains)) if gains else 0.0,
            mrr=(sum(1.0 / r for r in ranks if r is not None) / len(ranks) if ranks else 0.0),
            recall_at={
                n: sum(1 for r in ranks if r is not None and r <= n) / len(ranks) for n in RECALL_KS
            }
            if ranks
            else {},
            abstain_total=abstain_total,
            abstain_correct=abstain_correct,
        )
    finally:
        # The fixture project is scratch; leave nothing behind.
        from sqlalchemy import delete

        async with maker() as session:
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.project_id == project_id)
            )
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
    args = parser.parse_args()

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
        run(k=args.k if args.k is not None else max(RECALL_KS), rerank=args.rerank == "on")
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
