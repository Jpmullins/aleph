"""The restore drill's ledger-chain rules, pinned without a database.

`scripts/_acceptance/restore_drill.py` does not require the restored ledger chain
to verify cleanly. It requires the restored chain and the source chain to AGREE.
That is the subtle part of the whole workstream and it is easy to get wrong in
either direction:

  * too strict, and the drill is red forever for a reason that has nothing to do
    with backups — the development database really does contain projects whose
    chain does not verify, because every run of
    `tests/integration/test_ledger_immutability.py::test_tampering_is_detectable`
    forges a `chain_hash` into a fresh project and leaves it there. A check that
    is red for an unrelated reason gets ignored, which is worse than no check;
  * too loose, and it accepts a round trip that changed a payload, moved a
    divergence, or silently repaired a tampered ledger — which is the whole
    thing it exists to catch.

So the rule is: appending to the source is fine (the source is live and a later
event cannot change where an earlier chain FIRST diverges); everything else must
match. These tests state each half of that as a case, so a future simplification
of `_compare_chains` cannot quietly drop one.

Loaded by path because `scripts/_acceptance` is not an installed package and the
drill is run as a script, not imported.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType

_DRILL_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "_acceptance" / "restore_drill.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aleph_restore_drill", _DRILL_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {_DRILL_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_drill = _load()
_compare = _drill._compare_chains

# (first_divergence_event_id, event_count, event_ids)
_CLEAN = (None, 3, frozenset({"e1", "e2", "e3"}))
_DIVERGES_AT_E2 = ("e2", 3, frozenset({"e1", "e2", "e3"}))


def test_the_drill_file_is_where_the_docs_say_it_is() -> None:
    # check-dead-refs.sh scans .md and .py; this pins the path for the test too,
    # so a rename cannot leave these cases silently loading nothing.
    assert _DRILL_PATH.exists()


def test_an_identical_round_trip_passes() -> None:
    source = {"p": _CLEAN}
    restored = {"p": _CLEAN}
    problems, projects, events, reproduced = _compare(source, restored)
    assert problems == []
    assert (projects, events, reproduced) == (1, 3, 0)


def test_a_pre_existing_divergence_reproduced_at_the_same_event_passes() -> None:
    """The 29 tamper-test projects in the dev database must not fail the drill."""
    source = {"p": _DIVERGES_AT_E2}
    restored = {"p": _DIVERGES_AT_E2}
    problems, _projects, _events, reproduced = _compare(source, restored)
    assert problems == []
    assert reproduced == 1, "a reproduced divergence must be COUNTED, not just tolerated"


def test_a_divergence_the_round_trip_introduced_fails() -> None:
    source = {"p": _CLEAN}
    restored = {"p": _DIVERGES_AT_E2}
    problems, _p, _e, _r = _compare(source, restored)
    assert len(problems) == 1
    assert "the round trip changed something the hash is computed over" in problems[0]


def test_a_divergence_that_moved_to_a_different_event_fails() -> None:
    source = {"p": ("e3", 3, frozenset({"e1", "e2", "e3"}))}
    restored = {"p": _DIVERGES_AT_E2}
    problems, _p, _e, _r = _compare(source, restored)
    assert len(problems) == 1
    assert "diverges at e2" in problems[0] and "the source at e3" in problems[0]


def test_a_round_trip_that_silently_repaired_a_tampered_ledger_fails() -> None:
    """The direction nobody thinks to check, and the one a rendering change causes.

    If jsonb key order, timestamp precision or float rendering shifted in the
    round trip, a chain that did not verify at the source can start verifying in
    the restore. That is not good news — it means the bytes the hash is computed
    over are not the bytes that were backed up.
    """
    source = {"p": _DIVERGES_AT_E2}
    restored = {"p": _CLEAN}
    problems, _p, _e, _r = _compare(source, restored)
    assert len(problems) == 1
    assert "silently repaired a tampered ledger" in problems[0]


def test_events_appended_to_the_live_source_after_the_snapshot_are_not_a_failure() -> None:
    """The source keeps being written to while the drill runs. That must be fine.

    Here the source has grown a fourth event and diverges at it — an event the
    dump never saw. The restore is clean over the three it did see, and the drill
    must not report that as the restore having lost something.
    """
    source = {"p": ("e4", 4, frozenset({"e1", "e2", "e3", "e4"}))}
    restored = {"p": _CLEAN}
    problems, _p, _e, _r = _compare(source, restored)
    assert problems == []


def test_a_project_present_only_in_the_restore_is_reported() -> None:
    """Impossible against an append-only source — so it must never be skipped."""
    problems, _p, _e, _r = _compare({}, {"p": _CLEAN})
    assert len(problems) == 1
    assert "in the restore and not in the source" in problems[0]


def test_every_restored_project_is_counted_even_when_another_one_fails() -> None:
    """One bad project must not stop the walk — the drill reports all of them."""
    source = {"good": _CLEAN, "bad": _CLEAN}
    restored = {"good": _CLEAN, "bad": _DIVERGES_AT_E2}
    problems, projects, events, _r = _compare(source, restored)
    assert len(problems) == 1
    assert (projects, events) == (2, 6)
