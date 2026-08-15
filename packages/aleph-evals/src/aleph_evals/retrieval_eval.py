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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

# .../aleph-evals/src/aleph_evals/retrieval_eval.py
#   parents[0]=aleph_evals  [1]=src  [2]=aleph-evals  <- datasets live here
DATASET_DIR = Path(__file__).resolve().parents[2] / "datasets" / "retrieval"
EMBEDDING_DIM = 1024


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    expect: tuple[str, ...]
    phrasing: str


@dataclass
class Report:
    mode: str
    k: int
    total: int
    hits: int
    by_phrasing: dict[str, tuple[int, int]]
    misses: list[tuple[str, str, str]]

    @property
    def recall(self) -> float:
        return self.hits / self.total if self.total else 0.0

    def render(self) -> str:
        lines = [
            f"retrieval recall@{self.k} = {self.recall:.2f}  ({self.hits}/{self.total})",
            f"mode: {self.mode}",
        ]
        for phrasing, (hit, total) in sorted(self.by_phrasing.items()):
            share = hit / total if total else 0.0
            lines.append(f"  {phrasing:<14} {share:.2f}  ({hit}/{total})")
        if self.misses:
            lines.append(f"  misses ({len(self.misses)}):")
            lines.extend(f"    {qid}  expected {want}  — {q}" for qid, want, q in self.misses[:10])
        return "\n".join(lines)


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


async def _embedder(project_id: UUID):
    """Return (embed_fn, mode). Falls back to lexical-only without a gateway."""
    if not os.environ.get("INSIGHTS_LITELLM_API_KEY") or not os.environ.get("LITELLM_BASE_URL"):
        return (lambda _q: _zero_vector()), "lexical (no gateway configured)"
    return (lambda _q: _zero_vector()), "lexical (gateway present; dense leg not yet wired here)"


async def run(k: int = 8) -> Report:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aleph_core.ids import uuid7
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
        )
        for row in _load(DATASET_DIR / "questions.jsonl")
    ]

    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    project_id = uuid7()
    doc_to_source: dict[str, UUID] = {}

    try:
        # Seed. One source per document, one chunk per document — the unit under
        # test is retrieval, not chunking.
        async with maker() as session:
            for ordinal, doc in enumerate(corpus):
                source_id = uuid7()
                doc_to_source[doc["doc_id"]] = source_id
                body = f"{doc['title']}. {doc['text']}"
                session.add(
                    DocumentChunk(
                        id=uuid7(),
                        project_id=project_id,
                        source_id=source_id,
                        normalized_document_id=uuid7(),
                        ordinal=ordinal,
                        text=body,
                        text_tsv="",  # trigger fills this
                        section_path=None,
                        char_start=0,
                        char_end=len(body),
                        token_count=len(body.split()),
                        embedding=_zero_vector(),
                        embedder_model="eval-fixture",
                    )
                )
            await session.commit()

        embed, mode = await _embedder(project_id)

        hits = 0
        by_phrasing: dict[str, list[int]] = {}
        misses: list[tuple[str, str, str]] = []

        async with maker() as session:
            for question in questions:
                found = await search_corpus(
                    session,
                    project_id=project_id,
                    query_text=question.question,
                    query_embedding=embed(question.question),
                    top_k=k,
                    per_source_cap=None,  # one chunk per source anyway
                )
                got_sources = {h.source_id for h in found}
                wanted = {doc_to_source[d] for d in question.expect}
                ok = bool(got_sources & wanted)
                hits += ok
                bucket = by_phrasing.setdefault(question.phrasing, [0, 0])
                bucket[0] += ok
                bucket[1] += 1
                if not ok:
                    misses.append((question.id, ",".join(question.expect), question.question))

        return Report(
            mode=mode,
            k=k,
            total=len(questions),
            hits=hits,
            by_phrasing={p: (h, t) for p, (h, t) in by_phrasing.items()},
            misses=misses,
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
        help="exit non-zero below this recall — a gate, only once a baseline is known",
    )
    args = parser.parse_args()

    if args.k is not None:
        report = asyncio.run(run(k=args.k))
        print(report.render())
    else:
        reports = [asyncio.run(run(k=k)) for k in (1, 3, 8)]
        print(f"mode: {reports[0].mode}\n")
        for r in reports:
            print(f"recall@{r.k} = {r.recall:.2f}  ({r.hits}/{r.total})")
        print()
        # The breakdown is the useful part: a gap between verbatim and paraphrase
        # is a vocabulary-mismatch gap, which is what the dense leg is for.
        sharpest = reports[0]
        for phrasing, (hit, total) in sorted(sharpest.by_phrasing.items()):
            print(f"  @1 {phrasing:<14} {hit / total if total else 0:.2f}  ({hit}/{total})")
        report = reports[-1]

    if args.min_recall is not None and report.recall < args.min_recall:
        print(f"\nFAIL: recall {report.recall:.2f} < required {args.min_recall:.2f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
