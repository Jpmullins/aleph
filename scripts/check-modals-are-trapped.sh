#!/usr/bin/env bash
# Every dialog in the web app goes through `Modal.tsx`.
#
# WS-B1 criterion 4 is "Escape closes **every** modal and focus is trapped
# inside it". It shipped with three modals converted and ONE of them tested:
# reverting `ProjectCreateModal` to its original bare `<div class="fixed
# inset-0">` — no Escape, no trap, no `role="dialog"` — left lint, 181 vitest
# tests and 23 Playwright tests all green. Two of the three could silently lose
# the behaviour the criterion names.
#
# Three browser tests per modal is the obvious answer and the wrong one: it is
# slow, it tests the three that exist, and the fourth modal somebody adds next
# month is not covered by any of them. The invariant is structural — a dialog
# that is not a `Modal` cannot have the behaviour, whoever wrote it — so this
# checks the structure and the browser test proves the behaviour once.
#
# What counts as a dialog: `role="dialog"`, `aria-modal`, or a full-viewport
# `fixed inset-0` overlay. Those are the three ways one has been written in this
# repository, including the four that predated `Modal.tsx`.
#
# Exit 0 pass · 1 a dialog outside Modal.tsx.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - <<'PY'
import pathlib
import re
import sys

SRC = pathlib.Path("apps/web/src")
MODAL = SRC / "components" / "Modal.tsx"

if not MODAL.exists():
    print(f"✗ {MODAL} does not exist — this check has no subject", file=sys.stderr)
    raise SystemExit(1)

#: The three shapes a dialog has taken here. `fixed inset-0` is the one that
#: matters most: the four pre-WS-B1 dialogs were all bare overlays and only two
#: of them even claimed `role="dialog"`.
DIALOG = re.compile(
    r"""role\s*=\s*["']dialog["']|aria-modal|fixed\s+inset-0""",
    re.I,
)

#: A comment line. `HypothesesSurface` and `SettingsSurface` both DESCRIBE the
#: overlay they used to be, and a check that cannot tell prose from code would
#: force those explanations to be deleted — which is how the reason a rule
#: exists gets lost.
COMMENT = re.compile(r"^\s*(?://|/\*|\*)")

problems: list[str] = []
scanned = 0
for path in sorted(SRC.rglob("*.tsx")):
    if path == MODAL or path.name.endswith(".test.tsx"):
        continue
    scanned += 1
    text = path.read_text(encoding="utf-8")
    uses_modal = "from \"@/components/Modal\"" in text or "from \"./Modal\"" in text
    for number, line in enumerate(text.splitlines(), 1):
        if COMMENT.match(line) or not DIALOG.search(line):
            continue
        if uses_modal:
            # The file imports Modal AND hand-rolls an overlay. That is the
            # shape where one dialog is trapped and its sibling is not, which
            # is exactly what shipped.
            problems.append(
                f"{path}:{number}: imports Modal and also hand-rolls a "
                f"dialog — {line.strip()[:70]}"
            )
        else:
            problems.append(
                f"{path}:{number}: {line.strip()[:70]}"
            )

if not scanned:
    print("✗ no .tsx files scanned — this check would pass on an empty tree", file=sys.stderr)
    raise SystemExit(1)

if problems:
    print("✗ dialogs that do not go through Modal.tsx:", file=sys.stderr)
    for problem in problems:
        print(f"    {problem}", file=sys.stderr)
    print(
        "\nA dialog outside `Modal` has no Escape, no focus trap and no focus "
        "restore, whatever its markup claims. Wrap it in <Modal>, or if it is "
        "genuinely not a dialog (an inline panel inside a block, say) do not "
        "give it a full-viewport overlay or an aria-modal.",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(f"OK: every dialog across {scanned} .tsx modules goes through Modal.tsx")
PY
