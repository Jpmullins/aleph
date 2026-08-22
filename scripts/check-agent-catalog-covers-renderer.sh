#!/usr/bin/env bash
#
# The agent may not be shown a shorter list than the browser can draw.
#
# The browser rendered 39 components; the agent was told about 19. The gap was
# not a policy, it was an accident of how `scripts/gen_catalog.py` picked its
# entries — the ten `catalog.json` components that happened to carry an `agent`
# block, plus nine primitives copied by hand. Every input control was on the
# wrong side of it: TextField, CheckBox, ChoicePicker, Slider, DateTimeInput.
# So the assistant could not ask for a form, no plugin could declare a settings
# screen, and nothing anywhere reported a problem — a component the catalog
# omits is not an error, it is a component the model never mentions.
#
# WHAT THIS CHECKS, AND WHY IT IS NOT THE SAME AS check-catalog-generated.sh.
# That one asserts the generated files match their committed inputs. This one
# re-derives the renderable set from the LIVE module graph — it boots Vite,
# imports `apps/web/src/a2ui/aleph-catalog-v09.tsx` and calls the real
# `buildAlephCatalog()` — and compares that against the catalog the agent is
# actually handed. A stale committed extraction passes the first check and
# fails this one, which is the whole point: adding a component to the renderer
# and forgetting to re-extract is exactly how the gap opened.
#
# Three assertions:
#   1. renderable - agent-facing == {} (the criterion).
#   2. the committed extraction matches a live one (nothing has drifted since).
#   3. the five input controls are present by name — the concrete regression,
#      named, so a future refactor that reintroduces a filter says which
#      capability it removed instead of just moving a count.
#
# Needs Node and `pnpm -C apps/web install`. NOT wired into the Python-only
# quality job for that reason; it belongs beside `pnpm -C apps/web build`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

EXTRACT="packages/aleph-a2ui/tools/extract_render_catalog.mjs"
AGENT_CATALOG="apps/copilot-runtime/src/catalog.generated.ts"

if [ ! -d apps/web/node_modules ]; then
  echo "MISSING: apps/web/node_modules — run 'pnpm -C apps/web install'" >&2
  exit 1
fi
for f in "$EXTRACT" "$AGENT_CATALOG"; do
  [ -f "$f" ] || { echo "MISSING: $f" >&2; exit 1; }
done

live="$(mktemp)"
trap 'rm -f "$live"' EXIT
node "$EXTRACT" --print > "$live"

# The comparison is done in Python because the agent catalog is a TypeScript
# module: reading it with a regex would be a second, weaker parser of a file
# whose whole purpose is to be exact. `json.loads` over the object literal
# either parses or fails loudly.
uv run --quiet python - "$live" "$AGENT_CATALOG" <<'PY'
import json
import pathlib
import sys

live_path, agent_path = (pathlib.Path(p) for p in sys.argv[1:3])

live = json.loads(live_path.read_text(encoding="utf-8"))
renderable = set(live["components"])

src = agent_path.read_text(encoding="utf-8")
try:
    body = src[src.index("ALEPH_A2UI_CATALOG = ") + len("ALEPH_A2UI_CATALOG = ") : src.rindex(" as const;")]
    agent = json.loads(body)
except (ValueError, json.JSONDecodeError) as exc:  # pragma: no cover - shape guard
    print(f"FAIL: cannot parse {agent_path} as a JSON object literal: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc

shown = set(agent["components"])
fail = False

missing = sorted(renderable - shown)
if missing:
    print(
        f"FAIL: the browser can draw {len(renderable)} components, the agent is told about "
        f"{len(shown)}; {len(missing)} are renderable and unmentioned:",
        file=sys.stderr,
    )
    for name in missing:
        print(f"  {name}", file=sys.stderr)
    print(
        "  re-extract and regenerate:\n"
        "    node packages/aleph-a2ui/tools/extract_render_catalog.mjs\n"
        "    uv run python scripts/gen_catalog.py",
        file=sys.stderr,
    )
    fail = True

committed = pathlib.Path("packages/aleph-a2ui/src/aleph_a2ui/render_catalog.generated.json")
if committed.read_text(encoding="utf-8") != live_path.read_text(encoding="utf-8"):
    print(
        f"FAIL: {committed} is stale against the renderer — the committed extraction and a "
        "live one differ. Run: node packages/aleph-a2ui/tools/extract_render_catalog.mjs",
        file=sys.stderr,
    )
    fail = True

# Named, not counted. A count going from 19 to 39 says a number changed; this
# says which capability came back.
CONTROLS = {"TextField", "CheckBox", "ChoicePicker", "Slider", "DateTimeInput"}
absent = sorted(CONTROLS - shown)
if absent:
    print(
        "FAIL: the agent catalog offers no way to ask for input — missing "
        + ", ".join(absent)
        + ". A plugin cannot declare a settings screen without these.",
        file=sys.stderr,
    )
    fail = True

if fail:
    raise SystemExit(1)

print(
    f"✓ agent catalog covers the renderer: {len(shown)} components shown, "
    f"{len(renderable)} renderable, every input control present"
)
PY
