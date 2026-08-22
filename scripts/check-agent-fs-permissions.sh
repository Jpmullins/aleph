#!/usr/bin/env bash
#
# The agent may read its standing orders and may not rewrite them.
#
# `create_deep_agent` was called with no `permissions=`, `FilesystemBackend`
# implements `write` and `edit`, and deepagents allows any operation no rule
# matches. So the assistant could silently rewrite the four bundled SKILL.md
# files on the live API container — and text in an ingested web page could in
# principle instruct it to.
#
# This checks the EFFECTIVE decision, not the presence of a keyword argument.
# Matching is first-match-wins, so an `allow` rule placed ahead of the deny
# reopens everything while `grep -c permissions=` stays happily at 1. That
# distinction is the whole reason this is a script and not a grep.
#
# The wider rule it also enforces: no `FilesystemBackend(` may be rooted under
# `apps/`. The underlying mistake was pointing a read-write backend at the
# application's own source tree, and closing one call site does not close the
# next one somebody adds.
#
# CI-wired. Fails on: a writable /skills path, or a filesystem backend rooted in
# the application source tree.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv run --quiet python - <<'PY'
import ast
import pathlib
import sys

from deepagents import FilesystemPermission
from deepagents.middleware.filesystem import _check_fs_permission

AGENT = pathlib.Path("apps/api/src/aleph_api/copilot_agent.py")
if not AGENT.is_file():
    print(f"✗ {AGENT} is gone — update this sweep", file=sys.stderr)
    raise SystemExit(1)

tree = ast.parse(AGENT.read_text())

# --- 1. the effective decision for a write under /skills --------------------
rules = None
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name != "create_deep_agent":
        continue
    for kw in node.keywords:
        if kw.arg == "permissions":
            # WS-H1 moved the rules out of the call into
            # `_agent_filesystem_permissions()`, because the tests needed to
            # read them from more than one file. This used to `eval` the
            # literal and died with `NameError` the moment it became a call —
            # a correct signal, delivered as a traceback.
            #
            # Both halves are checked now, which the literal version could not:
            # the call site passes exactly that function's result, and the
            # rules are what the function returns. Before, the list could have
            # been moved out of the call entirely and this sweep would have
            # gone on evaluating a literal nothing used.
            expression = ast.unparse(kw.value)
            if expression != "_agent_filesystem_permissions()":
                print(
                    "✗ the agent's filesystem rules no longer come from "
                    f"_agent_filesystem_permissions(): {expression}",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            from aleph_api.copilot_agent import _agent_filesystem_permissions

            rules = list(_agent_filesystem_permissions())

if rules is None:
    print(
        "✗ create_deep_agent is called with no permissions= argument.\n"
        "  deepagents allows any operation no rule matches, so the agent can\n"
        "  rewrite its own SKILL.md files on the live container.",
        file=sys.stderr,
    )
    raise SystemExit(1)

problems: list[str] = []
for path in ("/skills/research/SKILL.md", "/skills/anything-at-all/SKILL.md"):
    if _check_fs_permission(rules, "write", path) != "deny":
        problems.append(f"a write to {path} is ALLOWED by the effective rules")

# Reading must still work, or the agent cannot follow its own instructions.
if _check_fs_permission(rules, "read", "/skills/research/SKILL.md") != "allow":
    problems.append("reading /skills is denied — the agent cannot read its own orders")

# The OTHER direction, and it belongs here for the same reason as the deny.
#
# WS-H1 opened exactly one prefix for writing so the agent can author a skill
# for itself. Stating only the deny leaves this sweep green when that allow is
# deleted — the self-improvement loop switches off silently, and the check that
# is supposed to know what the policy IS reports that the policy is intact.
# Order is first-match-wins, so the allow must also still precede the deny; a
# rule list where the deny wins for this path fails right here.
if _check_fs_permission(rules, "write", "/skills/authored/learned-thing/SKILL.md") != "allow":
    problems.append(
        "writes to /skills/authored/** are denied — the agent cannot author a skill "
        "(WS-H1). Either the allow rule is gone, or it now sits AFTER the deny."
    )

# --- 2. no read-write backend rooted in the application source tree ---------
for source in sorted(pathlib.Path("apps").rglob("*.py")):
    if "__pycache__" in source.parts:
        continue
    text = source.read_text()
    if "FilesystemBackend(" not in text:
        continue
    for lineno, line in enumerate(text.splitlines(), 1):
        if "FilesystemBackend(" not in line or line.lstrip().startswith("#"):
            continue
        # `_SKILLS_DIR` is `apps/api/src/aleph_api/skills`, which IS under
        # apps/ — and is exactly the case rule 1 governs. Anything else rooted
        # there is a new instance of the same mistake with no rule covering it.
        if "_SKILLS_DIR" not in line:
            problems.append(
                f"{source}:{lineno}: a FilesystemBackend not rooted at _SKILLS_DIR — "
                "if it points into apps/, the agent can write the application's source"
            )

if problems:
    print("✗ agent filesystem permissions:", file=sys.stderr)
    for problem in problems:
        print(f"    {problem}", file=sys.stderr)
    raise SystemExit(1)

print(f"OK: {len(rules)} rule(s); writes under /skills are denied, reads are not")
PY
