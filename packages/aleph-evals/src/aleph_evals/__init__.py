"""Aleph eval runner.

Inc 0 ships the runner skeleton: dataset discovery + CLI. No datasets
exist yet — Inc 8 introduces them, plus the AIQ benchmark adapters.
Until then, the runner exits 0 when given `--gate strict` with no
datasets present.
"""

from aleph_evals.runner import Gate, RunReport, RunResult, run

__all__ = ["Gate", "RunReport", "RunResult", "run"]
