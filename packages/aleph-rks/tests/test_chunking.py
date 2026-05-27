"""Chunking determinism and overlap correctness tests."""

from __future__ import annotations

from aleph_rks.chunking import chunk_markdown


SAMPLE = """# Methods

## Sample Selection

We selected 47 participants from the registry. Inclusion criteria
included age 18 to 65, no prior diagnosis of X, and willingness to
travel to the study site. Three were later excluded due to scheduling
conflicts. The final cohort had 44 participants.

## Measurement

Participants completed the protocol over two visits separated by 7 to
10 days. We recorded blood pressure, heart rate, and cortisol levels.

# Results

## Primary outcome

The primary outcome was reduced by 18% in the treatment arm. The
control arm showed no significant change. We replicate the finding
across two subgroups.
"""


def test_chunk_count_reasonable() -> None:
    out = chunk_markdown(SAMPLE, target_tokens=64, overlap_tokens=12)
    assert len(out) >= 2
    for c in out:
        assert c.text.strip()
        assert c.token_count > 0


def test_chunk_preserves_section_paths() -> None:
    out = chunk_markdown(SAMPLE, target_tokens=80, overlap_tokens=12)
    paths = {c.section_path for c in out}
    # We expect at least sample-selection and primary-outcome paths.
    assert any("sample-selection" in (p or "") for p in paths)
    assert any("primary-outcome" in (p or "") for p in paths)


def test_chunk_is_deterministic() -> None:
    a = chunk_markdown(SAMPLE, target_tokens=64, overlap_tokens=12)
    b = chunk_markdown(SAMPLE, target_tokens=64, overlap_tokens=12)
    assert [c.text for c in a] == [c.text for c in b]
    assert [c.section_path for c in a] == [c.section_path for c in b]


def test_empty_markdown_returns_empty() -> None:
    assert chunk_markdown("", target_tokens=64) == []
    assert chunk_markdown("   \n\n  \n", target_tokens=64) == []


def test_single_short_paragraph_one_chunk() -> None:
    md = "# T\n\nOne short sentence."
    out = chunk_markdown(md, target_tokens=128)
    assert len(out) == 1
    assert "One short sentence." in out[0].text
