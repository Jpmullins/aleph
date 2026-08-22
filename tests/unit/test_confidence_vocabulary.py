"""One confidence vocabulary — the sweep, the migration, and the tier weights.

Three separate things are pinned here, and they fail for three different
reasons:

1. **The sweep can fail.** `scripts/check-confidence-vocabulary.sh` is only
   worth having if it fires on the defect it was written for. Every reader it
   inspects is fed a deliberately-wrong copy and the mismatch is asserted by
   name — a sweep that returns "no findings" for a subject it never read is the
   failure mode `sweep_subject` exists to make impossible, and it has happened
   in this repo three times.
2. **The migration's copy of the legacy map cannot drift.** The Alembic
   revision cannot import `aleph_core` — a migration that imports application
   code stops replaying the day that code is refactored — so it holds its own
   copy of `LEGACY_CONFIDENCE`. Duplication is fine; silent divergence is not.
3. **The top confidence state is reachable.** `WELL_SUPPORTED` requires one
   piece of evidence weighing >= 1.5 and every writer in the tree stamped 1.0,
   so 850 live claims sat below a ceiling nothing could reach and no error was
   raised anywhere. The trust lattice supplies the missing number.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from confidence_vocabulary import (
    canonical_values,
    catalog_enums,
    compare,
    grounding_surface_keys,
    html_compiler_keys,
    web_switch_cases,
    web_union_values,
)
from sweep_subject import MissingSubject

from aleph_belief.trust import TrustTier
from aleph_core.confidence import (
    CONFIDENCE_VALUES,
    LEGACY_CONFIDENCE,
    Confidence,
    canonical_confidence,
)
from aleph_hypotheses.confidence import (
    HIGH_TIER_WEIGHT,
    TIER_WEIGHTS,
    EvidenceRow,
    next_confidence_from_evidence,
    weight_for_tier,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "apps/api/alembic/versions/20260822_0100_rs9_one_confidence_vocabulary.py"


# ---------------------------------------------------------------------------
# 1. The vocabulary itself
# ---------------------------------------------------------------------------


def test_the_tree_agrees_with_itself_today() -> None:
    """The state the repo is supposed to be in. This is the sweep's green case."""
    assert compare() == []


def test_every_reader_is_actually_read() -> None:
    """A sweep that inspects nothing reports no findings.

    Each of these returns real content or the sweep is looking at the wrong
    file, in which case the green above means nothing.
    """
    assert canonical_values() == list(CONFIDENCE_VALUES)
    assert len(catalog_enums()) >= 2, "the catalog declares confidence in more than one place"
    assert web_union_values() == list(CONFIDENCE_VALUES)
    assert web_switch_cases() == set(CONFIDENCE_VALUES)
    assert grounding_surface_keys() == set(CONFIDENCE_VALUES)
    assert html_compiler_keys() == set(CONFIDENCE_VALUES)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        # The catalog gains back one of the words it used to permit.
        (
            "packages/aleph-a2ui/src/aleph_a2ui/catalog.json",
            '"under_investigation",\n                  "weakly_supported"',
            '"cited",\n                  "weakly_supported"',
        ),
        # The client union loses a state the engine can emit.
        (
            "apps/web/src/a2ui/confidence.ts",
            '  "refuted",\n',
            "",
        ),
        # A renderer keeps a branch for a state that no longer exists.
        (
            "apps/web/src/a2ui/components/GroundingSurface.tsx",
            "  refuted: ",
            "  retracted: ",
        ),
        # The HTML compiler drops a badge colour.
        (
            "packages/aleph-wiki/src/aleph_wiki/html_compiler.py",
            "    Confidence.ABANDONED:",
            "    # Confidence.ABANDONED:",
        ),
    ],
    ids=[
        "catalog-regains-cited",
        "union-loses-refuted",
        "renderer-keeps-retracted",
        "compiler-drops-abandoned",
    ],
)
def test_the_sweep_fires_on_each_reader(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, path: str, old: str, new: str
) -> None:
    """Break one reader at a time; the sweep must name it.

    The whole tree is copied to a temp dir and the sweep repointed at the copy,
    so a mutation can never escape into the working tree — the repo has already
    paid for a "restore" step that did not run.
    """
    import confidence_vocabulary as cv

    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    assert old in text, f"mutation anchor {old!r} not in {path} — the mutation would be a no-op"

    copy = tmp_path / "subject"
    copy.mkdir()
    for attr in (
        "CANONICAL_PY",
        "CATALOG_JSON",
        "WEB_CONFIDENCE_TS",
        "HTML_COMPILER_PY",
        "GROUNDING_TSX",
    ):
        original: pathlib.Path = getattr(cv, attr)
        rel = original.relative_to(ROOT)
        dest = copy / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = original.read_text(encoding="utf-8")
        if original == target:
            body = body.replace(old, new, 1)
            assert body != original.read_text(encoding="utf-8"), "mutation applied nothing"
        dest.write_text(body, encoding="utf-8")
        monkeypatch.setattr(cv, attr, dest)

    problems = cv.compare()
    assert problems, f"mutating {path} produced no finding — the sweep is not reading it"


