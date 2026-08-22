#!/usr/bin/env bash
#
# A route whose URL names a project must resolve that project's scope.
#
# Per-project scoping is the whole tenant boundary. A handler that accepts
# `{project_id}` and never runs `project_scope_dep` reads and writes another
# tenant's rows for anyone who can guess a UUID, and answers 200 while doing it.
# There is no type, no test and no code review sweep that catches the omission,
# because the omission LOOKS like the normal case: `project_id: UUID` is a
# perfectly ordinary FastAPI path parameter, and it is one character different
# from the safe spelling.
#
# CLAUDE.md files this under "Rules that are real but only held by review", and
# the F1 defect — the agent endpoint taking its project scope from a
# client-supplied thread id — is the same failure one layer up. This is the
# routing half moved out of review.
#
# Accepted as scoping, all three genuine forms:
#   * `project_id: ProjectScopeDep` (or Annotated[..., Depends(project_scope_dep)]);
#   * `dependencies=[Depends(project_scope_dep)]` on the decorator or the router;
#   * `await assert_stream_access(request, project_id, principal)` — SSE routes
#     cannot take the dependency, because it pins a pool connection for the life
#     of a request that never ends.
#
# Static: no database, no gateway, no running server. The analyzer is
# `scripts/_lib/project_scope.py`, imported by this sweep and by
# `tests/unit/test_project_scope_sweep.py` — one implementation, two callers, so
# the gate does not depend on the test suite existing.
#
# Fails on: a project-scoped route handler with no scope resolution.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import pathlib
import sys

sys.path.insert(0, "scripts/_lib")
from project_scope import scan  # noqa: E402
from sweep_subject import MissingSubject  # noqa: E402

try:
    report = scan(pathlib.Path(".").resolve())
except MissingSubject as exc:
    print(f"✗ project scope: {exc}", file=sys.stderr)
    print(
        "   A sweep that parses nothing reports everything clean. Point it at the "
        "routes or delete it; do not leave it green.",
        file=sys.stderr,
    )
    raise SystemExit(1) from None

if report.offenders:
    print("✗ project scope: route handlers that name a project and never scope it", file=sys.stderr)
    for route in report.offenders:
        print(f"   {route}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "   Take `project_id: ProjectScopeDep` (aleph_api.middleware.project_scope) "
        "instead of `project_id: UUID`. It resolves membership, refuses a "
        "credential scoped to another project, and 404s rather than leaking "
        "existence across tenants.",
        file=sys.stderr,
    )
    print(
        "   An SSE route uses `await assert_stream_access(request, project_id, "
        "principal)` instead — ProjectScopeDep pins a pool connection for the "
        "life of a request that never ends.",
        file=sys.stderr,
    )
    raise SystemExit(1)

mechanisms = ", ".join(f"{count} by {name}" for name, count in sorted(report.by_mechanism.items()))
print(f"✓ project scope: {len(report.routes)} project-scoped routes, all scoped ({mechanisms})")

# Coverage, stated rather than implied — same rule as check-surface-bindings.
# An exemption that nobody counts is an exemption that grows.
if report.allowlisted:
    print(f"  {len(report.allowlisted)} allowlisted exemption(s):")
    for route in report.allowlisted:
        print(f"    {route}")
# This sweep checks that the scope is RESOLVED, not which role it requires.
# Saying so is the difference between a coverage number and a completeness claim.
print("  role requirements (require_at_least) are NOT checked by this sweep")
PY
