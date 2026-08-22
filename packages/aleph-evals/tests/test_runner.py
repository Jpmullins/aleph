"""Eval runner skeleton tests."""

from __future__ import annotations

from pathlib import Path

from aleph_evals.runner import Gate, RunReport, run


def test_empty_root_is_not_a_pass(tmp_path: Path) -> None:
    """A run that discovered no datasets must not report success.

    This used to assert `exit_code() == 0`. The non-zero exit lived only in
    `cli.main`, so the report object — the thing `write_summary`, `gate_main`
    and every programmatic caller ask — still answered "pass" for a run that
    executed no case against any code. That is the acceptance gate's own
    original defect ("a green you did not see"), one layer down, and the test
    was pinning it in place.
    """
    report = run(datasets_root=tmp_path, selected="all", gate=Gate.STRICT)
    assert report.selected_datasets == []
    assert report.results == []
    assert report.evaluated_nothing is True
    assert report.exit_code() == 1


def test_missing_root_is_not_a_pass(tmp_path: Path) -> None:
    report = run(datasets_root=tmp_path / "does-not-exist", gate=Gate.STRICT)
    assert report.exit_code() == 1


def test_evaluating_nothing_fails_under_the_soft_gate_too(tmp_path: Path) -> None:
    """SOFT downgrades dataset failures, never a harness that found no work.

    `Gate.SOFT` exists so a `warning`-tier dataset can regress without blocking
    a merge. "I discovered zero datasets" is not a quality signal about a
    dataset; it is the runner failing to run, and there is no gate setting for
    which that should be green.
    """
    report = run(datasets_root=tmp_path, gate=Gate.SOFT)
    assert report.exit_code() == 1


def test_discovers_dataset_dirs_with_manifest(tmp_path: Path) -> None:
    # Inc-8 runner is manifest-driven: a `manifest.yaml` lists `datasets:` with
    # `name`/`file`, and the named dataset is discovered when its file exists.
    d = tmp_path / "inc0_demo"
    d.mkdir()
    (d / "cases.jsonl").write_text('{"input": {}, "expected": {}}\n')
    (d / "manifest.yaml").write_text(
        "datasets:\n"
        "  - name: demo\n"
        "    file: cases.jsonl\n"
        "    kind: metric_only\n"
        "    gate_kind: metric_only\n"
    )
    other = tmp_path / "not-a-dataset"
    other.mkdir()
    report = run(datasets_root=tmp_path, gate=Gate.STRICT)
    assert report.selected_datasets == ["demo"]
    # A discovered dataset is the case the rule above must NOT catch: this run
    # evaluated something, so its exit code has to come from the cases.
    assert report.evaluated_nothing is False
    assert report.exit_code() == 0


def test_a_report_with_datasets_is_not_failed_by_the_empty_rule() -> None:
    """The empty-selection rule reads `selected_datasets`, not `results`.

    Pinned separately because the two can disagree — a selected dataset whose
    every case errors produces a non-empty selection and a non-empty result
    list, and must fail for the case reason, with `evaluated_nothing` False.
    """
    report = RunReport(selected_datasets=["demo"], results=[], gate=Gate.STRICT)
    assert report.evaluated_nothing is False
    assert report.exit_code() == 0
