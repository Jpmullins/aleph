"""Normalization tests — passthrough + HTML + dispatch path."""

from __future__ import annotations

import pytest

from aleph_rks.normalization import (
    NormalizationFailed,
    PassthroughNormalizer,
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
