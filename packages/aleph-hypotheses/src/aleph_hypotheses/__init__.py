"""Aleph hypotheses — first-class structured analyst questions with evidence."""

from aleph_hypotheses.confidence import (
    Confidence,
    next_confidence_from_evidence,
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
    "Confidence",
    "Hypothesis",
    "HypothesisEvidence",
    "HypothesisVersion",
    "add_evidence",
    "create_hypothesis",
    "get_hypothesis",
    "list_hypotheses",
    "next_confidence_from_evidence",
    "record_version",
]
