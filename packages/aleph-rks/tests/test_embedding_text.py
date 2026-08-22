"""What actually gets embedded, and what a change to it costs. WS-RS6.

`embedding_text` is the one function the eval and the ingest path both call, so
the string production embeds and the string the measurement embeds cannot drift
— that reconciliation is WS-RS5's and it is pinned by
`packages/aleph-evals/tests/test_eval_matches_production.py`.

RS6 adds the contextual representation behind it: the chunk prefixed with its
document title and heading path, which is the standard remedy for a passage
that says "this doubled throughput" without ever naming the thing. It ships
**off**. The reason is not caution, it is arithmetic: ~10,000 chunks are already
embedded under the bare representation, and a half-migrated index ranks new
documents against old ones on an unequal footing while every component reports
success. Measured on this instance's own corpus it also did not help — see the
WS-RS6 report.

So the tests here are about the two things that make the switch survivable: the
representation is part of the recorded index identity (so a flip is *visible* as
staleness and the existing re-embed repairs it), and the prefix never reaches
`DocumentChunk.text` (so grounding offsets keep slicing the real document).
"""

from __future__ import annotations

import pytest

from aleph_rks import indexing
from aleph_rks.chunking import chunk_markdown
from aleph_rks.indexing import CONTEXTUAL_TAG, embedding_text, index_signature

MARKDOWN = """\
# Throughput

Baseline runs completed in 41 seconds.

## Batched writes

This halved the wall clock without changing the result.
"""


def test_the_shipped_default_embeds_the_chunk_and_nothing_else() -> None:
    """The state of the deployed index, restated as an assertion.

    If this flips without a re-embed, every document ingested afterwards is
    embedded from a different string than every document before it.
    """
    assert indexing.CONTEXTUAL_EMBEDDING is False
    assert embedding_text(chunk_text="a passage") == "a passage"
    assert embedding_text(chunk_text="a passage", title="A Title") == "a passage"
    assert (
        embedding_text(chunk_text="a passage", title="A Title", section_path="methods")
        == "a passage"
    )


def test_the_contextual_representation_leads_with_title_and_heading() -> None:
    out = embedding_text(
        chunk_text="This halved the wall clock.",
        title="Throughput of the batching change",
        section_path="throughput > batched-writes",
        contextual=True,
    )
    assert out.startswith("Throughput of the batching change\n")
    # Slugs are stored hyphenated and both retrieval legs tokenise hyphens
    # badly; the heading goes in as a phrase a person would write.
    assert "Throughput > Batched Writes" in out
    assert out.endswith("This halved the wall clock.")


def test_a_chunk_with_no_title_and_no_heading_is_returned_unchanged() -> None:
    """PDFs are the majority of what Aleph ingests and the shipped parsers
    extract almost no structure — 751 of 10,098 live chunks carry a
    `section_path`. A representation that prepends an empty line to nine out of
    ten chunks is a different corpus for no signal."""
    assert embedding_text(chunk_text="bare", contextual=True) == "bare"
    assert embedding_text(chunk_text="bare", title="", section_path="", contextual=True) == "bare"


def test_the_title_alone_is_enough_to_change_the_representation() -> None:
    assert embedding_text(chunk_text="bare", title="A Paper", contextual=True) == "A Paper\n\nbare"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("methods", "Methods"),
        ("methods > sample-preparation", "Methods > Sample Preparation"),
        ("a > b > c", "A > B > C"),
    ],
)
def test_the_heading_path_is_made_readable(path: str, expected: str) -> None:
    out = embedding_text(chunk_text="x", section_path=path, contextual=True)
    assert out.split("\n")[0] == expected


# --- the migration story ---------------------------------------------------


def test_the_representation_is_part_of_the_recorded_index_identity() -> None:
    """Without this, flipping the constant is invisible.

    `RetrievalIndexRecord.embedder_model` held the model name alone, so two
    chunks embedded from *different strings* by the same model were
    indistinguishable and `reembed_for_project`'s staleness test
    (`embedder_model != current`) could never fire.
    """
    assert index_signature("titan-embed-text-v2", contextual=False) == "titan-embed-text-v2"
    assert (
        index_signature("titan-embed-text-v2", contextual=True)
        == f"titan-embed-text-v2+{CONTEXTUAL_TAG}"
    )
    assert index_signature("titan-embed-text-v2", contextual=True) != index_signature(
        "titan-embed-text-v2", contextual=False
    )


def test_the_signature_follows_the_module_default_when_not_told_otherwise() -> None:
    assert index_signature("m") == index_signature("m", contextual=indexing.CONTEXTUAL_EMBEDDING)


def test_flipping_the_constant_makes_every_existing_index_read_as_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property that turns a representation change into a repairable one.

    `reembed_for_project` selects `embedder_model != current_model`. Recorded
    under the old representation and compared under the new one, that predicate
    is true for every source — which is what makes the existing worker the
    migration, with no new column and no new job.
    """
    recorded = index_signature("titan-embed-text-v2")
    monkeypatch.setattr(indexing, "CONTEXTUAL_EMBEDDING", True)
    assert index_signature("titan-embed-text-v2") != recorded


# --- grounding spans -------------------------------------------------------


def test_the_prefix_never_reaches_the_stored_chunk_text() -> None:
    """WS-RS6 criterion: grounding spans survive contextual embedding.

    `char_start`/`char_end` index the source document, and `DocumentChunk.text`
    is asserted equal to `markdown[char_start:char_end]` by
    `test_chunk_offsets.py`. Embedding a *different* string is fine; storing
    one would put every offset out by the length of the prefix, and a grounding
    view would then highlight confidently and wrongly.
    """
    chunks = chunk_markdown(MARKDOWN)
    assert chunks
    for chunk in chunks:
        contextual = embedding_text(
            chunk_text=chunk.text,
            title="Throughput of the batching change",
            section_path=chunk.section_path,
            contextual=True,
        )
        assert contextual.endswith(chunk.text)
        assert contextual != chunk.text, (
            "this document has headings, so every chunk must gain a prefix — "
            "otherwise the test is not exercising the contextual path"
        )
        # The invariant `test_chunk_offsets.py` pins, restated against the
        # representation that could break it.
        assert MARKDOWN[chunk.char_start : chunk.char_end] == chunk.text
