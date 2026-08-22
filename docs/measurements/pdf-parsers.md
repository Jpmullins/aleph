# PDF normalizers, measured — WS-RS11

Run: `uv run python scripts/compare_pdf_parsers.py <dir-of-pdfs> --limit 6`
Corpus: PDFs drawn from this instance's own ingested sources (97 available).
Date: 2026-08-22. Medians, not means — one 300-page PDF should not decide this.

| parser | ok | chars | headings | **md head** | **table rows** | figures |
| --- | --- | --- | --- | --- | --- | --- |
| pypdf | 6/6 | 78,041 | 25 | **0** | **0** | 17 |
| pdfminer | 6/6 | 77,270 | 26 | **0** | **0** | 15 |
| **docling 2.121.0** | 6/6 | 76,972 | 36 | **27** | **24** | 14 |

## Read the `md head` column, not `headings`

`headings` uses a generous detector — a markdown `#`, a numbered `3.2 Method`,
or a short ALL-CAPS line. That is fair for asking *did the parser see
structure*, and misleading for asking *can the chunker use it*.

`chunk_markdown` finds a section by looking for `#`. So the flat parsers' 25 and
26 are ALL-CAPS lines in a wall of text, and **none of them sets a
`section_path`**. That is why every PDF chunk carried `section_path = NULL`, and
why both normalizers hardcoded `{"heading_count": 0, "table_count": 0,
"figure_count": 0}` with a comment admitting the library could not tell.

Docling emits 27 real markdown headings and 24 table rows per paper. The tables
are the sharper result: neither shipped parser produces a single one, and a
research harness that cannot read a table in a paper is not a research harness
in any field where the result is in the table.

## Two measurement bugs found while doing this, both mine

Recorded because each would have produced a confident wrong answer.

1. **The heading regex anchored `\s*$` immediately after `#{1,6}\s+\S`**, so it
   only matched a heading with a one-character title. It reported **0** for
   docling — whose headings are all markdown — and I nearly wrote down "the
   layout parser found no structure" about a parser that had found 41 headings
   in the first document tried.
2. **`headings` and `md head` were the same column.** Merged, docling scores 36
   against pypdf's 25 and the result looks like a 40% improvement. Split, it is
   27 against 0, and the improvement is categorical rather than incremental.

## What it costs

Docling is MIT and runs in-process, so no new container. It pulls a model stack
of roughly a gigabyte and downloads weights on first use, which is why it is an
**extra** (`aleph-rks[pdf-layout]`) rather than a dependency: a deployment that
ingests HTML should not carry a PDF model stack, and one that has not installed
it degrades to the flat parsers rather than failing to ingest.

`pymupdf4llm` was not measured. It is AGPL unless separately licensed, which is
a question to answer before running it, not after.

`ALEPH_PDF_PARSER` pins one by name — and pins it, without falling back, so a
comparison run cannot silently measure a different parser than the one it names.
