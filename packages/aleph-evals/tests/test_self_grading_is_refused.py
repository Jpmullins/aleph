"""The self-grading guard, driven through the runner rather than asserted.

`score()` refuses a case that carries its own `actual`, and `_run_dataset`
counts that refusal as an ERROR. Both halves were written, both work, and
`grep -rn SelfGradingFixture` over the tree returned five hits, all in `src/`:
nothing in any test named it, so deleting either half would have gone
unnoticed by every gate.

That matters more here than almost anywhere else in the repo. The defect the
guard exists to stop is a `pass_rate: 1.0` produced by a JSON file agreeing
with itself — a green that measures nothing. An unpinned guard against a false
green is itself a false green.

Every test below drives `aleph_evals.runner.run`, the same entry point
`python -m aleph_evals` and the CI gate call, over a dataset written to a temp
directory. Nothing asserts a fixture the test built and then read back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aleph_evals.runner import Gate, run
from aleph_evals.scorers import SelfGradingFixture, score

_MANIFEST = (
    "datasets:\n"
    "  - name: selfgrade\n"
    "    file: cases.jsonl\n"
    "    kind: retrieval\n"
    "    gate_kind: blocking\n"
)

#: One case the retrieval scorer can grade. `expected_page_slugs` is what the
#: system is asked to find; `actual` is what a real run returned and must be
#: passed in as `actual=`, never read out of the file.
_GRADEABLE = '{"expected_page_slugs": ["alpha"], "threshold": 0.7}\n'

#: The same case, plus the answer. This is the shape of the six fixtures that
#: shipped before the guard existed.
_SELF_GRADING = (
    '{"expected_page_slugs": ["alpha"], "threshold": 0.7, "actual": {"page_slugs": ["alpha"]}}\n'
)


def _dataset(root: Path, cases: str) -> Path:
    d = root / "inc0_selfgrade"
    d.mkdir(parents=True)
    (d / "cases.jsonl").write_text(cases)
    (d / "manifest.yaml").write_text(_MANIFEST)
    return root


def test_a_case_carrying_its_own_answer_is_errored_not_passed(tmp_path: Path) -> None:
    """The criterion, literally: `actual` in the file → `errored`."""
    report = run(datasets_root=_dataset(tmp_path, _SELF_GRADING), gate=Gate.STRICT)

    assert report.selected_datasets == ["selfgrade"]
    (result,) = report.results
    assert result.cases_total == 1
    assert result.cases_errored == 1, "a fixture that grades itself must not be scored"
    assert result.cases_passed == 0, (
        "the file agreed with itself and the runner called that a pass — "
        "this is the exact number that made `pass_rate: 1.0` meaningless"
    )
    assert result.metrics["error_rate"] == 1.0
    assert result.metrics["pass_rate"] == 0.0


def test_the_error_is_not_a_general_malformed_case(tmp_path: Path) -> None:
    """It is the `actual` key that errors the case, not the case being odd.

    `_run_dataset` has a bare `except Exception` beside the
    `except SelfGradingFixture`, so "errored" on its own does not distinguish
    the guard from any other blow-up. Grading the same case WITHOUT the key
    proves the difference: identical bytes minus `actual`, and it grades.
    """
    without = run(datasets_root=_dataset(tmp_path / "a", _GRADEABLE), gate=Gate.STRICT)
    (clean,) = without.results
    assert clean.cases_errored == 0
    assert clean.cases_total == 1

    with_answer = run(datasets_root=_dataset(tmp_path / "b", _SELF_GRADING), gate=Gate.STRICT)
    (dirty,) = with_answer.results
    assert dirty.cases_errored == 1


def test_a_self_grading_dataset_fails_the_gate(tmp_path: Path) -> None:
    """Counting it as an error is only useful if the error is fatal.

    `RunReport.any_failures` reads `cases_errored`, so a self-grading dataset
    has to make `python -m aleph_evals` exit non-zero. If it merely appeared in
    a metrics blob, the six fixtures that shipped this way would still be in CI
    reporting a number nobody could act on.
    """
    report = run(datasets_root=_dataset(tmp_path, _SELF_GRADING), gate=Gate.STRICT)
    assert report.any_failures is True
    assert report.exit_code() == 1


def test_score_refuses_with_the_named_exception() -> None:
    """The refusal is `SelfGradingFixture`, not a bare `RuntimeError`.

    Pinned by TYPE because `_run_dataset` catches it by name. Replacing the
    raise with a plain `ValueError` would still land in the bare `except` and
    still count as an error, so the count alone cannot tell that the guard is
    still the guard.
    """
    with pytest.raises(SelfGradingFixture) as excinfo:
        score(
            "retrieval",
            {"expected_page_slugs": ["alpha"], "actual": {"page_slugs": ["alpha"]}},
            profile_name="aleph-dev",
        )
    assert "actual" in str(excinfo.value)


def test_the_observed_result_is_scored_when_passed_in_properly() -> None:
    """The guard must not block the correct usage it exists to force.

    Same expectation, same observation — but the observation arrives as
    `actual=` from a caller that ran something, which is the whole distinction.
    A guard that also refused this would just have turned every dataset off.
    """
    ok, recall = score(
        "retrieval",
        {"expected_page_slugs": ["alpha"], "threshold": 0.7},
        profile_name="aleph-dev",
        actual={"page_slugs": ["alpha"]},
    )
    assert ok is True
    assert recall == 1.0
