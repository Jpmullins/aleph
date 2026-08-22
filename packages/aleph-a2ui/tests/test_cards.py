"""Unit tests for the inline-card builders (`aleph_a2ui.components.cards`).

These are pure dict builders the agent surface emits as chat cards. The tests
pin the catalog component `type`, the inline prop shape (UUIDs stringified,
action props present), and the `id` behaviour (explicit vs. auto-generated).
No DB, no I/O — run under `pytest -m "not integration"`.
"""

from __future__ import annotations

from uuid import uuid4

from aleph_a2ui.components.cards import (
    ApprovalCardProps,
    ClaimCardProps,
    DiffCardProps,
    HypothesisCardProps,
    SourceCardProps,
    TableCardProps,
    approval_card,
    claim_card,
    diff_card,
    source_card,
    table_card,
)
from aleph_core.confidence import CONFIDENCE_VALUES, Confidence


def test_claim_card_shape_and_stringified_uuid() -> None:
    cid = uuid4()
    card = claim_card(ClaimCardProps(claim_id=cid, text="The sky is blue"))
    assert card["type"] == "ClaimCard"
    assert card["id"].startswith("ClaimCard-")  # auto-generated
    props = card["props"]
    assert props["claim_id"] == str(cid)  # UUID serialised to str
    assert props["text"] == "The sky is blue"
    # WS-RS9: the default was "cited", which the catalog permitted and nothing
    # else in the tree recognised. A card built with no confidence stated has
    # had none derived.
    assert props["confidence"] == Confidence.UNDER_INVESTIGATION.value
    assert props["confidence"] in CONFIDENCE_VALUES
    assert props["citations"] == []
    assert props["open_action"] == "open"


def test_source_card_explicit_id_is_respected() -> None:
    card = source_card(
        SourceCardProps(source_id=uuid4(), short_id="S3", title="Doc", url="http://x"),
        card_id="my-card",
    )
    assert card["type"] == "SourceCard"
    assert card["id"] == "my-card"
    assert card["props"]["short_id"] == "S3"
    assert card["props"]["url"] == "http://x"
    assert card["props"]["navigate_wiki_action"] == "navigate_wiki"


def test_approval_card_carries_target_and_severity() -> None:
    target = uuid4()
    card = approval_card(
        ApprovalCardProps(
            target_id=target,
            target_kind="artifact",
            title="Build report?",
            summary="This will render a PDF.",
            severity="warn",
        )
    )
    assert card["type"] == "ApprovalCard"
    props = card["props"]
    assert props["target_id"] == str(target)
    assert props["target_kind"] == "artifact"
    assert props["severity"] == "warn"
    assert props["diff_card_id"] is None


def test_auto_ids_are_unique_per_call() -> None:
    a = claim_card(ClaimCardProps(claim_id=uuid4(), text="a"))
    b = claim_card(ClaimCardProps(claim_id=uuid4(), text="b"))
    assert a["id"] != b["id"]


def test_hypothesis_props_dataclass_defaults() -> None:
    p = HypothesisCardProps(hypothesis_id=uuid4(), title="H")
    assert p.confidence == "initial"
    assert p.evidence_count == 0


def test_source_card_carries_bound_normalized_preview() -> None:
    # WP-4e: the Library builder supplies the preview as a BOUND prop; the card
    # renders it in place (no self-fetch of /sources/*/normalized).
    card = source_card(
        SourceCardProps(
            source_id=uuid4(),
            short_id="S9",
            title="Doc",
            normalized_preview="# Heading\n\nBody text.",
        )
    )
    assert card["type"] == "SourceCard"
    assert card["props"]["normalized_preview"] == "# Heading\n\nBody text."


def test_source_card_preview_defaults_none() -> None:
    card = source_card(SourceCardProps(source_id=uuid4(), short_id="S1", title="Doc"))
    assert card["props"]["normalized_preview"] is None


def test_table_card_carries_bound_rows_and_columns() -> None:
    # WP-4e: rows/columns are bound props supplied by the producer; the card
    # never self-fetches dataset rows.
    card = table_card(
        TableCardProps(
            dataset_version_id=None,
            title="Benchmarks",
            columns=[{"name": "model", "label": "Model"}],
            rows=[{"model": "PaLM"}],
        )
    )
    assert card["type"] == "TableCard"
    assert card["props"]["rows"] == [{"model": "PaLM"}]
    assert card["props"]["columns"] == [{"name": "model", "label": "Model"}]


def test_diff_card_carries_bound_revision_bodies() -> None:
    # WP-4e: DiffCard renders a real diff from bound bodies (no self-fetch).
    p = DiffCardProps(
        from_revision_id=uuid4(),
        to_revision_id=uuid4(),
        page_id=uuid4(),
        from_body_md="line a\nline b",
        to_body_md="line a\nline c",
    )
    card = diff_card(p)
    assert card["type"] == "DiffCard"
    assert card["props"]["from_body_md"] == "line a\nline b"
    assert card["props"]["to_body_md"] == "line a\nline c"


def test_diff_card_bodies_default_none() -> None:
    card = diff_card(
        DiffCardProps(from_revision_id=uuid4(), to_revision_id=uuid4(), page_id=uuid4())
    )
    assert card["props"]["from_body_md"] is None
    assert card["props"]["to_body_md"] is None
