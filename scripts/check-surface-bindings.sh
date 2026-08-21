#!/usr/bin/env bash
# Every surface prop a Python producer binds must be declared in the client's
# zod schema, or the A2UI binder drops it silently: the payload is correct, the
# view reads `undefined`, and nothing reports an error.
#
# This shipped: the wiki surface sent ten categories and a health summary, the
# data model carried both, and the wiki rendered as though the project had no
# categories. Both halves looked right in isolation.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
import pathlib, sys
sys.path.insert(0, "scripts/_lib")
from surface_bindings import run

root = pathlib.Path(".").resolve()
mismatches = run(root)
if mismatches:
    print("✗ surface bindings: producer props the client never declares")
    for m in mismatches:
        print(f"   {m}")
    print()
    print("   Add the prop to its `*Api.schema` in apps/web/src/a2ui/aleph-catalog-v09.tsx.")
    raise SystemExit(1)

from surface_bindings import client_props, producer_props
prod = producer_props(
    pathlib.Path("packages/aleph-a2ui/src/aleph_a2ui/components/surfaces.py").read_text()
)
client = client_props(pathlib.Path("apps/web/src/a2ui/aleph-catalog-v09.tsx").read_text())
checked = {c: p for c, p in prod.items() if c in client}
print(
    f"✓ surface bindings: {len(checked)} components, "
    f"{sum(len(p) for p in checked.values())} bound props, all declared client-side"
)
PY
