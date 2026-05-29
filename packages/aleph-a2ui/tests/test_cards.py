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
    HypothesisCardProps,
    SourceCardProps,
    approval_card,
    claim_card,
    source_card,
)


def test_claim_card_shape_and_stringified_uuid() -> None:
    cid = uuid4()
    card = claim_card(ClaimCardProps(claim_id=cid, text="The sky is blue"))
    assert card["type"] == "ClaimCard"
    assert card["id"].startswith("ClaimCard-")  # auto-generated
    props = card["props"]
    assert props["claim_id"] == str(cid)  # UUID serialised to str
    assert props["text"] == "The sky is blue"
    assert props["confidence"] == "cited"  # default
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
