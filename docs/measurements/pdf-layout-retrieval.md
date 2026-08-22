# Layout-aware PDF parsing vs flat text, measured on retrieval — WS-RS11 c5

Run: `uv run python -m aleph_evals.pdf_layout_eval <dir-of-pdfs> --limit 60 --questions-per-doc 3`
Corpus: 57 PDFs drawn from this instance's own ingested sources (60 attempted, 3 unreadable by one parser and dropped from **both** arms).
Questions: 171, model-written from the **flat** text — see "the bias runs against docling", below.
Date: 2026-08-22. Gateway: `bedrock-titan-embed-text` dense leg, reranking off.

`docs/measurements/pdf-parsers.md` chose docling on structure counts. This is the
retrieval number the plan asked for next, and the plan is explicit that the gain
may be small and that **the number is produced either way**.

| metric | pypdf (flat) | docling (layout) | delta |
| --- | --- | --- | --- |
| nDCG@10 | 0.821 | **0.831** | **+0.011** |
| MRR | 0.783 | 0.785 | +0.002 |
| recall@1 | 0.696 | 0.702 | +0.006 |
| recall@3 | 0.854 | 0.830 | **−0.023** |
| recall@8 | 0.936 | 0.977 | +0.041 |
| recall@20 | 0.994 | 1.000 | +0.006 |
| **section-label rate** | **0.020** | **1.000** | **+0.980** |

Structure, as parsed: **1,806 headings and 3,189 table rows** across the 57
papers for docling; **0 and 0** for pypdf. 3,135 chunks in the flat arm, 3,611 in
the layout arm.

**Repeated on the same set** (`--reuse`, which re-seeds and re-measures without
re-parsing), because a delta this small is worth nothing without knowing the
noise floor:

| run | pypdf nDCG@10 | docling nDCG@10 | delta |
| --- | --- | --- | --- |
| 1 | 0.821 | 0.831 | +0.011 |
| 2 | 0.821 | 0.829 | +0.008 |

The flat arm reproduced exactly; the layout arm moved 0.002. So the gain is
above the noise floor on this set — and it is four times the noise floor, not
forty, which is the honest way to read +0.01.

## Read the last row, not the first

The ranking gain is real and it is small. +0.011 nDCG@10 over 171 questions is
roughly two questions, against a measured run-to-run spread of 0.002 on the same
set. The honest summary of rows one to six is **layout parsing did not break
document routing, and improved it slightly**, and the first half was the real
risk: docling emits different text, different
chunk boundaries and 15% more chunks, and any of those could have cost recall.
recall@3 did fall by 0.023 and recall@8 rose by 0.041, which is what shuffling
inside a window looks like.

The last row is not inside noise, and it is the one no ranking metric can see.
**2% of retrieved passages carried a section label in the flat arm; 100% did in
the layout arm.** Both arms hold the same words, so recall cannot tell them
apart — what the flat arm loses is every reader's ability to see *where in the
paper* an answer came from, and every downstream consumer of `section_path`:

* the grounding chain ends at a character span inside a passage nobody can place;
* RS6's context-aware chunk embeddings take `section_path` as their input, so on
  a flat corpus that improvement has nothing to work with;
* the 2% in the flat arm is not pypdf finding structure — it is the handful of
  ALL-CAPS lines `chunk_markdown` mistook for a heading.

## Why the measurement is shaped the way it is

**One corpus, parsed twice, one set of questions.** Every PDF goes through the
real `normalize_bytes`, pinned per arm by `ALEPH_PDF_PARSER` — which
`pdf_normalizer` honours *without falling back*, so a comparison run cannot
silently measure the same parser twice. Both arms are then handed to
`aleph_evals.retrieval_eval.run`, so chunking, embedding, RRF, the per-source cap
and `search_corpus` are identical between them and identical to production.

**The bias runs against docling.** The questions are model-written from the
**pypdf** rendering. A question written from docling's markdown would share its
vocabulary and its section titles, and the comparison would be measuring the
question-writer. Generating from the incumbent's output is the direction a
comparison is allowed to be wrong in — and the flat arm still lost, narrowly.

**Gold is the document, not the passage.** The two arms chunk differently, so no
passage id means the same thing in both; the document is the only label they can
share. Over 57 documents that is an easier task than the 738-part generated set
(where recall@1 is 0.34), which is why recall@20 is at the ceiling here. Read the
deltas, not the levels.

**Three PDFs were dropped from both arms**, not one. Keeping a document one
parser could not read would hand the other arm a document its rival cannot
possibly retrieve, and the delta would be measuring the drop.

## Reproducing it

The generated set is **not committed** — same reason as the RS5 retrieval set:
the corpus is published papers and redistributing them is not the eval's call.
Point the command at your own PDFs. `--reuse` re-measures an existing pair of
arms without re-parsing, which is also how run-to-run variance is established
(docling costs ~13 s per paper on this hardware, ~44 s inside the worker image).
