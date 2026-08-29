"""Every project-scoped worker job refuses a deleted project.

The rule exists because the absence of it cost $141.43 in one hour. See
`aleph_workers.project_guard` for the measurement and the mechanism.

A grep would not do. The guard is a CALL, and a job that imports
`refuse_if_project_is_gone` and never calls it — or calls it after the model
calls it was supposed to precede — satisfies a text search completely. So this
walks the AST of each job entry point and asks two things:

  1. Does it call the guard at all?
  2. Is the call in the function's PROLOGUE — before any `await` that could
     spend money? A guard placed after the work has already happened is a
     comment.

"Project-scoped" is decided by the signature, not by a list kept here: a job
whose parameters mention a project, or that takes an `agent_token` (which
carries a signed `project_id` and is how the indirect jobs learn their scope),
must guard. A list would go stale the first time somebody added a job, which is
the failure mode this file exists to prevent.
"""

from __future__ import annotations

import ast
import pathlib

GUARD = "refuse_if_project_is_gone"

#: Jobs with no project to check. Each needs a REASON, so that exempting a new
#: job is a decision somebody writes down rather than a line somebody adds.
EXEMPT: dict[str, str] = {
    "smoke_llm_job": "pings the gateway; touches no project row",
}


def _is_project_scoped(fn: ast.AsyncFunctionDef) -> bool:
    names = {a.arg for a in fn.args.args}
    if any("project" in n for n in names):
        return True
    # The indirect shape: scope arrives inside the signed token.
    return "agent_token" in names


def _guard_calls(fn: ast.AsyncFunctionDef) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == GUARD
    ]


def _first_guard_line(fn: ast.AsyncFunctionDef) -> int | None:
    calls = _guard_calls(fn)
    return min((c.lineno for c in calls), default=None)


def _spending_awaits(fn: ast.AsyncFunctionDef) -> list[int]:
    """Lines of `await`s that are not the guard itself and not trivially safe.

    Deliberately crude and deliberately inclusive: anything awaited before the
    guard is a candidate for work done on a dead project. Being wrong in the
    strict direction costs a comment; being wrong the other way costs money.
    """
    handled: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.ExceptHandler):
            handled.update(n.lineno for n in ast.walk(node) if isinstance(n, ast.Await))

    out: list[int] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == GUARD:
            continue
        # An `await` inside an `except` reports a failure that already
        # happened — converging a run whose token would not verify, say. It is
        # not work done on the project's behalf, and requiring the guard to
        # precede it would mean checking a project id the job has not managed
        # to read yet.
        if node.lineno in handled:
            continue
        out.append(node.lineno)
    return out


def violations(root: pathlib.Path) -> list[str]:
    problems: list[str] = []
    jobs_dir = root / "apps/workers/src/aleph_workers/jobs"
    for path in sorted(jobs_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in tree.body:
            if not isinstance(fn, ast.AsyncFunctionDef) or not fn.name.endswith("_job"):
                continue
            rel = f"{path.name}::{fn.name}"
            if fn.name in EXEMPT:
                continue
            if not _is_project_scoped(fn):
                continue
            guard_line = _first_guard_line(fn)
            if guard_line is None:
                problems.append(
                    f"{rel}: project-scoped and never calls {GUARD}(). A deleted "
                    f"project's queued work runs anyway, and the wiki chain "
                    f"enqueues more as it goes."
                )
                continue
            earlier = [ln for ln in _spending_awaits(fn) if ln < guard_line]
            if earlier:
                problems.append(
                    f"{rel}: calls {GUARD}() at line {guard_line}, but awaits "
                    f"something first at line {min(earlier)}. The guard has to "
                    f"come before the work, or it only reports the spend."
                )
    return problems
