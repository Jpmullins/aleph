"""`propose` has to finish on a real project, and drop nothing when it does.

WS-RS8 criterion 6. The reconciler scored every pair of live claims: 5,000
claims is 12,497,500 calls, each re-tokenizing both strings — twenty-five
million `re.findall` runs over five thousand distinct texts. It never mattered
because the belief layer had no caller. RS8 gives it one, so it matters now.

The interesting half is not that blocking is fast. It is that blocking is
**exact** here, and the test that proves it is `test_blocking_drops_nothing`:
speed bought by silently missing merges would be much worse than slowness, and
"we dedupe your knowledge base, mostly" is not a property anyone can act on.
"""

from __future__ import annotations

import random
import time
from uuid import UUID, uuid4

import pytest

from aleph_belief.reconcile import (
    DEFAULT_MAX_COMPARISONS,
    ClaimRef,
    propose,
    propose_with_stats,
    score_pair,
)

PROJECT = UUID("00000000-0000-0000-0000-00000000000b")

#: A synthetic corpus with a REALISTIC SHAPE, which is what the timing claim is
#: about. Two earlier fixtures were wrong in the same way and both are worth
#: recording, because each looked reasonable:
#:
#:   1. "{subject} record {i} shows a shift in regime {i}" — seven tokens, four
#:      shared by every claim. Blocking pruned nothing, and that was CORRECT:
#:      containment was 4/7 for every pair, so all 12,497,500 were genuine
#:      escalations.
#:   2. A 50-word vocabulary over 5,000 claims. Average posting list ~600, and
#:      a million real survivors. Also correct, also not a project.
#:
#: A corpus where every claim really is a near-match measures the backstop, not
#: the filter — and it is measured deliberately, in
#: `test_a_pathological_corpus_reports_truncation_instead_of_hanging`.
#:
#: So: ~900 distinct terms, sampled Zipf-ish so a handful are common and most
#: are rare, which is the property blocking exploits and the property real
#: vocabulary has. The terms are nonsense; the DISTRIBUTION is the fixture.
_VOCAB_SIZE = 900
_CLAIM_TERMS = 10


def _vocabulary() -> list[str]:
    roots = ["thal", "morp", "cryo", "litho", "pelag", "eolic", "flu", "gla", "sed", "iso"]
    stems = ["chron", "graph", "morph", "phase", "flux", "genic", "cline", "form", "lith", "zone"]
    return [
        f"{r}{s}{i}"
        for i, (r, s) in enumerate(
            (r, s) for r in roots for s in stems for _ in range(_VOCAB_SIZE // 100 + 1)
        )
    ][:_VOCAB_SIZE]


def _claims(n: int) -> list[ClaimRef]:
    """Deterministic, but not uniform. Seeded so a failure reproduces exactly."""
    rng = random.Random(20260822)
    vocab = _vocabulary()
    # Zipf-ish: index 0 is drawn far more often than index 899, so the corpus
    # has a few workhorse terms and a long tail — same shape as real prose, and
    # the reason a rarest-first prefix is worth computing at all.
    weights = [1.0 / (i + 1) for i in range(len(vocab))]
    out: list[ClaimRef] = []
    for _ in range(n):
        terms = rng.choices(vocab, weights=weights, k=_CLAIM_TERMS)
        out.append(ClaimRef(id=uuid4(), text=" ".join(terms), origin="test"))
    return out


def test_propose_is_bounded() -> None:
    """5,000 claims, under five seconds, with the scan reported."""
    claims = _claims(5_000)
    started = time.monotonic()
    results, stats = propose_with_stats(
        claims, project_id=PROJECT, profile_hash="p", graph_hash="g"
    )
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"took {elapsed:.1f}s"
    assert stats.claims == 5_000
    assert stats.pairs_total == 12_497_500
    # The filter is the point, so the assertion is about the ratio and not just
    # about finishing. Measured on this corpus: 137,725 of 12,497,500, or about
    # one pair in ninety. The bound is set at one in fifty so a modest
    # regression is tolerated and a collapse back toward O(n^2) is not.
    assert stats.pairs_scored < stats.pairs_total // 50, (
        f"blocking barely pruned: {stats.pairs_scored} of {stats.pairs_total}"
    )
    assert not stats.truncated, "the backstop fired on a realistic corpus"
    assert stats.pairs_scored == len(results)


