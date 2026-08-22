#!/usr/bin/env bash
#
# A ✅ row must name a test that exists and can be collected.
#
# `docs/acceptance.md` is the scoreboard, and it drifted into the exact failure
# it was built to catch: A1 claimed "49 tests" against a suite of 142, D2 claimed
# a test asserts "shows up in the agent's tool set" when the test asserts a key
# is registered in a kernel, and D4 claimed an integration test asserting
# database rows against thirteen pure in-memory unit tests.
#
# This sweep cannot check what a test *asserts* — nothing can, short of reading
# it. What it can check, and what stops the cheapest half of the drift, is that
# every file and every test id a ✅ row names is real and collectable. A row
# citing a node id that pytest cannot find is a row citing nothing.
#
# CI-wired. Fails on: a ✅ row naming a path or a test id that does not resolve.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import pathlib
import re
import subprocess
import sys

DOC = pathlib.Path("docs/acceptance.md")
if not DOC.is_file():
    print(f"✗ {DOC} does not exist — the scoreboard is the subject of this check", file=sys.stderr)
    raise SystemExit(1)

# Only look inside backticks — prose mentions filenames too, and a sweep that
# reads prose spends its life on false positives. Within a backtick span, find
# path-like tokens anywhere: rows legitimately write
# `pytest packages/aleph-kernel/tests`, not just the bare path.
CODE_SPAN = re.compile(r"`([^`]+)`")
TOKEN = re.compile(
    r"((?:packages|apps|tests|scripts)/[A-Za-z0-9_./-]+(?:::[A-Za-z0-9_]+)?"
    r"|\btest_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)?)"
)

# Row ids whose "check" column names a shell probe rather than a pytest target.
SHELL_ROWS = {
    "A2", "A3", "A5",
    "B5", "B6", "B8", "B9", "B10",
    "E4", "E5",
    "G1", "G2", "G3",
    "H1",
}

rows: list[tuple[str, str]] = []
for line in DOC.read_text().splitlines():
    if not line.startswith("| ") or line.startswith("| #") or line.startswith("|---"):
        continue
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < 4:
        continue
    row_id, _part, check, status = cells[0], cells[1], cells[2], cells[-1]
    if not re.fullmatch(r"[A-Z][0-9]+[a-z]?", row_id):
        continue
    if "✅" not in status:
        continue
    rows.append((row_id, check))

if not rows:
    print("✗ parsed no ✅ rows out of the scoreboard — the parser is broken", file=sys.stderr)
    raise SystemExit(1)

problems: list[str] = []
node_ids: list[tuple[str, str]] = []

for row_id, check in rows:
    tokens = [t for span in CODE_SPAN.findall(check) for t in TOKEN.findall(span)]
    if not tokens and row_id not in SHELL_ROWS:
        problems.append(f"{row_id}: a ✅ row that names no file and no test id — it cites nothing")
        continue
    for token in tokens:
        path_part = token.split("::", 1)[0]
        path = pathlib.Path(path_part)
        if not path.exists():
            # A bare basename: find it.
            matches = [
                p
                for p in pathlib.Path(".").rglob(path_part)
                if "__pycache__" not in p.parts and ".venv" not in p.parts
            ]
            if len(matches) != 1:
                where = "not found" if not matches else f"ambiguous ({len(matches)} matches)"
                problems.append(f"{row_id}: names {token}, {where}")
                continue
            path = matches[0]
        if "::" in token:
            node_ids.append((row_id, f"{path}::{token.split('::', 1)[1]}"))

# Collect every node id in one pytest invocation — one process, not N.
if node_ids:
    result = subprocess.run(  # noqa: S603
        ["uv", "run", "pytest", "--collect-only", "-q", "-p", "no:randomly", *(n for _, n in node_ids)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        blob = result.stdout + result.stderr
        for row_id, node in node_ids:
            name = node.split("::", 1)[1]
            if name not in blob:
                problems.append(f"{row_id}: pytest cannot collect {node}")
        if not any("cannot collect" in p for p in problems):
            problems.append(
                "pytest --collect-only failed over the cited node ids: "
                + blob.strip().splitlines()[-1]
            )

# ---------------------------------------------------------------------------
# docs/plan.md, under a WEAKER rule
# ---------------------------------------------------------------------------
#
# The plan describes work that has not been done, so it legitimately names test
# files that do not exist yet — requiring every citation to resolve would make
# it impossible to write a criterion about future work.
#
# But a node id whose FILE exists and which that file does not contain is a
# different thing: the criterion points at the wrong place, and running it as
# written reports "no tests ran" rather than a pass or a fail. WS-H3 cited
# `tests/integration/test_rubric.py::test_a_configured_rubric_lands_on_state`
# for five of its six criteria; the file exists, the test lives in
# `apps/api/tests/unit/test_rubric_grading.py`, and nothing noticed because
# this sweep read only the scoreboard.
PLAN = pathlib.Path("docs/plan.md")
plan_ids: list[str] = []
if PLAN.is_file():
    for span in CODE_SPAN.findall(PLAN.read_text(encoding="utf-8")):
        for token in TOKEN.findall(span):
            if "::" not in token:
                continue
            path_part, _, name = token.partition("::")
            target = pathlib.Path(path_part)
            # Only when the file is really there. A criterion about a test
            # nobody has written yet is the normal state of a plan.
            if target.is_file() and name not in target.read_text(encoding="utf-8"):
                problems.append(
                    f"docs/plan.md: cites {token}, and {path_part} exists but does "
                    f"not define {name} — the criterion runs as 'no tests ran'"
                )
            plan_ids.append(token)

if problems:
    print("✗ rows citing something that is not there:", file=sys.stderr)
    for problem in problems:
        print(f"    {problem}", file=sys.stderr)
    print(
        f"\n{len(problems)} problem(s). A ✅ against a test that does not exist is the"
        " scoreboard asserting rather than measuring.",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(f"OK: {len(rows)} ✅ rows, {len(node_ids)} cited test id(s), all resolve")
PY
