"""Eval runner.

Discovers datasets from a directory (default: `evals/datasets/`).
Each dataset is a directory with a `manifest.yaml` (loaded in Inc 8).
For Inc 0 we only need the discovery + reporting plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


class Gate(StrEnum):
    STRICT = "strict"
    SOFT = "soft"


class RunResult(BaseModel):
    dataset: str
    cases_total: int = 0
    cases_passed: int = 0
    cases_failed: int = 0
    cases_errored: int = 0
    notes: str | None = None


@dataclass
class RunReport:
    selected_datasets: list[str]
    results: list[RunResult] = field(default_factory=list)
    gate: Gate = Gate.STRICT

    @property
    def any_failures(self) -> bool:
        return any(r.cases_failed > 0 or r.cases_errored > 0 for r in self.results)

    def exit_code(self) -> int:
        if self.gate == Gate.STRICT and self.any_failures:
            return 1
        return 0


def _discover_datasets(root: Path) -> list[str]:
    if not root.exists():
        return []
    found: list[str] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "manifest.yaml").exists():
            found.append(child.name)
    return found


def _select(selected: str, available: list[str]) -> list[str]:
    if selected == "all":
        return available
    if selected.startswith("@"):
        # Tag-based selection (Inc 8 introduces tags). For now, no match.
        return []
    wanted = {s.strip() for s in selected.split(",") if s.strip()}
    return [d for d in available if d in wanted]


def run(
    *,
    datasets_root: Path,
    selected: str = "all",
    gate: Gate = Gate.STRICT,
) -> RunReport:
    available = _discover_datasets(datasets_root)
    chosen = _select(selected, available)
    report = RunReport(selected_datasets=chosen, gate=gate)
    # Inc 0 has no execution path; Inc 8 wires per-dataset adapters.
    # Empty selection under strict mode is allowed (no datasets ⇒ no failures).
    return report
