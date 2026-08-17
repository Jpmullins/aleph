"""Deterministic reconciliation — finding duplicate beliefs without a model.

The curator this replaces spent one judge-tier LLM call per new page deciding
whether two claims were the same thing. That is expensive, non-reproducible, and
untestable: the same two claims could be merged on Monday and not on Tuesday,
and nothing in the system could tell you why.

Identity is mostly a lexical question and lexical questions have answers. This
module scores a pair deterministically and puts the result in one of three
bands:

    score >= HIGH   auto-accept        no LLM
    score <= LOW    auto-reject        no LLM, with a named reason
    in between      escalate           the entire LLM budget lives here

That inverts the old economics from O(claims) model calls to O(genuinely
ambiguous pairs), and it makes the deterministic majority reproducible and
unit-testable. The model stops being the matcher and becomes the adjudicator.

Every outcome is a :class:`~aleph_belief.patch.BeliefPatch` — proposed, not
applied. Nothing here mutates anything.

The scoring shape (shared normalised terms, exact-form match, named reject
reasons) is taken from graphify's `entity-linking` / `differentEntityReason`
(MIT, Copyright (c) 2026 Safi Shamsi). See the package NOTICE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from aleph_belief.patch import BeliefPatch, PatchOperation
from aleph_belief.trust import TrustTier

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "HIGH_CONFIDENCE",
    "LOW_CONFIDENCE",
    "Candidate",
    "ClaimRef",
    "RejectReason",
    "propose",
    "score_pair",
]

#: At or above: the pair is the same belief. Below LOW: it is not. Between: ask.
HIGH_CONFIDENCE = 0.86
LOW_CONFIDENCE = 0.45

_WORD = re.compile(r"[a-z0-9]+")

#: Words that carry no identity signal. Deliberately short — an aggressive stop
#: list makes unrelated claims look similar, which is the expensive error here.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "from",
        "by",
        "with",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "as",
        "into",
        "than",
        "then",
    ]
)

#: Tokens whose DISAGREEMENT is decisive regardless of overall overlap. Two
#: claims identical except for a negation, a quantity or a direction are not the
#: same claim, and token overlap alone scores them as near-identical.
_NEGATIONS = frozenset({"no", "not", "never", "without", "absent", "fails", "failed", "cannot"})
_DIRECTIONS = frozenset(
    {
        "rose",
        "rise",
        "rises",
        "increase",
        "increased",
        "increases",
        "higher",
        "above",
        "fell",
        "fall",
        "falls",
        "decrease",
        "decreased",
        "decreases",
        "lower",
        "below",
    }
)
_OPPOSED = (
    {"rose", "rise", "rises", "increase", "increased", "increases", "higher", "above"},
    {"fell", "fall", "falls", "decrease", "decreased", "decreases", "lower", "below"},
)


class RejectReason(StrEnum):
    """Why a pair was refused. A named reason, never a bare threshold.

    "Below 0.45" tells a reviewer nothing and cannot be argued with. These can.
    """

    NEGATION_MISMATCH = "negation_mismatch"
    QUANTITY_MISMATCH = "quantity_mismatch"
    OPPOSED_DIRECTION = "opposed_direction"
    INSUFFICIENT_OVERLAP = "insufficient_overlap"
    DIFFERENT_SUBJECT = "different_subject"


@dataclass(frozen=True)
class ClaimRef:
    """The minimum needed to reconcile. Deliberately not the ORM row."""

    id: UUID
    text: str
    origin: str = "agent"


@dataclass(frozen=True)
class Candidate:
    left: ClaimRef
    right: ClaimRef
    score: float
    reason: RejectReason | None
    #: Terms both carry — what a reviewer needs to see to judge quickly.
    shared: tuple[str, ...]
    #: Terms only one carries, which is usually where the disagreement is.
    distinct: tuple[str, ...]

    @property
    def verdict(self) -> str:
        if self.reason is not None or self.score <= LOW_CONFIDENCE:
            return "reject"
        if self.score >= HIGH_CONFIDENCE:
            return "accept"
        return "escalate"


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS]


def _numbers(tokens: Iterable[str]) -> set[str]:
    return {t for t in tokens if any(c.isdigit() for c in t)}


def score_pair(left: ClaimRef, right: ClaimRef) -> Candidate:
    """Score two claims for identity. Pure; no I/O, no model, no randomness.

    The score is token-set Jaccard, lifted when one claim's terms are a subset
    of the other's — "rates rose after 8.2 ka" and "sedimentation rates rose
    sharply after 8.2 ka" are the same belief stated at different length, and
    plain Jaccard punishes that.

    Disqualifiers run first and are absolute. A negation, a quantity or a
    direction that disagrees means the claims are not the same claim no matter
    how much vocabulary they share — which is exactly the case where an
    overlap score is most confidently wrong.
    """
    lt, rt = _tokens(left.text), _tokens(right.text)
    ls, rs = set(lt), set(rt)
    shared = tuple(sorted(ls & rs))
    distinct = tuple(sorted(ls ^ rs))

    if not ls or not rs:
        return Candidate(left, right, 0.0, RejectReason.INSUFFICIENT_OVERLAP, shared, distinct)

    # Disqualifiers, in order of how badly an overlap score misjudges them.
    if bool(ls & _NEGATIONS) != bool(rs & _NEGATIONS):
        return Candidate(left, right, 0.0, RejectReason.NEGATION_MISMATCH, shared, distinct)

    ln, rn = _numbers(lt), _numbers(rt)
    if ln and rn and ln != rn:
        return Candidate(left, right, 0.0, RejectReason.QUANTITY_MISMATCH, shared, distinct)

    up, down = _OPPOSED
    if (ls & up and rs & down) or (ls & down and rs & up):
        return Candidate(left, right, 0.0, RejectReason.OPPOSED_DIRECTION, shared, distinct)

    jaccard = len(ls & rs) / len(ls | rs)
    containment = len(ls & rs) / min(len(ls), len(rs))
    # Containment carries most of the weight: restatement at different length is
    # the common real case, and it is exactly what Jaccard underrates.
    score = 0.35 * jaccard + 0.65 * containment

    reason: RejectReason | None = None
    if score <= LOW_CONFIDENCE:
        reason = (
            RejectReason.DIFFERENT_SUBJECT
            if containment < 0.34
            else RejectReason.INSUFFICIENT_OVERLAP
        )
    return Candidate(left, right, round(score, 4), reason, shared, distinct)


def propose(
    claims: Sequence[ClaimRef],
    *,
    project_id: UUID,
    profile_hash: str,
    graph_hash: str,
    author: str = "reconciler:deterministic",
    now: datetime | None = None,
) -> list[tuple[Candidate, BeliefPatch | None]]:
    """Score every pair and emit a patch for each decided outcome.

    Returns ``(candidate, patch)``. The patch is ``None`` for an escalation —
    that pair is what the LLM adjudicator is handed, and it will emit its own
    patch. Nothing is applied here.

    Pairs are ordered deterministically so two runs over the same claims produce
    the same list, which is the property the LLM curator could not offer.
    """
    stamped = now or datetime.now(UTC)
    ordered = sorted(claims, key=lambda c: str(c.id))
    out: list[tuple[Candidate, BeliefPatch | None]] = []

    for i, left in enumerate(ordered):
        for right in ordered[i + 1 :]:
            candidate = score_pair(left, right)
            verdict = candidate.verdict
            if verdict == "escalate":
                out.append((candidate, None))
                continue

            accepted = verdict == "accept"
            patch = BeliefPatch(
                id=uuid4(),
                project_id=project_id,
                operation=(
                    PatchOperation.ACCEPT_MATCH if accepted else PatchOperation.REJECT_MATCH
                ),
                profile_hash=profile_hash,
                graph_hash=graph_hash,
                target={
                    "source_claim_id": str(candidate.right.id),
                    "target_claim_id": str(candidate.left.id),
                    "score": candidate.score,
                    "shared_terms": list(candidate.shared),
                },
                # An accept asserts two claims are one thing, so it must carry
                # the evidence for that: the shared terms it matched on.
                # A reject is a judgment about the graph's own proposal and
                # stands on its named reason.
                evidence_refs=(tuple(f"term:{t}" for t in candidate.shared) if accepted else ()),
                trust=TrustTier.EARNED,
                reason=(
                    f"deterministic match, score {candidate.score}"
                    if accepted
                    else f"{candidate.reason}: score {candidate.score}"
                ),
                author=author,
                created_at=stamped,
            )
            out.append((candidate, patch))
    return out
