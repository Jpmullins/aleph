"""Eval runner skeleton tests."""

from __future__ import annotations

from pathlib import Path

from aleph_evals.runner import Gate, run


def test_empty_root_passes_strict(tmp_path: Path) -> None:
    report = run(datasets_root=tmp_path, selected="all", gate=Gate.STRICT)
    assert report.selected_datasets == []
    assert report.results == []
    assert report.exit_code() == 0


def test_missing_root_passes_strict(tmp_path: Path) -> None:
    report = run(datasets_root=tmp_path / "does-not-exist", gate=Gate.STRICT)
    assert report.exit_code() == 0


def test_discovers_dataset_dirs_with_manifest(tmp_path: Path) -> None:
    d = tmp_path / "demo"
    d.mkdir()
    (d / "manifest.yaml").write_text("name: demo\n")
    other = tmp_path / "not-a-dataset"
    other.mkdir()
    report = run(datasets_root=tmp_path, gate=Gate.STRICT)
    assert report.selected_datasets == ["demo"]
