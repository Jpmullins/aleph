#!/usr/bin/env bash
# Every surface prop a Python producer binds must be declared in the client's
# zod schema, or the A2UI binder drops it silently: the payload is correct, the
# view reads `undefined`, and nothing reports an error.
#
# This shipped: the wiki surface sent ten categories and a health summary, the
# data model carried both, and the wiki rendered as though the project had no
# categories. Both halves looked right in isolation.
#
# Two properties this wrapper is responsible for, beyond running the analyzer:
#
#   * a MOVED subject file is a loud failure naming the file, not a traceback
#     from an incidental second read. `run()` used to return `[]` when either
#     subject was missing and the nonzero exit came from this script re-reading
#     the same path below — so the analyzer was answering "no mismatches" about
#     a file it had never opened;
#   * the success line states COVERAGE, not a bare count. `5 components` reads
#     as "the catalog is clean"; `5 of N catalog components compared` reads as
#     what it is, and counts the ones nobody is checking. Those are the card
#     components: `cards.py` declares no `{"path": ...}` bindings at all, so
#     this sweep has never had anything to compare for them.
#
# CI-wired. Fails on: a bound prop the client never declares, or a subject file
# that has moved.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
import pathlib
import sys

sys.path.insert(0, "scripts/_lib")
from surface_bindings import run  # noqa: E402
from sweep_subject import MissingSubject  # noqa: E402

try:
    report = run(pathlib.Path(".").resolve())
except MissingSubject as exc:
    print(f"✗ surface bindings: {exc}", file=sys.stderr)
    print(
        "   This sweep checks nothing without it. Move the sweep with the code, "
        "in the same change — a gate pointed at a path that no longer exists is "
        "a gate that passes forever.",
        file=sys.stderr,
    )
    raise SystemExit(1) from None

if report.mismatches:
    print(
        "✗ surface bindings: producer props the client does not declare, or "
        "declares unresolvably",
        file=sys.stderr,
    )
    for mismatch in report.mismatches:
        print(f"   {mismatch}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "   Declare the prop in its `*Api.schema` in "
        "apps/web/src/a2ui/aleph-catalog-v09.tsx, as `CommonSchemas.Dynamic*` "
        "or `CommonSchemas.Action`. A `z3.*` declaration is a LITERAL the "
        "binder passes through untouched — correct for a Vega-Lite spec, "
        "fatal for a prop the producer sends as {path: ...}.",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(
    f"✓ surface bindings: {len(report.compared)} of {report.catalog_total} catalog "
    f"components compared, {report.bound_props} bound props, all declared "
    "client-side AND resolvable by the binder"
)
if report.uncompared:
    # Stated every run, on purpose. The uncovered components are the ones bound
    # from `cards.py`, which declares no `{"path": ...}` bindings at all, so this
    # sweep has never had anything to compare for them. Printing the number is
    # the difference between a coverage gap somebody can act on and a completeness
    # claim nobody questions.
    print(
        f"  {report.uncompared} catalog component(s) have no path-binding producer and "
        f"are NOT checked by this sweep (see packages/aleph-a2ui/.../components/cards.py)"
    )
PY
