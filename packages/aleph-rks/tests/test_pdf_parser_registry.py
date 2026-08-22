"""The PDF parser is a choice the registry resolves, not a hardcoded default.

WS-RS11 (b). `normalizer_for("application/pdf")` used to be

    try:
        return PyPDFNormalizer()
    except NormalizationFailed:
        return PDFMinerNormalizer()

— two flat parsers and no way to say otherwise. That is the plugin thesis
missing from ingest: the parser IS a plugin decision, and a deployment that has
not installed the layout stack should degrade to the flat parsers rather than
fail to ingest anything.
"""

from __future__ import annotations

import pytest

from aleph_rks.normalization import (
    PDF_PARSERS,
    NormalizationFailed,
    PyPDFNormalizer,
    normalizer_for,
    pdf_normalizer,
)


def test_the_preference_order_is_most_structure_first() -> None:
    """docling before the flat parsers, or installing it changes nothing."""
    assert [name for name, _ in PDF_PARSERS][:1] == ["docling"]
    assert {name for name, _ in PDF_PARSERS} >= {"docling", "pypdf", "pdfminer"}


def test_a_named_parser_is_pinned_and_never_falls_back() -> None:
    """Pinned means pinned.

    Falling back would make `ALEPH_PDF_PARSER` a suggestion — and a comparison
    run would then silently measure a different parser than the one it names,
    which is worse than refusing.
    """
    assert pdf_normalizer("pypdf").parser == "pypdf"
    assert isinstance(pdf_normalizer("pypdf"), PyPDFNormalizer)


def test_an_unknown_parser_name_is_refused_not_ignored() -> None:
    with pytest.raises(NormalizationFailed, match="unknown PDF parser"):
        pdf_normalizer("marker")


def test_the_env_var_selects_a_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEPH_PDF_PARSER", "pdfminer")
    assert pdf_normalizer().parser == "pdfminer"


def test_a_missing_layout_stack_degrades_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole reason docling is an EXTRA.

    An installation without it must keep ingesting PDFs — flat, and honestly
    labelled as flat — rather than failing at the first upload with an
    ImportError from a dependency it was never told it needed.
    """
    from aleph_rks import normalization

    class _Unavailable:
        def __init__(self) -> None:
            msg = "docling is not installed"
            raise NormalizationFailed(msg)

    monkeypatch.setattr(
        normalization,
        "PDF_PARSERS",
        (("docling", _Unavailable), *normalization.PDF_PARSERS[1:]),
    )
    resolved = normalizer_for("application/pdf")
    assert resolved.parser in {"pypdf", "pdfminer"}


def test_every_parser_in_the_table_is_a_normalizer() -> None:
    """A name in the preference list with no working class is a parser that
    can be selected and cannot run."""
    for name, cls in PDF_PARSERS:
        assert hasattr(cls, "normalize"), name
        assert hasattr(cls, "parser"), name
