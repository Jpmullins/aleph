"""A run the reaper can never touch is a run that is stuck forever.

`reap_stale_runs` deliberately skips a run with no `started_at`: it cannot tell
one that never began from one that began a second ago, and an over-eager reaper
kills live work. That is the right call — which makes it a hard requirement on
the WRITE side. A job that mints a run `status="running"` and leaves
`started_at` NULL creates a row that is unreapable by design.

Two did. Measured on this instance: 19 curator runs sat at `running` with no
`started_at`, orphaned when their worker was stopped, and no reaper pass would
ever have converged them. `status.sh` number 6 counts stuck runs by
`started_at < now() - interval '1 hour'`, so these were not even visible as
stuck — a NULL start time fails that predicate too.

An AST assertion rather than a runtime one, because the defect is in the shape
of a constructor call at import time, and the alternative is discovering it
from a health number a week later.
"""

from __future__ import annotations

import ast
import pathlib

JOBS = pathlib.Path(__file__).resolve().parents[2] / "apps/workers/src/aleph_workers/jobs"


def test_no_job_mints_a_running_run_the_reaper_cannot_reach() -> None:
    offenders: list[str] = []
    for path in sorted(JOBS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AgentRun"
            ):
                continue
            keywords = {k.arg for k in node.keywords}
            status = next((k.value for k in node.keywords if k.arg == "status"), None)
            if getattr(status, "value", None) == "running" and "started_at" not in keywords:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "these mint a run as `running` with no `started_at`, which "
        f"`reap_stale_runs` will never converge: {offenders}"
    )
