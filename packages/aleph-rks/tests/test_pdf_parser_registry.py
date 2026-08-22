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


def test_falling_back_says_so_once_with_the_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment with no layout parser has to be able to LEARN that.

    Before this, the degradation was inferable and nothing else: `section_path`
    NULL and `heading_count` 0 are exactly what a PDF with no headings
    produces, so "we never installed docling" and "these papers have no
    headings" were the same two values. Measured on the live instance, 91.5% of
    pypdf chunks carry a NULL `section_path` and no line anywhere said the
    extra was missing from the worker image.

    Captured with `structlog.testing.capture_logs` rather than `caplog`:
    structlog is configured here with its own renderer and does not hand the
    event to stdlib logging, so a `caplog`-based assertion would read zero
    records while the line was being printed a foot away — a test that passes
    by not looking, which is the failure mode this whole file is about.
    """
    from structlog.testing import capture_logs

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
    # Per PROCESS, so the memo has to be cleared for the call to be observable
    # at all — and clearing it is also how the second half of this test can ask
    # whether the memo works.
    monkeypatch.setattr(normalization, "_pdf_parser_announced", set())

    with capture_logs() as logs:
        first = normalization.pdf_normalizer()
        second = normalization.pdf_normalizer()

    assert first.parser == second.parser != "docling"
    warnings = [entry for entry in logs if entry.get("event") == "rks.pdf_parser.degraded"]
    assert len(warnings) == 1, (
        "the fall-back is announced once per process, not once per document: a "
        f"worker ingesting 300 PDFs must not emit 300 of these (got {len(warnings)})"
    )
    said = warnings[0]
    assert said["log_level"] == "warning"
    assert any("docling" in s for s in said["skipped"]), (
        "the line has to name the parser that was skipped"
    )
    assert "--all-extras" in said["remedy"], "a warning with no remedy is a warning nobody acts on"
    assert "section_path" in said["impact"], (
        "and it has to say what the reader will see instead, or the two "
        "indistinguishable states stay indistinguishable"
    )


def test_the_healthy_case_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Landing on the FIRST candidate is not a degradation.

    Logging unconditionally would make the line mean "a PDF was parsed", and an
    operator would filter it — which is the same as not having it.
    """
    from structlog.testing import capture_logs

    from aleph_rks import normalization

    monkeypatch.setattr(normalization, "_pdf_parser_announced", set())
    with capture_logs() as logs:
        normalization.pdf_normalizer("pypdf")

    assert not [entry for entry in logs if entry.get("event") == "rks.pdf_parser.degraded"]
