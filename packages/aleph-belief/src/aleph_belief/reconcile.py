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
    from collections.abc import Iterable, Iterator, Sequence

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


@dataclass(frozen=True)
class _Prepared:
    """One claim, tokenized once.

    `score_pair` used to tokenize both of its arguments on every call, and
    `propose` calls it once per pair — so a 5,000-claim project ran
    `_WORD.findall` twenty-five million times over five thousand distinct
    strings. The blocking below cuts how many pairs are scored; this cuts what
    each one costs, and the two are independent wins.
    """

    ref: ClaimRef
    tokens: tuple[str, ...]
    token_set: frozenset[str]
    numbers: frozenset[str]

    @staticmethod
    def of(ref: ClaimRef) -> _Prepared:
        tokens = tuple(_tokens(ref.text))
        return _Prepared(
            ref=ref,
            tokens=tokens,
            token_set=frozenset(tokens),
            numbers=frozenset(_numbers(tokens)),
        )


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
    return _score_prepared(_Prepared.of(left), _Prepared.of(right))


def _score_prepared(left_p: _Prepared, right_p: _Prepared) -> Candidate:
    """`score_pair`'s body, over already-tokenized claims.

    Kept byte-for-byte equivalent to what `score_pair` did, so the public
    function stays the tested one and `propose` gets the same answers faster.
    """
    left, right = left_p.ref, right_p.ref
    ls, rs = left_p.token_set, right_p.token_set
    shared = tuple(sorted(ls & rs))
    distinct = tuple(sorted(ls ^ rs))

    if not ls or not rs:
        return Candidate(left, right, 0.0, RejectReason.INSUFFICIENT_OVERLAP, shared, distinct)

    # Disqualifiers, in order of how badly an overlap score misjudges them.
    if bool(ls & _NEGATIONS) != bool(rs & _NEGATIONS):
        return Candidate(left, right, 0.0, RejectReason.NEGATION_MISMATCH, shared, distinct)

    ln, rn = left_p.numbers, right_p.numbers
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
    results, _stats = propose_with_stats(
        claims,
        project_id=project_id,
        profile_hash=profile_hash,
        graph_hash=graph_hash,
        author=author,
        now=now,
    )
    return results


@dataclass(frozen=True)
class ProposeStats:
    """What `propose` actually looked at. Printed, never inferred.

    A deduplication pass that quietly skips pairs is indistinguishable from one
    that found nothing to merge — both return an empty list. `truncated` is the
    difference, and a caller that ignores it is claiming a completeness it does
    not have.
    """

    claims: int
    #: Pairs the unblocked O(n²) loop would have scored.
    pairs_total: int
    #: Pairs that survived blocking and were actually scored.
    pairs_scored: int
    #: True when `limit` cut the scan short — the result is a SAMPLE, not a sweep.
    truncated: bool
    limit: int | None


#: Comparisons `propose` will make before it stops and says so.
#:
#: Blocking makes the pathological case rare, not impossible: a project whose
#: claims all share one content word blocks into a single bucket and is O(n²)
#: again. This is the backstop for that, and it is high enough that no realistic
#: project reaches it — 5,000 claims scan in well under a second once blocked.
DEFAULT_MAX_COMPARISONS = 2_000_000


