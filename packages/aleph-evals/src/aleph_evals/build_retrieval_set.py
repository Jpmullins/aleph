"""Build a retrieval eval set from the corpus this instance has actually ingested.

WS-RS5. The committed set is twelve documents, none longer than sixty words,
with forty-five questions. Measured on the real retriever it scores
**recall@1 = 1.00** — completely saturated, so it cannot resolve any change RS6
or RS10 might make. A reranker could be worth one question and be
indistinguishable from noise; more likely it would be worth zero, because there
is nothing left to get right.

**Why generate rather than commit a set.** The material Aleph ingests is
published papers — arXiv, OpenAlex, IEEE. Putting their full text into this
repository is a licensing decision that belongs to whoever owns the repository,
and for at least some of them the answer is no. So this builds the set from the
operator's own database, writes it outside the source tree by default, and
records provenance for every document it emits.

**Why the questions are model-generated, and what that costs.** There is no
labelled retrieval set for this corpus and hand-labelling 150 questions is not
something to fake. A model reading a passage and asking a question about it
produces a real question with a known answer — but it also produces a question
that shares vocabulary with the passage, which flatters a lexical retriever.
Two mitigations, both in the prompt and both incomplete: the model is told to
avoid the passage's distinctive terms, and every question is tagged
`generated` so a later hand-written set can be compared against it rather than
merged into it silently.

**The unanswerable category is generated differently on purpose.** Asking a
model to write a question a passage does not answer tends to produce a question
about a neighbouring topic — which the corpus often DOES answer, elsewhere. So
they are drawn from a fixed list of subjects far outside the corpus instead. A
question the corpus can accidentally answer is not an abstention test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path
from typing import Any

#: Target document length. The plan asks for 2-15k characters, which is what a
#: real page or section looks like — long enough that chunking matters, short
#: enough that a document is about one thing.
TARGET_CHARS = 8_000
MIN_CHARS = 2_000
MAX_CHARS = 15_000

#: Subjects the corpus demonstrably does not cover, for the abstention set.
#: Fixed rather than model-invented: asked to write an unanswerable question, a
#: model reliably writes one about a neighbouring topic that the corpus answers
#: somewhere else, and a question the corpus can accidentally answer measures
#: nothing.
OFF_CORPUS_SUBJECTS = [
    "the offside rule in association football",
    "how sourdough starter is maintained",
    "the tuning of a baroque harpsichord",
    "crop rotation in medieval England",
    "the migration route of the Arctic tern",
    "how a diesel injector pump is timed",
    "the rules of contract bridge scoring",
    "glaze chemistry in raku pottery",
]

QUESTION_PROMPT = """\
You are building a retrieval evaluation set. You will be shown one passage.

Write {n} questions that this passage answers.

Rules:
1. Each question must be answerable from THIS passage alone.
2. Do NOT reuse the passage's distinctive terms. If the passage says \
"gradient-based meta-learning", ask about "learning to learn" or "adapting \
quickly to new tasks". A question that copies the passage's vocabulary tests \
string matching, not retrieval.
3. Ask what a reader would actually ask — not "what does the passage say \
about X".
4. Vary the form: some asking for a fact, some for a reason, some for a \
comparison the passage supports.
5. No question may name the document, the authors, or the passage.

