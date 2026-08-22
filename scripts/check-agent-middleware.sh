#!/usr/bin/env bash
#
# A tool that throws must not kill the conversation — everywhere, not once.
#
# `AlephAgentMiddleware` turns a raising tool into a `ToolMessage` the model can
# read and route around. The whole value of that is coverage: the orchestrator
# carrying it while a subagent does not is indistinguishable from not having it,
# because the failure surfaces as "the agent went quiet" either way.
#
# deepagents lets a subagent spec REPLACE the parent's middleware rather than
# extend it (`graph.py`: `spec.get("middleware", ...)`), so "the orchestrator has
# it" is not "the subagents have it". That is the specific thing this asserts.
#
# The browser half matters for the same reason and fails differently: a
# `useFrontendTool` handler that rejects throws into the AG-UI stream, and the
# run dies on the client side where no server-side guard can reach it.
#
# CI-wired. Fails on: a subagent without the guard, the orchestrator without it,
# or an unguarded `await dispatchAction(` in the chat surface.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import ast
import pathlib
import re
import sys

problems: list[str] = []

AGENT = pathlib.Path("apps/api/src/aleph_api/copilot_agent.py")
SUBAGENTS = pathlib.Path("apps/api/src/aleph_api/subagents")
SURFACE = pathlib.Path("apps/web/src/components/CopilotChatSurface.tsx")

# --- 0. the subjects are where this sweep thinks they are --------------------
#
# `AGENT.read_text()` on a moved file raises a bare FileNotFoundError from the
# middle of the sweep: nonzero, so CI is red, but the traceback names a line of
# this script rather than saying "your subject moved" — and the reaction to that
# is to delete the sweep. `SUBAGENTS` is worse: `glob` over a missing directory
# yields nothing, which reaches the "no build_*_subagent functions" branch and
# reads as a parser problem rather than as a moved package.
for subject, why in (
    (AGENT, "it is where create_deep_agent is called; the orchestrator's middleware "
            "list cannot be checked without it"),
    (SUBAGENTS, "it holds every build_*_subagent spec, and deepagents lets a spec "
                "REPLACE the parent's guard rather than extend it"),
):
    if not subject.exists():
        print(f"✗ agent tool guard: {subject} is not there — {why}", file=sys.stderr)
        print(
            "   Move the sweep with the code, in the same change. A gate pointed at "
            "a path that no longer exists is a gate that stops checking.",
            file=sys.stderr,
        )
        raise SystemExit(1)

# --- 1. the orchestrator -----------------------------------------------------
tree = ast.parse(AGENT.read_text())
found_call = False
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name != "create_deep_agent":
        continue
    found_call = True
    middleware = next((kw for kw in node.keywords if kw.arg == "middleware"), None)
    if middleware is None:
        problems.append(f"{AGENT}: create_deep_agent has no middleware= argument")
        continue
    names = {
        n.func.id
        for n in ast.walk(middleware.value)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    if "AlephAgentMiddleware" not in names:
        problems.append(
            f"{AGENT}: the orchestrator's middleware list is {sorted(names)} — "
            "AlephAgentMiddleware is missing, so a raising tool kills the turn"
        )

if not found_call:
    problems.append(f"{AGENT}: no create_deep_agent call found — update this sweep")

# --- 2. every subagent -------------------------------------------------------
builders: list[tuple[pathlib.Path, str]] = []
for path in sorted(SUBAGENTS.glob("*.py")):
    if path.name == "__init__.py":
        continue
    module = ast.parse(path.read_text())
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and re.fullmatch(
            r"build_\w+_subagent", node.name
        ):
            builders.append((path, node.name))

if not builders:
    problems.append(f"{SUBAGENTS}: no build_*_subagent functions found — update this sweep")

# Scoped to the BUILDER's own AST, not to the file's text. The file-level
# search this replaced decided a subagent was guarded if the string
# "AlephAgentMiddleware" appeared anywhere in the module — which the import
# line and the explanatory comment both satisfy. Measured: emptying the
# retriever's middleware list to `"middleware": [],` left this sweep at rc=0
# and `test_agent_tool_guard.py` at 11 passed, with every tool that subagent
# carries one exception away from ending the turn. A sweep that passes on its
# own import statement is the defect it was written to prevent.
for path, builder in builders:
    module = ast.parse(path.read_text())
    fn = next(
        n
        for n in ast.walk(module)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == builder
    )
    # The value bound to a "middleware" key anywhere inside this function.
    value = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, val in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "middleware":
                value = val
    if value is None:
        problems.append(
            f"{path}: {builder} returns a spec with no \"middleware\" key. deepagents "
            "REPLACES the parent's middleware when a spec declares its own and "
            "inherits only when it does not — either way this subagent's tools "
            "are unguarded unless the key is here."
        )
        continue
    constructed = {
        n.func.id
        for n in ast.walk(value)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    if "AlephAgentMiddleware" not in constructed:
        problems.append(
            f"{path}: {builder}'s middleware value constructs {sorted(constructed) or 'nothing'} "
            "— AlephAgentMiddleware is not among them, so it OVERRIDES the parent's "
            "guard rather than adding to it, and a raising tool ends the turn"
        )

# --- 3. the browser's own tool handlers --------------------------------------
if not SURFACE.is_file():
    problems.append(f"{SURFACE} is gone — update this sweep")
else:
    lines = SURFACE.read_text().splitlines()
    for index, line in enumerate(lines):
        if "await dispatchAction(" not in line:
            continue
        # Look back for an enclosing `try {` that has not been closed. Cheap and
        # sufficient: these live in short handler bodies, and the alternative is
        # parsing TSX.
        window = "\n".join(lines[max(0, index - 12) : index])
        if "try {" not in window:
            problems.append(
                f"{SURFACE}:{index + 1}: an unguarded `await dispatchAction(`. A "
                "frontend tool handler that rejects throws into the AG-UI stream "
                "and kills the run where no server-side guard can reach it."
            )

if problems:
    print("✗ agent tool guard:", file=sys.stderr)
    for problem in problems:
        print(f"    {problem}", file=sys.stderr)
    raise SystemExit(1)

print(f"OK: orchestrator + {len(builders)} subagents guarded; chat surface dispatches wrapped")
PY
