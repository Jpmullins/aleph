#!/usr/bin/env bash
# The kernel imports the standard library and nothing else.
#
# "The core cannot depend on plugins" is only enforceable if the core cannot
# depend on ANYTHING above it — an import added for one convenience is how the
# rule erodes. It imported `aleph-core` for one function (`uuid7`) and
# `aleph-observability` for one (`start_span`), and the second pulled eight
# OpenTelemetry distributions plus an LLM-observability vendor SDK. A loader
# that cannot be imported without a tracing vendor is not a loader.
#
# An AST walk, not a grep: a grep over import LINES misses `importlib` and
# function-local imports, and the kernel's own `manifest.py` resolves factories
# by string on purpose, so the file legitimately contains module paths that are
# not imports.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python - <<'PY'
import ast
import pathlib
import sys

root = pathlib.Path("packages/aleph-kernel/src/aleph_kernel")
offenders: list[str] = []

for path in sorted(root.rglob("*.py")):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` / relative imports have no module and are local.
            if node.level:
                continue
            names = [node.module or ""]
        else:
            continue
        for name in names:
            top = name.split(".", 1)[0]
            if not top or top == "aleph_kernel":
                continue
            if top not in sys.stdlib_module_names:
                offenders.append(f"{path.relative_to(root.parent.parent)}:{node.lineno}: {name}")

if offenders:
    print("✗ the kernel imports something outside the standard library:")
    for line in offenders:
        print(f"    {line}")
    print()
    print("The core must depend on nothing above it. Declare the SHAPE the kernel")
    print("needs and have the composition root supply it — see aleph_kernel.tracing.")
    raise SystemExit(1)

n = len(list(root.rglob("*.py")))
print(f"OK: all {n} kernel modules import stdlib only")
PY
