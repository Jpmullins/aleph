"""Confidence-state machine for hypotheses.

States:
  under_investigation → weakly_supported → well_supported
                     \\→ contested ↘
                                   refuted | abandoned

Transitions are driven by aggregated `HypothesisEvidence`:
  net_support = sum(weight * sign(stance)) where stance ∈ {supports: +1,
                                                            contradicts: -1,
                                                            contextualizes: 0}

Thresholds (Inc 5 defaults; spec §5.5):
  net_support ≥  3 and any high-weight supporting evidence  → well_supported
  net_support ≥  1                                          → weakly_supported
  net_support ≤ -1                                          → contested
  net_support ≤ -3                                          → refuted
  zero evidence                                             → under_investigation
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Confidence(StrEnum):
    UNDER_INVESTIGATION = "under_investigation"
    WEAKLY_SUPPORTED = "weakly_supported"
    WELL_SUPPORTED = "well_supported"
    CONTESTED = "contested"
    REFUTED = "refuted"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class EvidenceRow:
    stance: str  # "supports" | "contradicts" | "contextualizes"
    weight: float


def next_confidence_from_evidence(
    evidence: Iterable[EvidenceRow],
) -> Confidence:
    pos = 0.0
    neg = 0.0
    max_pos = 0.0
    for e in evidence:
        if e.stance == "supports":
            pos += e.weight
            max_pos = max(max_pos, e.weight)
        elif e.stance == "contradicts":
            neg += e.weight
    net = pos - neg
    if not pos and not neg:
        return Confidence.UNDER_INVESTIGATION
    if net <= -3:
        return Confidence.REFUTED
    if net <= -1:
        return Confidence.CONTESTED
    if net >= 3 and max_pos >= 1.5:
        return Confidence.WELL_SUPPORTED
    if net >= 1:
        return Confidence.WEAKLY_SUPPORTED
    return Confidence.UNDER_INVESTIGATION
