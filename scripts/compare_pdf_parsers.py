"""Compare PDF normalizers on numbers, per WS-RS11 criterion 4.

Aleph reads PDFs as a flat wall of words. Both shipped normalizers hardcode

    structure = {"heading_count": 0, "table_count": 0, "figure_count": 0}

with a comment admitting the library cannot tell. The chunker decides where a
section begins by looking for markdown headings, so **every passage from every
PDF carries `section_path = NULL`** — by construction, not by accident. That
caps grounding below what the rest of the machinery can deliver: the chunker
computes exact character offsets and a test asserts the span slices back to the
original, and that precision is spent on a document with no structure to be
precise about.

Choosing a replacement means adding a dependency, and the candidates are not
small — Docling pulls a model stack, GROBID is a separate service. That is a
decision with a real cost to whoever runs this, so this script exists to make it
on evidence instead of on reputation. It measures what is installed, names what
is not, and prints per-PDF numbers.

It deliberately does NOT install anything or pick a winner. Run it, read the
table, then decide.
"""

from __future__ import annotations

import argparse

#: A line that reads as a heading in the extracted text.
#:
#: Deliberately generous — a markdown `#`, an ALL-CAPS short line, or a numbered
#: section like `3.2 Method`. Over-counting is the safer error here: it flatters
#: the incumbents, so a layout-aware parser that still wins wins clearly.
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: NOTE the `.*` on the markdown branch. Without it the `\s*$` anchor demanded
#: end-of-line immediately after the first title character, so `## ABSTRACT`
#: did not match and only a one-character heading did. That undercounted every
#: parser and reported **zero** for docling, whose headings are all markdown —
#: which would have been read as "the layout parser found no structure" when it
#: had found 41 headings in the first document tried.
_HEADING = re.compile(
    r"^(?:#{1,6}[ \t]+\S.*|(?:\d+\.)+\d*[ \t]+[A-Z].*|[A-Z][A-Z \-]{4,60})[ \t]*$",
    re.MULTILINE,
)
#: A markdown table row. The only table shape a downstream chunker can use.
#: A MARKDOWN heading, and this is the column that decides the criterion.
#:
#: `chunk_markdown` finds a section by looking for `#`. The generous detector
#: above also counts an ALL-CAPS line and a numbered `3.2 Method`, which is
#: fair for judging "did the parser see structure" and MISLEADING for judging
#: "can the chunker use it" — a flat parser scores 25 on the first and 0 on the
#: second, and only the second sets `section_path`.
_MD_HEADING = re.compile(r"^#{1,6}[ \t]+\S", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\s*\|.+\|\s*$", re.MULTILINE)
_FIGURE = re.compile(r"^\s*(?:Figure|Fig\.|Table)\s+\d+", re.MULTILINE | re.IGNORECASE)


@dataclass
class Measurement:
    parser: str
    chars: int = 0
    headings: int = 0
    md_headings: int = 0
    table_rows: int = 0
    figures: int = 0
    failed: str | None = None


@dataclass
class Parser:
    name: str
    available: bool
    why_not: str = ""
    run: Any = None
    #: What adopting it costs, stated so the table is a decision aid rather
    #: than a scoreboard.
    cost: str = ""
    _m: list[Measurement] = field(default_factory=list)


def _measure(name: str, text: str) -> Measurement:
    return Measurement(
        parser=name,
        chars=len(text),
        headings=len(_HEADING.findall(text)),
        md_headings=len(_MD_HEADING.findall(text)),
        table_rows=len(_TABLE_ROW.findall(text)),
        figures=len(_FIGURE.findall(text)),
    )


def _pypdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join((p.extract_text() or "") for p in reader.pages)


def _pdfminer(path: Path) -> str:
    from pdfminer.high_level import extract_text

    return extract_text(str(path)) or ""


def _docling(path: Path) -> str:
    # Imported by name because it is not a dependency — this script's whole
    # point is to measure candidates BEFORE one is adopted, so a static import
    # would make the file unimportable until the decision it informs is already
    # made.
    import importlib

    converter = importlib.import_module("docling.document_converter")
    return converter.DocumentConverter().convert(str(path)).document.export_to_markdown()


def _pymupdf4llm(path: Path) -> str:
    import importlib

    return importlib.import_module("pymupdf4llm").to_markdown(str(path))


def _parsers() -> list[Parser]:
    out: list[Parser] = []
    for name, fn, module, cost in (
        ("pypdf", _pypdf, "pypdf", "already a dependency"),
        ("pdfminer", _pdfminer, "pdfminer", "already a dependency"),
        (
            "docling",
            _docling,
            "docling",
            "MIT, in-process, pulls a model stack (~GB) and needs first-run downloads",
        ),
        (
            "pymupdf4llm",
            _pymupdf4llm,
            "pymupdf4llm",
            "AGPL unless licensed — a licence question before a technical one",
        ),
    ):
        try:
            __import__(module)
            out.append(Parser(name=name, available=True, run=fn, cost=cost))
        except Exception as exc:
            out.append(
                Parser(
                    name=name,
                    available=False,
                    why_not=f"{type(exc).__name__}",
                    cost=cost,
                )
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdfs", nargs="*", type=Path, help="PDF paths, or a directory")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    paths: list[Path] = []
    for p in args.pdfs:
        paths.extend(sorted(p.rglob("*.pdf")) if p.is_dir() else [p])
    paths = paths[: args.limit]
    if not paths:
        print("no PDFs given. Pass files or a directory.")
        return 2

    parsers = _parsers()
    print(
        f"comparing {len([p for p in parsers if p.available])} installed parser(s) "
        f"over {len(paths)} PDF(s)\n"
    )

    for parser in parsers:
        if not parser.available:
            continue
        for path in paths:
            try:
                parser._m.append(_measure(parser.name, parser.run(path)))
            except Exception as exc:
                parser._m.append(Measurement(parser=parser.name, failed=f"{type(exc).__name__}"))

    print(
        f"{'parser':<14}{'ok':>5}{'chars':>10}{'headings':>10}"
        f"{'md head':>9}{'table rows':>12}{'figures':>9}"
    )
    print("-" * 69)
    for parser in parsers:
        if not parser.available:
            print(f"{parser.name:<14}{'—':>5}  NOT INSTALLED ({parser.why_not})")
            continue
        ok = [m for m in parser._m if m.failed is None]
        if not ok:
            print(f"{parser.name:<14}{0:>5}  every file failed")
            continue
        med = statistics.median
        print(
            f"{parser.name:<14}{len(ok):>5}"
            f"{int(med([m.chars for m in ok])):>10}"
            f"{int(med([m.headings for m in ok])):>10}"
            f"{int(med([m.md_headings for m in ok])):>9}"
            f"{int(med([m.table_rows for m in ok])):>12}"
            f"{int(med([m.figures for m in ok])):>9}"
        )
    print("\nmedians, not means: one 300-page PDF should not decide this.\n")

    print("what each would cost:")
    for parser in parsers:
        mark = " " if parser.available else "*"
        print(f" {mark}{parser.name:<13}{parser.cost}")
    if any(not p.available for p in parsers):
        print("\n* not installed. This script does not install anything — adding a PDF")
        print("  stack is a real cost to whoever runs Aleph, and the point of the")
        print("  table above is to make that decision on evidence.")

    # The number that matters for RS11 criterion 1: a parser emitting no
    # headings leaves every chunk with `section_path = NULL`, whatever else it
    # does well.
    print("\n`md head` is the criterion, not `headings`. `chunk_markdown` finds a")
    print("section by looking for `#`, so only markdown headings set `section_path`.")
    print("A parser with a median of 0 there leaves `section_path IS NULL` at 1.00")
    print("however much structure it appears to have seen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