def _uneven_claims(n: int) -> list[ClaimRef]:
    """Claims of deliberately unequal length, sharing terms across the divide.

    Containment normalizes by the SHORTER claim, so a three-term claim can be
    fully contained in a twenty-term one and the pair legitimately qualifies.
    That is precisely the case a one-directional probe drops, and only when the
    shorter claim happens to sort to the higher index — which a corpus of
    uniform-length claims can never exhibit.

    The uniform fixture used for timing does not produce this and cannot: it
    was silently unable to catch the exact bug this file exists to catch.
    """
    rng = random.Random(7)
    vocab = _vocabulary()[:120]
    out: list[ClaimRef] = []
    for i in range(n):
        size = 3 if i % 3 == 0 else rng.choice([8, 14, 20])
        start = rng.randrange(0, 100)
        terms = vocab[start : start + size]
        out.append(ClaimRef(id=uuid4(), text=" ".join(terms), origin="test"))
    return out


def _dense_claims(n: int) -> list[ClaimRef]:
    """Paraphrase families — the thing deduplication actually exists for.

    Random sampling from a small vocabulary does NOT produce matches, which is
    worth knowing: six terms drawn from twenty-four share three at best, and
    three of six scores 0.44 against a 0.45 floor. Every pair rejects.

    So the families are explicit. Each has a stable core plus a rotating
    modifier, which is what a restatement of the same belief looks like, and
    what containment-over-Jaccard was weighted to catch in the first place.
    """
    rng = random.Random(11)
    vocab = _vocabulary()[:200]
    out: list[ClaimRef] = []
    family_size = 5
    for family in range((n // family_size) + 1):
        core = vocab[family * 4 % 180 : family * 4 % 180 + 6]
        for variant in range(family_size):
            if len(out) >= n:
                break
            extra = vocab[(family * 7 + variant) % len(vocab)]
            terms = [*core, extra] if variant else list(core)
            rng.shuffle(terms)
            out.append(ClaimRef(id=uuid4(), text=" ".join(terms), origin="test"))
    return out[:n]


@pytest.mark.parametrize("corpus", ["dense", "uneven"])
def test_blocking_drops_nothing(corpus: str) -> None:
    """The exactness claim, checked against the unblocked answer.

    The only thing that makes the speed test above trustworthy: speed bought by
    silently missing merges is far worse than slowness, and "we deduplicate your
    knowledge base, mostly" is not a property anyone can act on.

    This test has already earned its keep once. `score <= containment`, so a
    pair below the containment floor cannot be an accept or an escalation —
    the algebra was right, and the LOOP was wrong: it probed `prefix(i)` only
    against `j > i`, dropping every qualifying pair whose shorter half sat at
    the higher index. Comparing against the exhaustive answer found it;
    restating the proof in an assertion would not have.
    """
    claims = _dense_claims(300) if corpus == "dense" else _uneven_claims(300)
    kept = {
        (str(c.left.id), str(c.right.id))
        for c, _patch in propose(claims, project_id=PROJECT, profile_hash="p", graph_hash="g")
        if c.verdict != "reject"
    }

    exhaustive: set[tuple[str, str]] = set()
    ordered = sorted(claims, key=lambda c: str(c.id))
    for i, left in enumerate(ordered):
        for right in ordered[i + 1 :]:
            candidate = score_pair(left, right)
            if candidate.verdict != "reject":
                exhaustive.add((str(left.id), str(right.id)))

    # The guard that caught this file's second mistake: a 300-claim corpus of
    # uniform-length claims over a 900-term vocabulary produced ZERO matches, so
    # `kept == exhaustive` was `set() == set()` and had been passing vacuously.
    assert exhaustive, "the corpus produced no matches at all — it proves nothing"
    assert kept == exhaustive, f"blocking lost {len(exhaustive - kept)} pair(s)"


def test_a_short_claim_at_a_higher_index_is_still_found() -> None:
    """The directional bug, as a constructed case rather than a hoped-for one.

    Two randomly-generated corpora both failed to catch this, which is the
    lesson: a property that depends on an ordering coincidence has to be built,
    not sampled. Here every part is pinned.

    `common-a/b/c` appear in fifty filler claims, so they are the most FREQUENT
    terms in the corpus and rarest-first ordering pushes them out of the long
    claim's prefix. The short claim is fully contained in the long one, so
    containment is 1.0 and the pair scores ~0.70 — an escalation, not a reject.

    Its id sorts LAST, so a probe restricted to `j > i` never runs the short
    claim against anything, and the long claim's prefix holds only rare terms
    the short claim does not have. The pair vanishes. With bidirectional
    normalization the short claim's own prefix finds it.
    """
    rare = [f"rare{i}" for i in range(17)]
    common = ["commona", "commonb", "commonc"]

    long_claim = ClaimRef(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        text=" ".join(common + rare),
        origin="test",
    )
    short_claim = ClaimRef(
        id=UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
        text=" ".join(common),
        origin="test",
    )
    filler = [
        ClaimRef(
            id=UUID(int=0x1000 + i),
            text=" ".join(common) + f" filler{i} filler{i}b filler{i}c filler{i}d",
            origin="test",
        )
        for i in range(50)
    ]

    # The pair is a genuine non-reject: assert that directly, so a threshold
    # change turns this into a clear failure rather than a silent pass.
    assert score_pair(long_claim, short_claim).verdict != "reject"

    claims = [long_claim, *filler, short_claim]
    found = {
        (str(c.left.id), str(c.right.id))
        for c, _p in propose(claims, project_id=PROJECT, profile_hash="p", graph_hash="g")
        if c.verdict != "reject"
    }
    assert (str(long_claim.id), str(short_claim.id)) in found, (
        "the short claim at the higher index was never probed"
    )


def test_a_pathological_corpus_reports_truncation_instead_of_hanging() -> None:
    """Every claim sharing one word blocks into a single bucket — O(n²) again.

    Blocking makes this rare, not impossible, and the honest response to hitting
    the backstop is to say so. A dedupe pass that quietly skips pairs is
    indistinguishable from one that found nothing to merge: both return an empty
    list. `truncated` is the entire difference.
    """
    claims = [
        ClaimRef(id=uuid4(), text=f"identical subject variant {i}", origin="test")
        for i in range(400)
    ]
    _results, stats = propose_with_stats(
        claims, project_id=PROJECT, profile_hash="p", graph_hash="g", limit=1_000
    )
    assert stats.truncated
    assert stats.pairs_scored == 1_000
    assert stats.limit == 1_000


def test_the_default_limit_is_stated_not_implied() -> None:
    """A cap nobody can name is a cap nobody can reason about."""
    assert DEFAULT_MAX_COMPARISONS == 2_000_000
    _r, stats = propose_with_stats(
        _claims(10), project_id=PROJECT, profile_hash="p", graph_hash="g"
    )
    assert stats.limit == DEFAULT_MAX_COMPARISONS
    assert not stats.truncated


@pytest.mark.parametrize("n", [0, 1, 2])
def test_tiny_inputs(n: int) -> None:
    _r, stats = propose_with_stats(_claims(n), project_id=PROJECT, profile_hash="p", graph_hash="g")
    assert stats.claims == n
    assert stats.pairs_total == n * (n - 1) // 2
