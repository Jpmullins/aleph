# PDF normalizers, measured — WS-RS11 criterion 4

Run: `uv run python scripts/compare_pdf_parsers.py <dir-of-pdfs> --limit 20`
Corpus: 20 PDFs drawn from this instance's own ingested sources (97 available).
Date: 2026-08-22. Medians, not means — one 300-page PDF should not decide this.

| parser | ok | chars | **headings** | **table rows** | figures |
| --- | --- | --- | --- | --- | --- |
| pypdf | 20/20 | 78,139 | **2** | **0** | 11 |
| pdfminer | 20/20 | 78,513 | **1** | **0** | 11 |
| docling | — | not installed | | | |
| pymupdf4llm | — | not installed | | | |

## What this establishes

**Both shipped normalizers extract the words and none of the structure.** A
median of one or two headings across an entire paper, and zero markdown table
rows. The heading detector is deliberately generous — a markdown `#`, a numbered
`3.2 Method`, or a short ALL-CAPS line all count — so this flatters the
incumbents and they still produce nothing usable.

That is why `structure = {"heading_count": 0, "table_count": 0,
"figure_count": 0}` is a hardcoded literal in both
(`normalization.py:97-101`, `:135-139`): the libraries genuinely cannot tell,
and the comment beside it says so.

And it is why **every chunk from every PDF has `section_path = NULL`**. The
chunker looks for markdown headings to decide where a section begins; there are
none, so there is nothing to label. The chunker computes exact character offsets
and a test asserts the span slices back to the original — that precision is
being spent on a document with no structure to be precise about.

## The decision this does not make

Fixing it means adding a PDF stack, and the two credible candidates each carry a
real cost to whoever runs Aleph:

- **Docling** — MIT, in-process, and pulls a model stack on the order of a
  gigabyte with first-run downloads. Technically the best fit for the
  compose deployment model; materially larger images and a cold start.
- **pymupdf4llm** — AGPL unless separately licensed. That is a licence question
  before it is a technical one, and not one to answer by adding a line to
  `pyproject.toml`.
- **GROBID** — a separate service, which suits the compose model but adds a
  container and an operational surface.

`scripts/compare_pdf_parsers.py` installs nothing and picks nothing. Install a
candidate, re-run it, and the table extends itself — the numbers above are the
baseline any candidate has to beat.
