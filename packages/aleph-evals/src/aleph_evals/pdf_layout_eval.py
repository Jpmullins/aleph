"""Does layout-aware PDF parsing change what retrieval finds? — WS-RS11 c5.

The plan chose docling over pypdf and pdfminer on structure counts
(`docs/measurements/pdf-parsers.md`: 27 markdown headings and 24 table rows per
paper against zero and zero). Structure counts are not a retrieval result, and
the criterion is explicit that the gain may be small and that the number is
produced **either way**. This produces it.

**One corpus, parsed twice, one set of questions.** Every PDF is normalized
through the real `normalize_bytes` — pinned to each parser by `ALEPH_PDF_PARSER`,
which `pdf_normalizer` honours WITHOUT falling back, precisely so a comparison
run cannot silently measure the same parser twice. Each arm is then handed to
`aleph_evals.retrieval_eval.run`, so chunking, embedding, fusion, the per-source
cap and `search_corpus` itself are identical between the arms and identical to
production.

**The questions are generated from the FLAT text, on purpose.** A question
written from docling's markdown would share its vocabulary and its section
titles, and the comparison would be measuring the question-writer. Generating
from pypdf's output biases the measurement AGAINST the parser under test, which
is the direction a comparison is allowed to be wrong in.

**Two numbers, and the second is the honest one.**

* *nDCG@10, MRR, recall@k.* Gold is the DOCUMENT, because that is the only label
  both arms can share: the arms chunk differently, so there is no passage id
  that means the same thing in both. Document-level gold over a few dozen papers
  is an easier task than the 738-part generated set, so read these as "did
  layout parsing break or improve document routing", not as an absolute.
* *Section-label rate.* The share of RETRIEVED passages that carry a
  `section_path`. This is what layout parsing actually buys and no ranking
  metric can see it: the passages contain the same words either way, so a
  reader loses only the ability to see where in the paper the answer came from —
  and `section_path` is also the input RS6's contextual embeddings need.

Run it::

    uv run python -m aleph_evals.pdf_layout_eval <dir-of-pdfs> --limit 40

`DATABASE_URL`, `LITELLM_BASE_URL` and `INSIGHTS_LITELLM_API_KEY` are required:
this drives the real retriever against real Postgres and a real embedder, the
same as `retrieval_eval`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aleph_evals.retrieval_eval import Report

#: The parsers compared. `layout` is the one under test; `flat` is the incumbent
#: and is also what the questions are written from.
FLAT_PARSER = "pypdf"
LAYOUT_PARSER = "docling"

#: Off-corpus questions are NOT included. This set exists to compare two
#: renderings of the same documents; an abstention rate would be a property of
#: the reranker, measured twice, and reported as though the parser moved it.


def _normalize(path: Path, parser: str) -> tuple[str, dict[str, int]]:
    """One PDF through the production normalizer, pinned to one parser.

    `normalize_bytes` and not `Normalizer.normalize`: it is the single boundary
    every ingested byte crosses, and it is where `defang` runs. Measuring a
    string production never stores would repeat WS-RS5's own defect one layer
    down.
    """
    from aleph_rks.normalization import normalize_bytes

    previous = os.environ.get("ALEPH_PDF_PARSER")
    os.environ["ALEPH_PDF_PARSER"] = parser
    try:
        result = normalize_bytes(path.read_bytes(), "application/pdf")
    finally:
        if previous is None:
            os.environ.pop("ALEPH_PDF_PARSER", None)
        else:
            os.environ["ALEPH_PDF_PARSER"] = previous
    if result.parser != parser:
        # `pdf_normalizer` pins without falling back, so this cannot happen —
        # asserted anyway, because the whole comparison is void if it does and
        # the failure would otherwise present as "the two parsers agree".
        msg = f"asked for {parser!r} and got {result.parser!r}; the comparison would be void"
        raise SystemExit(msg)
    return result.markdown, {k: int(v) for k, v in result.structure.items()}


def _write_arm(
    out_dir: Path,
    docs: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "corpus.jsonl").open("w") as fh:
        for doc in docs:
            fh.write(json.dumps(doc) + "\n")
    with (out_dir / "questions.jsonl").open("w") as fh:
        for question in questions:
            fh.write(json.dumps(question) + "\n")


def _questions_for(docs: list[dict[str, Any]], *, per_doc: int, model: str) -> list[dict[str, Any]]:
    """Model-written questions, one batch per document, from the flat text.

    Reuses `build_retrieval_set`'s prompt and its lenient JSON reader rather
    than writing a second copy: the prompt carries the "do not reuse the
    passage's distinctive terms" rule, which is the only thing keeping a
    generated question from being a lexical-match test.
    """
    from aleph_evals.build_retrieval_set import QUESTION_PROMPT, _chat_json

    questions: list[dict[str, Any]] = []
    failed = 0
    for index, doc in enumerate(docs):
        try:
            reply = _chat_json(model, QUESTION_PROMPT.format(n=per_doc), doc["text"][:12_000])
        except Exception as exc:
            failed += 1
            print(f"  ! {doc['doc_id']}: {type(exc).__name__}: {exc}")
            continue
        for n, question in enumerate(reply.get("questions") or []):
            if not isinstance(question, str) or not question.strip():
                continue
            questions.append(
                {
                    "id": f"p{index}-{n}",
                    "question": question.strip(),
                    "expect": [doc["doc_id"]],
                    "phrasing": "generated-from-flat",
                    "category": "factual",
                }
            )
        if (index + 1) % 10 == 0:
            print(f"  {index + 1}/{len(docs)} documents, {len(questions)} questions")
    if failed:
        print(f"  ({failed} document(s) produced no questions)")
    return questions


def _row(label: str, flat: float, layout: float) -> str:
    return f"  {label:<14} {flat:>8.3f} {layout:>8.3f} {layout - flat:>+8.3f}"


def build_and_measure(
    pdf_dir: Path,
    *,
    out_dir: Path,
    limit: int,
    per_doc: int,
    model: str,
    reuse: bool,
) -> int:
    from aleph_evals.retrieval_eval import NDCG_K, RECALL_KS, run

    pdfs = sorted(p for p in pdf_dir.rglob("*.pdf"))[:limit]
    if not pdfs:
        print(f"no PDFs under {pdf_dir}")
        return 2

    flat_dir = out_dir / "flat"
    layout_dir = out_dir / "layout"

    if reuse and (flat_dir / "corpus.jsonl").exists() and (layout_dir / "corpus.jsonl").exists():
        print(f"reusing the parsed corpus under {out_dir} (--reuse)")
    else:
        flat_docs: list[dict[str, Any]] = []
        layout_docs: list[dict[str, Any]] = []
        structure = {FLAT_PARSER: [0, 0], LAYOUT_PARSER: [0, 0]}
        for n, pdf in enumerate(pdfs):
            doc_id = pdf.stem[:24]
            try:
                flat_md, flat_struct = _normalize(pdf, FLAT_PARSER)
                layout_md, layout_struct = _normalize(pdf, LAYOUT_PARSER)
            except SystemExit:
                raise
            except Exception as exc:
                print(f"  ! {pdf.name}: {type(exc).__name__}: {exc}")
                continue
            # A document one parser could not read is dropped from BOTH arms.
            # Keeping it in one would hand that arm a document the other cannot
            # possibly retrieve, and the delta would measure the drop.
            if not flat_md.strip() or not layout_md.strip():
                print(f"  ! {pdf.name}: empty from one parser; dropped from both arms")
                continue
            title = f"paper-{n}"
            flat_docs.append({"doc_id": doc_id, "title": title, "text": flat_md})
            layout_docs.append({"doc_id": doc_id, "title": title, "text": layout_md})
            structure[FLAT_PARSER][0] += flat_struct.get("heading_count", 0)
            structure[FLAT_PARSER][1] += flat_struct.get("table_count", 0)
            structure[LAYOUT_PARSER][0] += layout_struct.get("heading_count", 0)
            structure[LAYOUT_PARSER][1] += layout_struct.get("table_count", 0)
            if (n + 1) % 5 == 0:
                print(f"  parsed {n + 1}/{len(pdfs)}")

        if not flat_docs:
            print("every PDF failed to parse")
            return 1
        print(
            f"parsed {len(flat_docs)} PDF(s) twice · headings "
            f"{FLAT_PARSER}={structure[FLAT_PARSER][0]} {LAYOUT_PARSER}="
            f"{structure[LAYOUT_PARSER][0]} · table rows "
            f"{FLAT_PARSER}={structure[FLAT_PARSER][1]} {LAYOUT_PARSER}="
            f"{structure[LAYOUT_PARSER][1]}"
        )

        print(f"writing {per_doc} question(s) per document from the FLAT text ({model})")
        questions = _questions_for(flat_docs, per_doc=per_doc, model=model)
        if not questions:
            print("no questions were generated — the gateway answered nothing")
            return 1
        _write_arm(flat_dir, flat_docs, questions)
        _write_arm(layout_dir, layout_docs, questions)

    print(f"\n{FLAT_PARSER} arm (flat text)")
    flat: Report = asyncio.run(run(k=max(RECALL_KS), dataset_dir=flat_dir))
    print(flat.render())
    print(f"\n{LAYOUT_PARSER} arm (layout-aware)")
    layout: Report = asyncio.run(run(k=max(RECALL_KS), dataset_dir=layout_dir))
    print(layout.render())

    print()
    print(f"  {'metric':<14} {FLAT_PARSER:>8} {LAYOUT_PARSER:>8} {'delta':>8}")
    print(_row(f"nDCG@{NDCG_K}", flat.ndcg, layout.ndcg))
    print(_row("MRR", flat.mrr, layout.mrr))
    for cut in sorted(set(flat.recall_at) | set(layout.recall_at)):
        print(_row(f"recall@{cut}", flat.recall_at.get(cut, 0.0), layout.recall_at.get(cut, 0.0)))
    print(_row("section-label", flat.section_label_rate, layout.section_label_rate))

    print(
        "\n  Gold is the DOCUMENT, not the passage: the two arms chunk "
        "differently, so no passage id means the same thing in both. Over "
        f"{flat.corpus_documents} documents that is an easier task than the "
        "generated part-level set, and the ranking deltas should be read as "
        "'did layout parsing break document routing', not as an absolute."
    )
    print(
        "  The section-label row is the one the ranking metrics cannot see. "
        "Both arms hold the same words, so a flat parser costs the reader the "
        "ability to see WHERE in the paper an answer came from — and costs RS6's "
        "contextual embeddings their only input."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_dir", type=Path, help="directory of PDFs, searched recursively")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="where the two parsed corpora are written (default: a temp dir under the CWD)",
    )
    parser.add_argument("--limit", type=int, default=40, help="how many PDFs to parse")
    parser.add_argument("--questions-per-doc", type=int, default=4)
    parser.add_argument("--model", default=os.environ.get("ALEPH_EVAL_MODEL", "claude-sonnet-4-6"))
    parser.add_argument(
        "--reuse",
        action="store_true",
        help=(
            "re-measure an --out directory that already holds both arms, without "
            "re-parsing or re-generating. Docling is ~9s per paper and the "
            "questions cost gateway spend; a second measurement of the SAME set "
            "is also how run-to-run variance is established."
        ),
    )
    args = parser.parse_args()
    out = args.out or Path("pdf-layout-eval")
    return build_and_measure(
        args.pdf_dir,
        out_dir=out,
        limit=args.limit,
        per_doc=args.questions_per_doc,
        model=args.model,
        reuse=args.reuse,
    )


if __name__ == "__main__":
    sys.exit(main())
