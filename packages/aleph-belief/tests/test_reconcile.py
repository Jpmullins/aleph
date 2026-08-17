"""Deterministic reconciliation — identity decided without a model."""

from __future__ import annotations

from uuid import uuid4

from aleph_belief.patch import PatchOperation, validate_envelope
from aleph_belief.reconcile import (
    HIGH_CONFIDENCE,
    LOW_CONFIDENCE,
    ClaimRef,
    RejectReason,
    propose,
    score_pair,
)


def claim(text: str) -> ClaimRef:
    return ClaimRef(id=uuid4(), text=text)


# -- the disqualifiers, which overlap scoring gets most confidently wrong ----


def test_a_negation_is_decisive() -> None:
    """Identical vocabulary, opposite meaning. Token overlap calls these twins."""
    c = score_pair(
        claim("Sedimentation rates rose sharply after the event."),
        claim("Sedimentation rates did not rise sharply after the event."),
    )
    assert c.reason is RejectReason.NEGATION_MISMATCH
    assert c.verdict == "reject"


def test_a_quantity_mismatch_is_decisive() -> None:
    c = score_pair(
        claim("Rates rose sharply after 8.2 kiloyears before present."),
        claim("Rates rose sharply after 4.2 kiloyears before present."),
    )
    assert c.reason is RejectReason.QUANTITY_MISMATCH


def test_opposed_direction_is_decisive() -> None:
    c = score_pair(
        claim("Sedimentation rates increased through the interval."),
        claim("Sedimentation rates decreased through the interval."),
    )
    assert c.reason is RejectReason.OPPOSED_DIRECTION


# -- the ordinary cases ------------------------------------------------------


def test_a_restatement_at_different_length_is_a_match() -> None:
    """The common real case, and exactly what plain Jaccard underrates."""
    c = score_pair(
        claim("Rates rose after 8.2 ka."),
        claim("Sedimentation rates rose sharply after 8.2 ka in the basin."),
    )
    assert c.score >= HIGH_CONFIDENCE, c.score
    assert c.verdict == "accept"


def test_identical_text_matches() -> None:
    text = "Two dates were rejected as reworked."
    assert score_pair(claim(text), claim(text)).verdict == "accept"


def test_unrelated_claims_are_rejected_with_a_reason() -> None:
    c = score_pair(
        claim("Heavy minerals indicate a recycled orogenic source."),
        claim("Tidal range in the embayment reached 4.2 metres."),
    )
    assert c.verdict == "reject"
    assert c.reason in {RejectReason.DIFFERENT_SUBJECT, RejectReason.INSUFFICIENT_OVERLAP}


def test_a_reject_always_names_a_reason() -> None:
    """'Below 0.45' cannot be argued with; a named reason can."""
    pairs = [
        ("The core was 3.4 metres long.", "The classifier achieved kappa 0.81."),
        ("Rates rose.", "Rates did not rise."),
    ]
    for a, b in pairs:
        c = score_pair(claim(a), claim(b))
        assert c.verdict != "reject" or c.reason is not None


def test_the_middle_band_escalates_rather_than_guessing() -> None:
    """The whole LLM budget lives here, and only here."""
    c = score_pair(
        claim("Benthic foraminifera indicate a rapid deepening."),
        claim("Benthic assemblages indicate deepening of the shelf."),
    )
    if LOW_CONFIDENCE < c.score < HIGH_CONFIDENCE:
        assert c.verdict == "escalate"


def test_scoring_is_deterministic() -> None:
    a, b = claim("Rates rose after 8.2 ka."), claim("Rates rose sharply after 8.2 ka.")
    assert score_pair(a, b).score == score_pair(a, b).score


def test_empty_text_does_not_match_anything() -> None:
    c = score_pair(claim(""), claim("A real claim."))
    assert c.reason is RejectReason.INSUFFICIENT_OVERLAP


# -- patches -----------------------------------------------------------------


def test_every_decided_pair_yields_a_valid_patch() -> None:
    project = uuid4()
    results = propose(
        [
            claim("Rates rose after 8.2 ka."),
            claim("Sedimentation rates rose sharply after 8.2 ka."),
            claim("Tidal range reached 4.2 metres at spring tide."),
        ],
        project_id=project,
        profile_hash="sha256:profile",
        graph_hash="sha256:graph",
    )
    assert results
    for candidate, patch in results:
        if candidate.verdict == "escalate":
            assert patch is None
            continue
        assert patch is not None
        validation = validate_envelope(patch)
        assert validation.valid, validation.issues
        assert patch.operation in {PatchOperation.ACCEPT_MATCH, PatchOperation.REJECT_MATCH}


def test_an_accept_carries_evidence_and_a_reject_need_not() -> None:
    """An accept asserts two things are one; a reject judges the graph's proposal."""
    project = uuid4()
    results = propose(
        [
            claim("Rates rose after 8.2 ka."),
            claim("Sedimentation rates rose sharply after 8.2 ka."),
            claim("Tidal range reached 4.2 metres."),
        ],
        project_id=project,
        profile_hash="p",
        graph_hash="g",
    )
    for _, patch in results:
        if patch is None:
            continue
        if patch.operation is PatchOperation.ACCEPT_MATCH:
            assert patch.evidence_refs, "an accept must show what it matched on"
        assert patch.reason.strip()


def test_nothing_is_applied() -> None:
    """propose() proposes. Every patch comes back as a proposal."""
    from aleph_belief.patch import PatchStatus

    results = propose(
        [claim("A claim."), claim("A different claim entirely about tides.")],
        project_id=uuid4(),
        profile_hash="p",
        graph_hash="g",
    )
    for _, patch in results:
        if patch is not None:
            assert patch.status is PatchStatus.PROPOSED


def test_pair_order_is_stable_across_runs() -> None:
    claims = [claim("Alpha claim."), claim("Beta claim."), claim("Gamma claim.")]
    first = [
        (c.left.id, c.right.id)
        for c, _ in propose(claims, project_id=uuid4(), profile_hash="p", graph_hash="g")
    ]
    second = [
        (c.left.id, c.right.id)
        for c, _ in propose(claims, project_id=uuid4(), profile_hash="p", graph_hash="g")
    ]
    assert first == second


def test_no_llm_is_involved() -> None:
    """The module must not import a model client, now or later."""
    import inspect

    from aleph_belief import reconcile

    src = inspect.getsource(reconcile)
    for forbidden in ("litellm", "openai", "anthropic", "ChatOpenAI", "LiteLLMClient"):
        assert forbidden not in src, f"reconciliation reached for {forbidden}"
