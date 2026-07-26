#!/usr/bin/env bash
#
# WP-4 sub-spec (e) — producer/renderer sweep (Final State item 8).
#
# Every A2UI catalog component must have BOTH:
#   * a RENDERER — a FE view file `apps/web/src/a2ui/components/<Name>.tsx` AND a
#     registered v0_9 impl in `apps/web/src/a2ui/aleph-catalog-v09.tsx`; and
#   * a PRODUCER — a backend emitter (a `surfaces.py`/`cards.py` builder, a worker
#     render job, a route surface-builder) or a documented agent-emit tool
#     (`apps/copilot-runtime/src/server.ts`), verified by the ledgered map below.
#
# It also asserts the two catalog rosters agree (`catalog.py` ⟷ `catalog.ts`) and
# that the DELETED names (MapCard / GraphCard / NotebookCellCard) appear NOWHERE
# in any catalog layer.
#
# CI-wired. Fails on: a renderer with no producer, a producer with no renderer,
# a roster mismatch, or a lingering deleted name.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CATALOG_PY="packages/aleph-a2ui/src/aleph_a2ui/catalog.py"
CATALOG_TS="apps/web/src/a2ui/catalog.ts"
IMPLS_TS="apps/web/src/a2ui/aleph-catalog-v09.tsx"
CTX_TS="apps/web/src/a2ui/surface-context.tsx"
VIEWS_DIR="apps/web/src/a2ui/components"

DELETED=(MapCard GraphCard NotebookCellCard)

# --- Ledgered producer map: name -> "path::grep-pattern" ---------------------
# Each entry names the authoritative producer for that catalog component. The
# sweep verifies the pattern is present in the file (the producer still exists).
# NOTE: a plain newline-delimited list, NOT `declare -A`. macOS ships bash 3.2,
# which has no associative arrays, so the sweep could not run on the primary dev
# platform at all — it only ever ran in CI. Same data, portable form.
PRODUCER_MAP='
WikiSurface=surfaces::def wiki_surface_v09
ArtifactsSurface=surfaces::def artifacts_surface_v09
NotesSurface=surfaces::def notes_surface_v09
HypothesesSurface=surfaces::def hypotheses_surface_v09
BriefsSurface=surfaces::def briefs_surface_v09
ClaimCard=cards::def claim_card
SourceCard=cards::def source_card
ArtifactCard=server.ts::ArtifactCard:
ChartCard=viz_builder::"ChartCard"
ImageCard=render_code::ImageCard
HtmlFrameCard=render_code::HtmlFrameCard
TableCard=cards::def table_card
ApprovalCard=cards::def approval_card
FindingCard=cards::def finding_card
HypothesisCard=cards::def hypothesis_card
FormCard=server.ts::FormCard:
DiffCard=cards::def diff_card
WikiPageCard=a2ui_handlers::"WikiPageCard"
NoteEditorCard=NotesSurface.tsx::NoteEditorCard
HtmlDocCard=WikiPageCard.tsx::HtmlDocCard
'

producer_spec() {
  printf '%s\n' "$PRODUCER_MAP" | grep "^$1=" | head -1 | cut -d= -f2-
}
producer_names() {
  printf '%s\n' "$PRODUCER_MAP" | grep -E '^[A-Za-z]+=' | cut -d= -f1
}

# Resolve a producer key's file path.
producer_file() {
  case "$1" in
    surfaces) echo "packages/aleph-a2ui/src/aleph_a2ui/components/surfaces.py" ;;
    "$CATALOG_PY-surfaces") echo "packages/aleph-a2ui/src/aleph_a2ui/components/surfaces.py" ;;
    cards) echo "packages/aleph-a2ui/src/aleph_a2ui/components/cards.py" ;;
    server.ts) echo "apps/copilot-runtime/src/server.ts" ;;
    viz_builder) echo "apps/api/src/aleph_api/subagents/viz_builder.py" ;;
    render_code) echo "apps/workers/src/aleph_workers/jobs/render_code.py" ;;
    a2ui_handlers) echo "apps/api/src/aleph_api/a2ui_handlers.py" ;;
    NotesSurface.tsx) echo "$VIEWS_DIR/NotesSurface.tsx" ;;
    WikiPageCard.tsx) echo "$VIEWS_DIR/WikiPageCard.tsx" ;;
    *) echo "" ;;
  esac
}

fail=0
err() { echo "✗ $*"; fail=1; }

# --- 1. Extract the two rosters ----------------------------------------------
PY_NAMES=(); while IFS= read -r _l; do PY_NAMES+=("$_l"); done < <(grep -oE '^    "[A-Za-z]+": _comp\(' "$CATALOG_PY" | grep -oE '"[A-Za-z]+"' | tr -d '"' | sort -u)
TS_NAMES=(); while IFS= read -r _l; do TS_NAMES+=("$_l"); done < <(sed -n '/COMPONENT_NAMES = \[/,/\] as const;/p' "$CATALOG_TS" | grep -oE '"[A-Za-z]+"' | tr -d '"' | sort -u)

if [ "${PY_NAMES[*]}" != "${TS_NAMES[*]}" ]; then
  err "catalog.py and catalog.ts rosters differ:"
  diff <(printf '%s\n' "${PY_NAMES[@]}") <(printf '%s\n' "${TS_NAMES[@]}") | sed 's/^/    /' || true
fi

# --- 2. Deleted names appear nowhere in any catalog layer --------------------
for d in "${DELETED[@]}"; do
  hits="$(grep -rnE "\\b$d\\b" "$CATALOG_PY" "$CATALOG_TS" "$IMPLS_TS" "$CTX_TS" "$VIEWS_DIR" \
    "packages/aleph-a2ui/src/aleph_a2ui/components" "apps/copilot-runtime/src/server.ts" \
    2>/dev/null || true)"
  if [ -n "$hits" ]; then
    err "deleted card '$d' still referenced:"
    echo "$hits" | sed 's/^/    /'
  fi
done

# --- 3. Every roster name has a renderer AND a producer ----------------------
for name in "${PY_NAMES[@]}"; do
  # Renderer: FE view file + registered impl.
  [ -f "$VIEWS_DIR/$name.tsx" ] || err "$name: no renderer view file ($VIEWS_DIR/$name.tsx)"
  grep -q "name: \"$name\"" "$IMPLS_TS" || err "$name: no v0_9 impl registered in $IMPLS_TS"

  # Producer: ledgered map entry that still resolves.
  spec="$(producer_spec "$name")"
  if [ -z "$spec" ]; then
    err "$name: no producer entry in the ledgered map (add name→producer)"
    continue
  fi
  key="${spec%%::*}"
  pat="${spec#*::}"
  pfile="$(producer_file "$key")"
  if [ -z "$pfile" ] || [ ! -f "$pfile" ]; then
    err "$name: producer file for key '$key' not found"
  elif ! grep -qF "$pat" "$pfile"; then
    err "$name: producer '$pat' not found in $pfile"
  fi
done

# --- 4. Every producer-map entry names a real roster component ---------------
for name in $(producer_names); do
  found=0
  for n in "${PY_NAMES[@]}"; do [ "$n" = "$name" ] && found=1; done
  [ "$found" -eq 1 ] || err "producer map lists '$name' which is not a catalog component"
done

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "Catalog roster sweep FAILED — every component needs a renderer + a producer,"
  echo "the two rosters must agree, and deleted cards must be gone."
  exit 1
fi

echo "✓ catalog roster: ${#PY_NAMES[@]} components, each with a renderer + producer"
echo "✓ deleted cards absent: ${DELETED[*]}"
