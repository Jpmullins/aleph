"""CI gate — composes run reports into a single pass/fail decision.

Used by `.github/workflows/ci.yml` and `eval-nightly.yml`. The gate:

  * permission-leakage findings are always blocking
  * `gate_kind=blocking` dataset failures are blocking under STRICT
  * `gate_kind=warning` → exit 0 with a printed warning
  * `gate_kind=metric_only` → exit 0; metrics still surface in the report
"""

from __future__ import annotations

import json
import sys

from aleph_evals.runner import Gate, RunReport


def write_summary(report: RunReport, path: str | None = None) -> int:
    """Write a structured summary to `path` (or stdout). Return exit code."""
    serialized = {
        "selected_datasets": report.selected_datasets,
        "gate": report.gate.value,
        "any_failures": report.any_failures,
        "results": [r.model_dump() for r in report.results],
    }
    if path:
        from pathlib import Path

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(serialized, indent=2))
    else:
        sys.stdout.write(json.dumps(serialized, indent=2) + "\n")
    code = report.exit_code()
    if code != 0:
        # Two different failures, two different sentences. "0 failures across 0
        # datasets" was what this printed for an empty selection, which reads
        # as a pass in a CI log and is the opposite of what happened.
        if report.evaluated_nothing:
            sys.stderr.write("GATE FAIL: no datasets were selected, so nothing was evaluated\n")
        else:
            sys.stderr.write(
                f"GATE FAIL: {sum(r.cases_failed + r.cases_errored for r in report.results)} "
                f"failures across {len(report.results)} datasets\n"
            )
    return code


def gate_main(report: RunReport, *, report_path: str | None = None) -> int:
    return write_summary(report, path=report_path)


__all__ = ["Gate", "gate_main", "write_summary"]
