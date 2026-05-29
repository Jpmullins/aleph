"""Per-MIME normalization pipeline.

Each Normalizer turns raw bytes into a canonicalized Markdown string plus
a structure outline and quality_flags list. The `Normalizer` protocol is
a thin facade; per-MIME implementations live in this module.

Inc 1 supports:
  application/pdf                                    → pypdf (primary), pdfminer.six (fallback)
  application/vnd.openxmlformats-...wordprocessingml → python-docx
  text/html                                          → readability-lxml + bs4
  text/markdown, text/plain                          → passthrough
  application/epub+zip                               → ebooklib

Failures are explicit (raise `NormalizationFailed`). The worker maps the
exception into `Source.status="failed"` with a clear `failure_reason`.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Protocol

import tiktoken


class NormalizationFailed(Exception):
    """Normalizer hard-failure. The worker records this as the failure_reason."""


@dataclass
class NormalizationResult:
    markdown: str
    parser: str
    parser_version: str
    structure: dict
    quality_flags: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.markdown)

    @property
    def token_count(self) -> int:
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(self.markdown, disallowed_special=()))


class Normalizer(Protocol):
    parser: str
    parser_version: str

    def normalize(self, data: bytes) -> NormalizationResult: ...


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


class PyPDFNormalizer:
    parser = "pypdf"

    def __init__(self) -> None:
        import pypdf  # local import keeps the package optional at import time

        self.parser_version = f"pypdf@{pypdf.__version__}"
        self._pypdf = pypdf

    def normalize(self, data: bytes) -> NormalizationResult:
        try:
            reader = self._pypdf.PdfReader(io.BytesIO(data))
        except Exception as exc:
            msg = f"pypdf failed to open document: {exc}"
            raise NormalizationFailed(msg) from exc

        pages_text: list[str] = []
        flags: list[str] = []
        total_chars = 0
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            pages_text.append(text)
            total_chars += len(text)

        if total_chars == 0 or (
            len(reader.pages) > 0 and total_chars / max(len(reader.pages), 1) < 100
        ):
            flags.append("ocr-required")

        body = "\n\n".join(p.strip() for p in pages_text if p.strip())
        structure = {
            "page_count": len(reader.pages),
            "heading_count": 0,  # pypdf doesn't expose this reliably
            "table_count": 0,
            "figure_count": 0,
        }
        return NormalizationResult(
            markdown=_canonicalize_text(body),
            parser=self.parser,
            parser_version=self.parser_version,
            structure=structure,
            quality_flags=flags,
        )


class PDFMinerNormalizer:
    parser = "pdfminer"

    def __init__(self) -> None:
        import pdfminer  # noqa: F401
        from pdfminer import __version__ as v

        self.parser_version = f"pdfminer.six@{v}"

    def normalize(self, data: bytes) -> NormalizationResult:
        try:
            from pdfminer.high_level import extract_text

            text = extract_text(io.BytesIO(data)) or ""
        except Exception as exc:
            msg = f"pdfminer failed: {exc}"
            raise NormalizationFailed(msg) from exc

        flags: list[str] = []
        if len(text.strip()) == 0:
            flags.append("ocr-required")
        return NormalizationResult(
            markdown=_canonicalize_text(text),
            parser=self.parser,
            parser_version=self.parser_version,
            structure={
                "page_count": text.count("\f") + 1,
                "heading_count": 0,
                "table_count": 0,
                "figure_count": 0,
            },
            quality_flags=flags,
        )


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


class DocxNormalizer:
    parser = "python-docx"

    def __init__(self) -> None:
        import docx  # python-docx

        self.parser_version = f"python-docx@{getattr(docx, '__version__', 'unknown')}"
        self._docx = docx

    def normalize(self, data: bytes) -> NormalizationResult:
        try:
            doc = self._docx.Document(io.BytesIO(data))
        except Exception as exc:
            msg = f"python-docx failed: {exc}"
            raise NormalizationFailed(msg) from exc

        lines: list[str] = []
        headings = 0
        for para in doc.paragraphs:
            style = (para.style.name or "").lower() if para.style else ""
            text = (para.text or "").strip()
            if not text:
                lines.append("")
                continue
            if style.startswith("heading"):
                lvl_str = style.replace("heading", "").strip() or "1"
                try:
                    lvl = max(1, min(6, int(lvl_str)))
                except ValueError:
                    lvl = 1
                lines.append(f"{'#' * lvl} {text}")
                headings += 1
            else:
                lines.append(text)

        tables = 0
        for tbl in doc.tables:
            tables += 1
            rows = []
            for row in tbl.rows:
                cells = [cell.text.replace("\n", " ").strip() for cell in row.cells]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                header = rows[0]
                sep = "|" + "|".join(["---"] * (header.count("|") - 1)) + "|"
                lines.append("")
                lines.append(header)
                lines.append(sep)
                lines.extend(rows[1:])
                lines.append("")

        return NormalizationResult(
            markdown=_canonicalize_text("\n".join(lines)),
            parser=self.parser,
            parser_version=self.parser_version,
            structure={
                "page_count": 0,
                "heading_count": headings,
                "table_count": tables,
                "figure_count": 0,
            },
            quality_flags=[],
        )


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


class HtmlNormalizer:
    parser = "readability-lxml+bs4"

    def __init__(self) -> None:
        import readability

        self.parser_version = f"readability-lxml@{getattr(readability, '__version__', 'unknown')}"

    def normalize(self, data: bytes) -> NormalizationResult:
        from bs4 import BeautifulSoup
        from readability import Document

        try:
            html_str = data.decode("utf-8", errors="replace")
            doc = Document(html_str)
            cleaned = doc.summary(html_partial=True)
            title = (doc.short_title() or "").strip()
        except Exception as exc:
            msg = f"readability-lxml failed: {exc}"
            raise NormalizationFailed(msg) from exc

        soup = BeautifulSoup(cleaned, "lxml")
        lines: list[str] = []
        if title:
            lines.append(f"# {title}")
            lines.append("")
        headings = 0
        for el in soup.descendants:
            if not hasattr(el, "name") or el.name is None:
                continue
            name = el.name
            if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                lvl = int(name[1])
                lines.append(f"{'#' * lvl} {el.get_text(' ', strip=True)}")
                headings += 1
            elif name == "p":
                lines.append(el.get_text(" ", strip=True))
                lines.append("")
            elif name in {"ul", "ol"}:
                for i, li in enumerate(el.find_all("li", recursive=False)):
                    prefix = "- " if name == "ul" else f"{i + 1}. "
                    lines.append(f"{prefix}{li.get_text(' ', strip=True)}")
                lines.append("")
            elif name == "pre":
                lines.append("```")
                lines.append(el.get_text())
                lines.append("```")
                lines.append("")

        return NormalizationResult(
            markdown=_canonicalize_text("\n".join(lines)),
            parser=self.parser,
            parser_version=self.parser_version,
            structure={
                "page_count": 0,
                "heading_count": headings,
                "table_count": 0,
                "figure_count": 0,
            },
            quality_flags=[],
        )


# ---------------------------------------------------------------------------
# Plain markdown / text
# ---------------------------------------------------------------------------


class PassthroughNormalizer:
    parser = "passthrough"
    parser_version = "passthrough@1.0"

    def __init__(self, *, parser_label: str = "passthrough") -> None:
        self.parser = parser_label

    def normalize(self, data: bytes) -> NormalizationResult:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        headings = sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))
        return NormalizationResult(
            markdown=_canonicalize_text(text),
            parser=self.parser,
            parser_version=self.parser_version,
            structure={
                "page_count": 0,
                "heading_count": headings,
                "table_count": 0,
                "figure_count": 0,
            },
            quality_flags=[],
        )


# ---------------------------------------------------------------------------
# EPUB
# ---------------------------------------------------------------------------


class EpubNormalizer:
    parser = "ebooklib"

    def __init__(self) -> None:
        import ebooklib  # noqa: F401
        from ebooklib import __version__ as v

        self.parser_version = f"ebooklib@{v}"

    def normalize(self, data: bytes) -> NormalizationResult:
        import tempfile

        from bs4 import BeautifulSoup
        from ebooklib import ITEM_DOCUMENT, epub

        with tempfile.NamedTemporaryFile(suffix=".epub", delete=True) as f:
            f.write(data)
            f.flush()
            try:
                book = epub.read_epub(f.name)
            except Exception as exc:
                msg = f"ebooklib failed: {exc}"
                raise NormalizationFailed(msg) from exc

        lines: list[str] = []
        title = book.get_metadata("DC", "title")
        if title:
            lines.append(f"# {title[0][0]}")
            lines.append("")

        headings = 0
        for item in book.get_items_of_type(ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_body_content(), "lxml")
            for el in soup.descendants:
                if not hasattr(el, "name") or el.name is None:
                    continue
                if el.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    lvl = int(el.name[1])
                    lines.append(f"{'#' * lvl} {el.get_text(' ', strip=True)}")
                    headings += 1
                elif el.name == "p":
                    lines.append(el.get_text(" ", strip=True))
                    lines.append("")

        return NormalizationResult(
            markdown=_canonicalize_text("\n".join(lines)),
            parser=self.parser,
            parser_version=self.parser_version,
            structure={
                "page_count": 0,
                "heading_count": headings,
                "table_count": 0,
                "figure_count": 0,
            },
            quality_flags=[],
        )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def normalizer_for(mime_type: str) -> Normalizer:
    mt = mime_type.split(";", maxsplit=1)[0].strip().lower()
    if mt == "application/pdf":
        try:
            return PyPDFNormalizer()
        except NormalizationFailed:
            return PDFMinerNormalizer()
    if mt == ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
        return DocxNormalizer()
    if mt in {"text/html", "application/xhtml+xml"}:
        return HtmlNormalizer()
    if mt in {"text/markdown", "text/x-markdown"}:
        return PassthroughNormalizer(parser_label="markdown")
    if mt == "text/plain":
        return PassthroughNormalizer(parser_label="text")
    if mt == "application/epub+zip":
        return EpubNormalizer()
    msg = f"no normalizer for mime type: {mime_type}"
    raise NormalizationFailed(msg)


def normalize_bytes(data: bytes, mime_type: str) -> NormalizationResult:
    primary = normalizer_for(mime_type)
    try:
        return primary.normalize(data)
    except NormalizationFailed:
        if mime_type.split(";", maxsplit=1)[0].strip().lower() == "application/pdf":
            return PDFMinerNormalizer().normalize(data)
        raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_WS_BLOCK = re.compile(r"\n{3,}")


def _canonicalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_BLOCK.sub("\n\n", text)
    return text.strip() + "\n" if text.strip() else ""
