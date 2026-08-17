#!/usr/bin/env bash
#
# LangGraph silently drops writes to state keys a graph's TypedDict does not
# declare. There is no error, no warning, and no failed assertion: the node
# returns `{"resolved_wikilinks": [...]}`, the channel does not exist, the write
# is discarded, and the downstream reader's `state.get("resolved_wikilinks", [])`
# hands back an empty list. Everything reports success and the feature is inert.
#
# Four shipped defects had exactly this shape — synthesis pages with no
# wikilinks, artifacts with empty bibliographies, reviews reporting zero
# findings, and every artifact rebuild overwriting version 1's bytes. Three were
# found by audit; the fourth was found by this sweep.
#
# It parses every module that builds a StateGraph, resolves each state class
# through its inheritance chain, and compares the keys returned by functions
# actually registered via `add_node` against the declared channels. Static, so
# it needs no database, no gateway, and no running graph.
#
# CI-wired. Fails on: a node returning a key its graph does not declare.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv run --quiet python - <<'PY'
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path("tests/unit").resolve()))

# The analyzer lives beside the behavioural tests that prove it models
# LangGraph's real semantics (a node write AND an initial-state key are both
# dropped when undeclared). Importing it keeps one implementation rather than a
# second copy that can disagree with the tests.
from test_graph_state_keys import _graph_modules, _violations  # noqa: E402

modules = _graph_modules()
if not modules:
    print("✗ no graph modules found — the sweep is not looking where the graphs are", file=sys.stderr)
    raise SystemExit(1)

failures = 0
for path in sorted(modules):
    bad = _violations(path)
    if bad:
        failures += 1
        rel = path.relative_to(pathlib.Path.cwd())
        print(f"✗ {rel}: node(s) write undeclared state key(s): {sorted(bad)}", file=sys.stderr)
        print(
            "  LangGraph will discard these writes silently; the reader sees an "
            "empty default and reports success.",
            file=sys.stderr,
        )

if failures:
    raise SystemExit(1)
print(f"✓ graph state keys: {len(modules)} graph modules, every node write declared")
PY
