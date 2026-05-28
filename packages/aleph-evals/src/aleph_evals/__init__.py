"""Aleph eval suite.

Datasets discovered from
`packages/aleph-evals/datasets/<inc>_<area>/<name>.jsonl` plus a
`manifest.yaml`. Scorers per `kind`. CI gate composes the run report
into a pass/fail exit code.
"""

from aleph_evals.runner import Gate, RunReport, RunResult, run

__all__ = ["Gate", "RunReport", "RunResult", "run"]
