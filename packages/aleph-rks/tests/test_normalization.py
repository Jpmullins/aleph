"""Normalization tests — passthrough + HTML + dispatch path."""

from __future__ import annotations

import pytest

from aleph_rks.normalization import (
    OCR_REQUIRED,
    NormalizationFailed,
    PassthroughNormalizer,
    PyPDFNormalizer,
    normalize_bytes,
    normalizer_for,
)


def test_passthrough_markdown() -> None:
    md = b"# Title\n\nSome paragraph.\n"
    out = PassthroughNormalizer(parser_label="markdown").normalize(md)
    assert out.parser == "markdown"
    assert "Title" in out.markdown
    assert out.char_count > 0


def test_dispatch_picks_pdfminer_when_pypdf_unavailable_for_garbage() -> None:
    with pytest.raises(NormalizationFailed):
        # Random garbage bytes can't be parsed as PDF; ensure we surface a
        # NormalizationFailed rather than a silent empty result.
        normalize_bytes(b"\x00\x01garbage-not-pdf\x00", "application/pdf")


def test_normalizer_for_unknown_mime_raises() -> None:
    with pytest.raises(NormalizationFailed):
        normalizer_for("application/x-totally-fake")


def test_passthrough_text() -> None:
    txt = b"Just plain text with no markdown."
    out = PassthroughNormalizer(parser_label="text").normalize(txt)
    assert out.markdown.startswith("Just plain text")
    assert out.token_count > 0


# --- ocr-required: the flag that had three producers and no reader ---------


def _pdf_with_no_text_layer(pages: int = 3) -> bytes:
    """A real PDF with real pages and no extractable characters — a scan, as
    far as any text-layer parser can tell. Built rather than committed as a
    fixture so the bytes cannot drift from the pypdf version reading them."""
    import io

    import pypdf

    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_a_pdf_with_no_text_layer_raises_the_ocr_flag() -> None:
    """The producer half of the link, driven through the real normalizer.

    `aleph_rks.indexing` branches on this exact string. Both sides now spell it
    with one constant — before, the producers wrote a literal and nobody read
    it at all, so a typo would have been undetectable in either direction.
    """
    result = PyPDFNormalizer().normalize(_pdf_with_no_text_layer())
    assert OCR_REQUIRED in result.quality_flags
    assert result.markdown.strip() == ""


def test_a_pdf_with_a_text_layer_does_not_raise_the_ocr_flag() -> None:
    """The other half. A flag that is always set says nothing."""
    body = (
        "This page carries a real text layer with plenty of characters on it, over and over. "
    ) * 20
    result = normalize_bytes(body.encode(), "text/plain")
    assert OCR_REQUIRED not in result.quality_flags