def propose_with_stats(
    claims: Sequence[ClaimRef],
    *,
    project_id: UUID,
    profile_hash: str,
    graph_hash: str,
    author: str = "reconciler:deterministic",
    now: datetime | None = None,
    limit: int | None = DEFAULT_MAX_COMPARISONS,
) -> tuple[list[tuple[Candidate, BeliefPatch | None]], ProposeStats]:
    """`propose`, plus what it had to skip to finish.

    **Why blocking, and why this particular blocking.** The original loop scored
    every pair: 5,000 live claims is 12,497,500 calls, each re-tokenizing both
    strings. It was never run on a real project, so it never mattered; RS8 gives
    the belief layer its first caller and it would have mattered immediately.

    The rule is: **only claims sharing at least one content token are scored.**
    That is not a heuristic, it is exact for the thresholds in this module.
    `verdict` rejects anything at or below `LOW_CONFIDENCE`, and

        score = 0.35 * jaccard + 0.65 * containment ,  jaccard <= containment

    so `score <= containment` always. A pair with no shared token has
    containment 0, therefore score 0, therefore `reject`. Skipping it changes
    nothing — it cannot become an accept or an escalation. The pruning is free,
    and a stronger prefix filter would not be: it would buy speed by risking a
    missed merge, which is the wrong trade for a knowledge base.

    Rejections are also not what a caller wants. `propose` returns
    `(candidate, patch)` for every scored pair including rejects, so on a large
    project the vast majority of the return value was REJECT_MATCH patches for
    claims about entirely unrelated subjects. Those are noise with a cost.
    """
    stamped = now or datetime.now(UTC)
    ordered = [_Prepared.of(c) for c in sorted(claims, key=lambda c: str(c.id))]
    out: list[tuple[Candidate, BeliefPatch | None]] = []
    n = len(ordered)
    pairs_total = n * (n - 1) // 2

    scored = 0
    truncated = False
    for i, j in _blocked_pairs(ordered):
        if limit is not None and scored >= limit:
            truncated = True
            break
        scored += 1
        candidate = _score_prepared(ordered[i], ordered[j])
        verdict = candidate.verdict
        if verdict == "escalate":
            out.append((candidate, None))
            continue

        accepted = verdict == "accept"
        patch = BeliefPatch(
            id=uuid4(),
            project_id=project_id,
            operation=(PatchOperation.ACCEPT_MATCH if accepted else PatchOperation.REJECT_MATCH),
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

    return out, ProposeStats(
        claims=n,
        pairs_total=pairs_total,
        pairs_scored=scored,
        truncated=truncated,
        limit=limit,
    )


def _blocked_pairs(ordered: list[_Prepared]) -> Iterator[tuple[int, int]]:
    """Yield only the pairs that could survive scoring. Exact, not heuristic.

    Two filters, both derived from the thresholds in this module rather than
    tuned:

    **1. Enough shared terms.** `verdict` rejects anything at or below
    `LOW_CONFIDENCE`, and

        score = 0.35 * jaccard + 0.65 * containment ,  jaccard <= containment

    so `score <= containment` always. A non-reject therefore requires

        |L n R| > LOW_CONFIDENCE * min(|L|, |R|)

    A pair failing that is a reject no matter what else is true, so skipping it
    cannot lose an accept or an escalation. `test_blocking_drops_nothing` checks
    this against the exhaustive answer rather than trusting the algebra.

    **2. Probe with the rarest terms first.** For a pair needing `k` shared
    terms, the shorter claim can hold at most `k - 1` of them outside any
    prefix of length `|S| - k + 1` — so probing that prefix cannot miss the
    pair. Ordering tokens by ascending document frequency puts the rare,
    discriminating terms in the prefix and leaves the corpus-wide ones out of
    it. That is what stops "shows", "record" and "increased" from generating a
    posting list containing the entire project.

    Order is ascending `(i, j)`, because `propose`'s contract is that two runs
    over the same claims produce the same list, and a set-iteration order would
    break that in a way only a flaky test would catch. Candidates are collected
    per `i` rather than into one global set: the global set is the same size as
    the thing being avoided, and materializing twelve million tuples to prove
    they were unnecessary is its own kind of slow.
    """
    n = len(ordered)
    if n < 2:
        return

    frequency: dict[str, int] = {}
    for prepared in ordered:
        for token in prepared.token_set:
            frequency[token] = frequency.get(token, 0) + 1

    def by_rarity(token: str) -> tuple[int, str]:
        # Lexical tie-break so the ordering — and therefore every prefix — is
        # identical across runs and across processes.
        return (frequency[token], token)

    full: dict[str, list[int]] = {}
    prefixes: list[tuple[str, ...]] = []
    for idx, prepared in enumerate(ordered):
        tokens = sorted(prepared.token_set, key=by_rarity)
        prefixes.append(tuple(tokens[: _prefix_length(len(tokens))]))
        for token in tokens:
            full.setdefault(token, []).append(idx)

    # One probe per claim, against every other claim regardless of index order,
    # with the pair normalized on the way in.
    #
    # The guarantee is about the SHORTER claim of a pair: its prefix must
    # contain one of the shared terms. Which one is shorter is not known at
    # index time — so an earlier version probed `prefix(i)` only against `j > i`
    # and silently dropped every pair whose shorter half sat at the higher
    # index. `test_blocking_drops_nothing` caught it on 300 claims: the algebra
    # was right and the loop was not, which is why that test compares against
    # the exhaustive answer rather than restating the proof.
    #
    # Probing in one direction and normalizing covers both, because when the
    # shorter claim's turn comes its prefix hits the longer one's full index.
    # Two directional loops also work and cost twice the traversal.
    candidates: set[tuple[int, int]] = set()
    for i in range(n):
        for token in prefixes[i]:
            for j in full[token]:
                if j != i:
                    candidates.add((i, j) if i < j else (j, i))

    for i, j in sorted(candidates):
        left, right = ordered[i], ordered[j]
        floor_ = min(len(left.token_set), len(right.token_set)) * LOW_CONFIDENCE
        if len(left.token_set & right.token_set) > floor_:
            yield (i, j)


def _prefix_length(size: int) -> int:
    """How many of a claim's rarest terms have to be probed to miss nothing.

    A non-reject needs `k = floor(LOW_CONFIDENCE * size) + 1` shared terms. At
    most `k - 1` of them can sit outside a prefix of `size - k + 1`, so that
    prefix always contains one — which is the whole guarantee. Never below 1,
    or a one-token claim would be indexed and never probed.
    """
    if size <= 0:
        return 0
    needed = int(LOW_CONFIDENCE * size) + 1
    return max(1, size - needed + 1)