def test_a_missing_subject_raises_rather_than_passing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A moved file must crash the sweep, not quietly empty it."""
    import confidence_vocabulary as cv

    monkeypatch.setattr(cv, "WEB_CONFIDENCE_TS", ROOT / "apps/web/src/a2ui/gone.ts")
    with pytest.raises(MissingSubject):
        cv.compare()


# ---------------------------------------------------------------------------
# 2. The migration's copy of the legacy map
# ---------------------------------------------------------------------------


def _load_migration() -> object:
    spec = importlib.util.spec_from_file_location("rs9_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_maps_exactly_what_the_code_maps() -> None:
    """The duplicated legacy table must not drift from the one in `aleph-core`."""
    module = _load_migration()
    migration_map: dict[str, str] = module.LEGACY_TO_CANONICAL  # type: ignore[attr-defined]
    assert migration_map == {k: v.value for k, v in LEGACY_CONFIDENCE.items()}
    assert tuple(module.CANONICAL) == CONFIDENCE_VALUES  # type: ignore[attr-defined]


def test_a_legacy_spelling_is_translated_and_an_invented_one_raises() -> None:
    assert canonical_confidence("cited") is Confidence.WEAKLY_SUPPORTED
    assert canonical_confidence("uncited") is Confidence.UNDER_INVESTIGATION
    assert canonical_confidence("well-supported") is Confidence.WELL_SUPPORTED
    assert canonical_confidence("contested") is Confidence.CONTESTED
    with pytest.raises(ValueError, match="not a confidence"):
        canonical_confidence("very sure")


def test_no_legacy_spelling_is_also_a_canonical_one() -> None:
    """A word in both tables would make the write-path guard a no-op for it."""
    assert not set(LEGACY_CONFIDENCE) & set(CONFIDENCE_VALUES)


# ---------------------------------------------------------------------------
# 3. The top confidence state is reachable
# ---------------------------------------------------------------------------


def test_three_top_tier_supports_reach_well_supported() -> None:
    """The criterion. Fails if the tier weights go back to a flat 1.0.

    `EARNED` is a quote located verbatim in ingested source text — the tier the
    belief write path stamps on every citation it accepts.
    """
    evidence = [EvidenceRow.at_tier("supports", TrustTier.EARNED) for _ in range(3)]
    assert next_confidence_from_evidence(evidence) is Confidence.WELL_SUPPORTED


def test_three_agent_assertions_do_not() -> None:
    """The threshold has to mean something: volume alone must not buy the top state."""
    evidence = [EvidenceRow.at_tier("supports", TrustTier.ASSERTED) for _ in range(3)]
    assert next_confidence_from_evidence(evidence) is Confidence.WEAKLY_SUPPORTED


def test_a_missing_tier_is_an_assertion_not_an_earning() -> None:
    """UNKNOWN provenance must not be scored as corpus-grounded."""
    assert weight_for_tier(None) == TIER_WEIGHTS[TrustTier.ASSERTED]
    assert (
        next_confidence_from_evidence([EvidenceRow.at_tier("supports", None) for _ in range(4)])
        is Confidence.WEAKLY_SUPPORTED
    )


def test_the_threshold_and_the_lattice_are_the_same_number() -> None:
    """`HIGH_TIER_WEIGHT` exists to be the EARNED weight; drift makes it a magic 1.5."""
    assert TIER_WEIGHTS[TrustTier.EARNED] == HIGH_TIER_WEIGHT
    assert set(TIER_WEIGHTS) == set(TrustTier)


def test_every_state_the_machine_can_emit_is_in_the_vocabulary() -> None:
    """The engine and the alphabet are separate modules; they must not diverge."""
    reachable = {
        next_confidence_from_evidence([]),
        next_confidence_from_evidence([EvidenceRow("supports", 1.0)]),
        next_confidence_from_evidence([EvidenceRow.at_tier("supports", TrustTier.SIGNED)] * 3),
        next_confidence_from_evidence([EvidenceRow("contradicts", 1.0)]),
        next_confidence_from_evidence([EvidenceRow("contradicts", 3.0)]),
    }
    assert reachable <= set(Confidence)
    assert {c.value for c in reachable} <= set(CONFIDENCE_VALUES)
