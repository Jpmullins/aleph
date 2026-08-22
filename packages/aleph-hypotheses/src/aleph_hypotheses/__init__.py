"""Aleph hypotheses — first-class structured analyst questions with evidence."""

from aleph_hypotheses.confidence import (
    TIER_WEIGHTS,
    Confidence,
    EvidenceRow,
    next_confidence_from_evidence,
    weight_for_tier,
)
from aleph_hypotheses.hypothesis_service import (
    add_evidence,
    create_hypothesis,
    get_hypothesis,
    list_hypotheses,
    record_version,
)
from aleph_hypotheses.models import (
    Hypothesis,
    HypothesisEvidence,
    HypothesisVersion,
)

__all__ = [
    "TIER_WEIGHTS",
    "Confidence",
    "EvidenceRow",
    "Hypothesis",
    "HypothesisEvidence",
    "HypothesisVersion",
    "add_evidence",
    "create_hypothesis",
    "get_hypothesis",
    "list_hypotheses",
    "next_confidence_from_evidence",
    "record_version",
    "weight_for_tier",
]
