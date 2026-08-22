#!/usr/bin/env bash
# One prop contract, three copies, four ways for them to disagree in silence.
#
# The copies are the Python PRODUCER that sends the prop, `catalog.json` (which
# the server validates against, and which tells the agent what it may set), and
# the client zod schema the A2UI binder resolves against. The binder resolves
# ONLY what the zod schema names, so a prop missing from that copy is discarded:
# the payload is correct, the view reads `undefined`, nothing raises.
#
# All four directions have failed here:
#
#   * a bound prop the client never declares — the wiki surface sent ten
#     categories and a health summary and rendered as though the project had
#     none;
#   * a bound prop the client declares as a `z3.*` literal, which the binder
#     passes through VERBATIM — `runs.map` on `{path: "/runs"}` threw and React
#     unmounted the pane;
#   * a LITERAL prop no schema names — `ApprovalCard.diff_card_id`,
#     `ChartCard._placeholder`, `WikiPageCard.dossier_refs`. This one was
#     invisible to the sweep for its whole life, because it only read
#     `{"path": ...}` bindings and every card sends plain values;
#   * `catalog.json` and the renderer disagreeing — nine props declared in the
#     catalog the renderer had never heard of, fourteen the renderer resolves
#     that the catalog did not mention, and `WikiSurface.view_mode` REQUIRED and
#     never sent by anybody.
#
# And one direction over the other half of the same contract, the ACTIONS: three
# of twenty-one verbs the ActionRouter would dispatch had no emitter anywhere in
# the product. `clarify` was an echo that wrote nothing; `mark_handedit` and
# `clear_handedit` were a second, ledger-poorer copy of `routes/handedits.py`.
#
# Two properties this wrapper is responsible for, beyond running the analyzer:
#
#   * a MOVED subject file is a loud failure naming the file, not a traceback
#     from an incidental second read. `run()` used to return `[]` when either
#     subject was missing and the nonzero exit came from this script re-reading
#     the same path below — so the analyzer was answering "no mismatches" about
#     a file it had never opened;
#   * the success line states COVERAGE, not a bare count. `5 components` reads
#     as "the catalog is clean"; `18 of N catalog components compared` reads as
#     what it is, and names what is still uncovered.
#
# CI-wired. Fails on: a prop no client schema declares, a bound prop declared
# unresolvably, catalog/renderer drift, an agent-offered prop that reaches no
# view, a registered action nothing can send, or a subject file that has moved.
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
        "   An `actions.<kind>` row is different: register it only when "
        "something can send it, or delete the registration and its catalog "
        "entry. A verb nothing dispatches reads as capability and is not.",
        file=sys.stderr,
    )
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
    f"components compared, {report.bound_props} props, all declared client-side, "
    "resolvable by the binder, and matching catalog.json in both directions; "
    f"{report.actions_total} action kinds, every one with an emitter"
)
if report.uncompared:
    # Stated every run, on purpose — the difference between a coverage gap
    # somebody can act on and a completeness claim nobody questions. What is
    # left are the components no Python producer emits: they are built in the
    # browser, or by a worker, or only ever by the agent.
    print(
        f"  {report.uncompared} catalog component(s) are emitted by no Python producer "
        "in the subject list and are NOT compared"
    )
if report.unknown_to_client:
    # The basic-catalog primitives (`Text`, `Button`, `TextField`, …) come from
    # @a2ui/react's own catalog, not from Aleph's zod file, so their props
    # cannot be checked here. Named rather than silently skipped.
    print(
        f"  {len(report.unknown_to_client)} emitted component(s) are upstream "
        f"primitives with no Aleph zod schema: {', '.join(report.unknown_to_client)}"
    )
PY
