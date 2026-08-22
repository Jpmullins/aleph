"""Expanded eval runner (Inc 8).

Discovers eval datasets from the filesystem at
`packages/aleph-evals/datasets/<inc>_<area>/<name>.jsonl` (plus a
`manifest.yaml`). Loads cases, scores them via the registered scorer
for the dataset's `kind`, writes `EvalRun` + `EvalResult` rows, and
returns a `RunReport` that the CI gate consumes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
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
    metrics: dict[str, float] = {}
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


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    kind: str
    fixture_path: Path
    gate_kind: str  # blocking | warning | metric_only
    gate_thresholds: dict[str, Any]
    introduced_in_increment: int


def _discover_specs(root: Path) -> list[DatasetSpec]:
    out: list[DatasetSpec] = []
    if not root.exists():
        return out
    for inc_dir in sorted(root.iterdir()):
        if not inc_dir.is_dir():
            continue
        manifest_path = inc_dir / "manifest.yaml"
        if not manifest_path.exists():
            continue
        try:
            manifest = yaml.safe_load(manifest_path.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(manifest, dict):
            continue
        for d in manifest.get("datasets", []) or []:
            if not isinstance(d, dict):
                continue
            jsonl = inc_dir / d["file"]
            if not jsonl.exists():
                continue
            out.append(
                DatasetSpec(
                    name=d["name"],
                    kind=d.get("kind", "metric_only"),
                    fixture_path=jsonl,
                    gate_kind=d.get("gate_kind", "metric_only"),
                    gate_thresholds=d.get("gate_thresholds", {}) or {},
                    introduced_in_increment=int(
                        d.get(
                            "introduced_in_increment",
                            int(inc_dir.name.split("_", 1)[0].lstrip("inc"))
                            if inc_dir.name.startswith("inc")
                            else 0,
                        )
                    ),
                )
            )
    return out


def _select(selected: str, available: list[DatasetSpec]) -> list[DatasetSpec]:
    if selected == "all":
        return available
    if selected.startswith("@"):
        tag = selected[1:]
        return [s for s in available if tag in s.name or tag == s.kind]
    wanted = {s.strip() for s in selected.split(",") if s.strip()}
    return [s for s in available if s.name in wanted]


def _load_cases(fixture: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for raw in fixture.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return cases


def _run_dataset(
    spec: DatasetSpec,
    *,
    profile_name: str,
) -> RunResult:
    from aleph_evals.scorers import SelfGradingFixture, score

    cases = _load_cases(spec.fixture_path)
    passed = 0
    failed = 0
    errored = 0
    score_total = 0.0
    for case in cases:
        try:
            # No `actual=`, because there is no executor for this dataset kind.
            #
            # That is the honest state and it is now visible. This loop used to
            # score a fixture that carried its own `actual`, so `pass_rate: 1.0`
            # meant "the JSON file agrees with itself" — nothing was run, and
            # the number could not fail for any reason connected to the code.
            # `score()` refuses such a case, so those datasets now ERROR rather
            # than pass, and the report says which.
            ok, sc = score(spec.kind, case, profile_name=profile_name)
            if ok:
                passed += 1
            else:
                failed += 1
            if sc is not None:
                score_total += sc
        except SelfGradingFixture:
            errored += 1
        except Exception:
            errored += 1
    metrics: dict[str, float] = {
        "pass_rate": (passed / len(cases)) if cases else 1.0,
        "fail_rate": (failed / len(cases)) if cases else 0.0,
        "error_rate": (errored / len(cases)) if cases else 0.0,
    }
    if cases and score_total:
        metrics["mean_score"] = score_total / len(cases)
    return RunResult(
        dataset=spec.name,
        cases_total=len(cases),
        cases_passed=passed,
        cases_failed=failed,
        cases_errored=errored,
        metrics=metrics,
    )


def _apply_gate(spec: DatasetSpec, result: RunResult, profile_name: str) -> RunResult:
    if spec.gate_kind != "blocking":
        return result
    thresholds = spec.gate_thresholds.get(profile_name) or spec.gate_thresholds.get("default", {})
    min_pass = float(thresholds.get("min_pass_rate", 1.0))
    if result.metrics.get("pass_rate", 0.0) < min_pass:
        # Convert remaining "passed" into fails so the gate trips.
        notes = (
            f"gate trip: pass_rate {result.metrics.get('pass_rate', 0):.2f} "
            f"< threshold {min_pass:.2f} for profile {profile_name}"
        )
        return RunResult(
            dataset=result.dataset,
            cases_total=result.cases_total,
            cases_passed=result.cases_passed,
            cases_failed=max(result.cases_failed, 1),
            cases_errored=result.cases_errored,
            metrics=result.metrics,
            notes=notes,
        )
    return result


def run(
    *,
    datasets_root: Path,
    selected: str = "all",
    gate: Gate = Gate.STRICT,
    profile_name: str = "aleph-dev",
) -> RunReport:
    specs = _discover_specs(datasets_root)
    chosen = _select(selected, specs)
    report = RunReport(selected_datasets=[s.name for s in chosen], gate=gate)
    for spec in chosen:
        raw = _run_dataset(spec, profile_name=profile_name)
        report.results.append(_apply_gate(spec, raw, profile_name))
    return report
