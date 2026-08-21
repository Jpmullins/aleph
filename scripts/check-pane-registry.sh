#!/usr/bin/env bash
#
# The client must not know what surfaces exist.
#
# This check used to compare two hardcoded lists — one in the web app, one in
# `routes/surfaces.py` — and assert they matched. They had drifted (the server
# accepted seven kinds, the client five), so `artifacts` and `grounding` could
# be streamed with nowhere to land and `GroundingSurface` shipped complete on
# every layer with no way to open it.
#
# Making them match was the wrong fix. Wiki/Library/Notes/Hypotheses/Briefs are
# the RESEARCH plugin suite, and a client that knows their names cannot render a
# workbench whose abilities arrive at runtime: install something unrelated and it
# has nowhere to appear, remove the suite and the rail still advertises it.
#
# The list now lives in `aleph_a2ui.pane_registry` and is served by
# `GET /v1/projects/{id}/panes`. This sweep asserts the client did not grow a
# second copy — because the moment it does, the two can disagree again, and the
# failure is silent in both directions.
#
# CI-wired. Fails on: a hardcoded pane-kind list under apps/web/src.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import pathlib
import re
import sys

registry = pathlib.Path("packages/aleph-a2ui/src/aleph_a2ui/pane_registry.py")
if not registry.is_file():
    print(f"✗ missing {registry} — the registry is the source of truth", file=sys.stderr)
    raise SystemExit(1)

ids = set(re.findall(r'PaneKind\(\s*id="([^"]+)"', registry.read_text()))
if not ids:
    print("✗ parsed no pane kinds from the registry", file=sys.stderr)
    raise SystemExit(1)

# Scoped to the files that decide what a person can OPEN. Two other kinds of
# list legitimately name surfaces and are not violations:
#
#   * the A2UI catalog maps component types to React renderers — a client must
#     ship renderers for what it can draw, and a plugin brings its own entries;
#   * the live-signal hook maps a domain to the query keys it invalidates.
#
# Neither decides what is launchable. The rail, the context bar, the agent's
# navigation tool and the workspace state do, and those are checked here.
NAVIGATION = [
    "apps/web/src/components/Rail.tsx",
    "apps/web/src/components/ContextBar.tsx",
    "apps/web/src/components/CopilotChatSurface.tsx",
    "apps/web/src/lib/workspace-ui.tsx",
]
offenders: list[tuple[str, list[str]]] = []
for name in NAVIGATION:
    path = pathlib.Path(name)
    if not path.is_file():
        print(f"✗ {name} is gone — update this sweep's NAVIGATION list", file=sys.stderr)
        raise SystemExit(1)
    text = path.read_text()
    body = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith(("*", "//", "/*"))
    )
    hit = sorted(i for i in ids if re.search(rf'["\']{re.escape(i)}["\']', body))
    if len(hit) >= 2:
        offenders.append((str(path), hit))

# The rail must actually ASK. Without this, deleting the fetch and rendering
# nothing would pass the check above by being trivially empty.
rail = pathlib.Path("apps/web/src/components/Rail.tsx").read_text()
if "usePaneKinds" not in rail:
    print("✗ Rail.tsx does not read the server's pane list", file=sys.stderr)
    raise SystemExit(1)

if offenders:
    print("✗ the client is carrying its own list of pane kinds", file=sys.stderr)
    for path, hit in offenders:
        print(f"    {path}: {', '.join(hit)}", file=sys.stderr)
    print("  render GET /v1/projects/{id}/panes instead — see usePaneKinds()", file=sys.stderr)
    raise SystemExit(1)

print(f"✓ pane kinds: {len(ids)} on the server, none hardcoded in the client")
PY