Return JSON: {{"questions": ["...", "..."]}}\
"""


def _documents(rows: list[tuple[str, str, int, str]]) -> list[dict[str, Any]]:
    """Group consecutive chunks of one source into documents of realistic length.

    The chunks in the database are ~1,900 characters — a passage, not a
    document. Concatenating consecutive ones back up to ~8k reconstructs
    something document-shaped, which the eval then puts through
    `chunk_markdown` itself. That ordering matters: the eval has to do the
    chunking, or chunking is outside the measurement again.
    """
    docs: list[dict[str, Any]] = []
    current: list[str] = []
    current_source: str | None = None
    current_title = ""
    part = 0

    def flush() -> None:
        nonlocal current, part
        if not current:
            return
        text = "\n\n".join(current)
        if len(text) >= MIN_CHARS:
            docs.append(
                {
                    "doc_id": f"{current_source}#{part}",
                    "title": current_title,
                    "text": text[:MAX_CHARS],
                    "provenance": {"source_id": current_source, "part": part},
                }
            )
            part += 1
        current = []

    for source_id, title, _ordinal, text in rows:
        if source_id != current_source:
            flush()
            current_source, current_title, part = source_id, title, 0
        current.append(text)
        if sum(len(t) for t in current) >= TARGET_CHARS:
            flush()
    flush()
    return docs


def _chat_json(model: str, system: str, user: str) -> dict[str, Any]:
    """One chat call to the gateway. Raises; the caller decides what a failure means."""
    import urllib.request

    base = os.environ["LITELLM_BASE_URL"].rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.7,
    }
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['INSIGHTS_LITELLM_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read())
    return _loads_lenient(body["choices"][0]["message"]["content"])


def _loads_lenient(content: str) -> dict[str, Any]:
    """Parse JSON a model wrapped in prose or a fence.

    `response_format: {"type": "json_object"}` is a request, not a guarantee —
    the gateway passes it to whatever model is bound, and not every model
    honours it. Measured: a strict `json.loads` failed on 43 of 45 replies,
    every one of which contained valid JSON inside a ```json fence. Failing
    there produced a 16-question set and looked like the model refusing.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    msg = f"no JSON object in reply: {content[:120]!r}"
    raise ValueError(msg)


async def build(out_dir: Path, *, questions_per_doc: int, sample: int, model: str) -> int:
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import create_async_engine

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required — this builds from YOUR ingested corpus")
        return 2

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        rows = [
            (str(r.source_id), str(r.title), int(r.ordinal), str(r.text))
            for r in (
                await conn.execute(
                    sql_text(
                        "select c.source_id, s.title, c.ordinal, c.text"
                        " from document_chunks c join sources s on s.id = c.source_id"
                        " order by c.source_id, c.ordinal"
                    )
                )
            ).all()
        ]
    await engine.dispose()

    if not rows:
        print("no chunks in the database — nothing to build from (see WS-RS1)")
        return 1

    docs = _documents(rows)
    print(f"built {len(docs)} documents from {len({r[0] for r in rows})} sources")

    # handling a request. Reaching for anyio.Path here would add a dependency to
    # avoid blocking an event loop that has nothing else to do.
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    with (out_dir / "corpus.jsonl").open("w") as fh:
        for doc in docs:
            fh.write(json.dumps(doc) + "\n")

    rng = random.Random(20260822)
    chosen = rng.sample(docs, min(sample, len(docs)))
    questions: list[dict[str, Any]] = []
    failed = 0
    for index, doc in enumerate(chosen):
        try:
            reply = _chat_json(
                model,
                QUESTION_PROMPT.format(n=questions_per_doc),
                doc["text"][:12_000],
            )
        except Exception as exc:
            failed += 1
            print(f"  ! {doc['doc_id']}: {type(exc).__name__}: {exc}")
            continue
        for n, question in enumerate(reply.get("questions") or []):
            if not isinstance(question, str) or not question.strip():
                continue
            questions.append(
                {
                    "id": f"g{index}-{n}",
                    "question": question.strip(),
                    "expect": [doc["doc_id"]],
                    "phrasing": "generated",
                    "category": "factual",
                }
            )
        if (index + 1) % 10 == 0:
            print(f"  {index + 1}/{len(chosen)} documents, {len(questions)} questions")

    for n, subject in enumerate(OFF_CORPUS_SUBJECTS):
        questions.append(
            {
                "id": f"u{n}",
                "question": f"What does this collection say about {subject}?",
                # No expected document, by construction. The eval scores these
                # on returning NOTHING.
                "expect": [],
                "phrasing": "off-corpus",
                "category": "unanswerable",
            }
        )

    with (out_dir / "questions.jsonl").open("w") as fh:
        for q in questions:
            fh.write(json.dumps(q) + "\n")

    with (out_dir / "NOTICE").open("w") as fh:
        fh.write(
            "Retrieval evaluation set, GENERATED — do not redistribute without checking.\n\n"
            f"Documents: {len(docs)}, assembled from {len({r[0] for r in rows})} sources\n"
            f"already ingested into this Aleph instance. The source material is\n"
            "third-party published work (arXiv, OpenAlex, publisher HTML/PDF) and its\n"
            "licence is whatever each source carries — this file does NOT grant any\n"
            "right to redistribute it. Every document records its originating\n"
            "`source_id` under `provenance`.\n\n"
            f"Questions: {len(questions)}, of which {len(OFF_CORPUS_SUBJECTS)} are\n"
            "unanswerable-by-construction.\n\n"
            "The factual questions are MODEL-GENERATED from the passage they are\n"
            "labelled against. That makes them real questions with known answers, and\n"
            "it also makes them share vocabulary with their passage, which flatters a\n"
            "lexical retriever. They are tagged `phrasing: generated` so a later\n"
            "hand-written set can be compared against them rather than merged in.\n"
        )
    print(
        f"wrote {len(docs)} documents and {len(questions)} questions to {out_dir}"
        + (f" ({failed} document(s) failed)" if failed else "")
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.environ.get("ALEPH_RETRIEVAL_DATASET"))
    parser.add_argument("--questions-per-doc", type=int, default=4)
    parser.add_argument("--sample", type=int, default=50)
    parser.add_argument("--model", default=os.environ.get("ALEPH_EVAL_MODEL", "claude-sonnet-4-6"))
    args = parser.parse_args()
    if not args.out:
        print("--out (or ALEPH_RETRIEVAL_DATASET) is required")
        return 2
    return asyncio.run(
        build(
            Path(args.out),
            questions_per_doc=args.questions_per_doc,
            sample=args.sample,
            model=args.model,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
