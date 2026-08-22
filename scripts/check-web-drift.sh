#!/usr/bin/env bash
#
# The interface has a written specification and most of the app predates it.
#
# `apps/web/src/styles/tokens.css` sets `--radius: 0px`, hairline borders, no
# shadows, and colour reserved for state. Components still carry `rounded-lg`,
# `shadow-md` and Tailwind's default palette. The palette one is not cosmetic:
# `text-slate-500` is a fixed colour with no theme behind it, so it renders
# identically on both grounds — which is why parts of this app look right in one
# theme and wrong in the other, and why no screenshot of a single theme can find
# it.
#
# Nothing counts these today, so the number moves in whichever direction the
# last person happened to push it. This pins each count. WS-UI-G1 drives them to
# zero; until then the only rule is that they may not grow.
#
# The pin is two-sided on purpose. "It may only fall" is a claim, and a pin that
# is allowed to sit above the real number does not enforce it — after G1 takes
# `rounded-*` to zero, a pin still reading 56 would silently permit all 56 back.
# So a count that has FALLEN is also an error, with the new block printed ready
# to paste. Lowering the pin is part of the change that earned it.
#
# Usage:
#   check-web-drift.sh [--ratchet]   compare against the pin (default)
#   check-web-drift.sh --report      print the counts and exit 0
#
# CI-wired. Fails on: any counter above or below its pinned value.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="${1:---ratchet}"
case "$MODE" in
  --ratchet|--report) ;;
  *) echo "usage: $(basename "$0") [--ratchet|--report]" >&2; exit 2 ;;
esac

MODE="$MODE" python3 - <<'PY'
import os
import pathlib
import re
import sys
from collections import Counter

MODE = os.environ["MODE"]
SRC = pathlib.Path("apps/web/src")

# ---------------------------------------------------------------------------
# THE PIN. Measured on this tree; lower it in the change that lowers the count.
#
#   2026-08-21  WS-UI-1  first pin, taken after four unreachable modules
#                        (A2UISurfaceView, ActivityCard, ReadingRegion,
#                        A2UIRightPanel) and four orphan icons were deleted.
#                        Baseline before that deletion was 56 / 9 / 122 / 50 /
#                        25 / 27 = 289.
# ---------------------------------------------------------------------------
PIN = {
    "rounded-{sm..full}": 53,
    "rounded (bare)": 48,
    "shadow-{sm..2xl}": 9,
    # 108 -> 96: GroundingSurface's confidence badges moved from emerald/amber/
    # rose scales onto the semantic --badge-* tokens tokens.css already defines
    # for light and dark. A raw `emerald-100` has no theme behind it and reads
    # as a bright chip on a dark background.
    "palette-scale colour": 96,
    "var(--token, LITERAL)": 25,
    "raw hex / rgba()": 27,
}

PALETTE_PROPS = (
    "bg|text|border|ring|from|to|via|fill|stroke|divide|outline|decoration|"
    "shadow|accent|caret|placeholder"
)
PALETTE_HUES = (
    "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|"
    "teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose"
)

# Each counter says, in its name, what a reader should write instead.
COUNTERS: dict[str, tuple[re.Pattern[str], str]] = {
    "rounded-{sm..full}": (
        re.compile(r"\brounded-(?:sm|md|lg|xl|2xl|3xl|full)\b"),
        "tokens.css sets --radius: 0px; Aleph is squared",
    ),
    # Tailwind's bare `rounded` is 0.25rem, which is still not zero. The
    # backlog's counting rules excluded it and it is 50 real violations.
    "rounded (bare)": (
        re.compile(r"\brounded\b(?!-)"),
        "bare `rounded` is 0.25rem, not --radius: 0px",
    ),
    "shadow-{sm..2xl}": (
        re.compile(r"\bshadow-(?:sm|md|lg|xl|2xl)\b"),
        "the spec has no drop shadows; use a hairline border",
    ),
    "palette-scale colour": (
        re.compile(rf"\b(?:{PALETTE_PROPS})-(?:{PALETTE_HUES})-(?:50|[1-9]00|950)\b"),
        "a fixed colour has no theme behind it; use a semantic token",
    ),
    # `var(--x, #fef3c7)` looks token-clean and is not: the literal is what
    # renders whenever the token is absent, in whichever theme that happens.
    "var(--token, LITERAL)": (
        re.compile(r"var\(--[a-z0-9-]+,\s*[^)]+\)"),
        "a fallback literal is a hardcoded colour that only shows up sometimes",
    ),
    "raw hex / rgba()": (
        re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([0-9]"),
        "raw colour in .tsx; move it to tokens.css",
    ),
}

files = sorted(p for p in SRC.rglob("*") if p.suffix in (".ts", ".tsx"))
if not files:
    print(f"✗ no .ts/.tsx under {SRC} — this pin would pass on an empty tree", file=sys.stderr)
    raise SystemExit(1)

counts: dict[str, int] = {}
tokens: dict[str, Counter[str]] = {}
where: dict[str, Counter[str]] = {}
for name, (pattern, _) in COUNTERS.items():
    tok: Counter[str] = Counter()
    loc: Counter[str] = Counter()
    for path in files:
        hits = pattern.findall(path.read_text())
        if hits:
            tok.update(hits)
            loc[str(path)] += len(hits)
    counts[name] = sum(tok.values())
    tokens[name] = tok
    where[name] = loc

width = max(len(n) for n in COUNTERS)

if MODE == "--report":
    for name in COUNTERS:
        print(f"  {name:<{width}}  {counts[name]:>4}")
    print(f"  {'TOTAL':<{width}}  {sum(counts.values()):>4}  across {len(files)} modules")
    raise SystemExit(0)

missing = set(COUNTERS) - set(PIN)
if missing:
    print(f"✗ counters with no pinned value: {', '.join(sorted(missing))}", file=sys.stderr)
    raise SystemExit(1)

grew = {n: (counts[n], PIN[n]) for n in COUNTERS if counts[n] > PIN[n]}
fell = {n: (counts[n], PIN[n]) for n in COUNTERS if counts[n] < PIN[n]}

if grew:
    print("✗ design-token drift grew:", file=sys.stderr)
    for name, (now, pin) in grew.items():
        print(f"    {name}: {pin} → {now}  (+{now - pin}) — {COUNTERS[name][1]}", file=sys.stderr)
        # Name the literal classes, so "which rounded?" is answered here rather
        # than by re-grepping. This is the line the mutation test reads.
        hist = ", ".join(f"{t}×{c}" for t, c in tokens[name].most_common(12))
        print(f"      matched: {hist}", file=sys.stderr)
        top = ", ".join(f"{pathlib.Path(f).name}×{c}" for f, c in where[name].most_common(4))
        print(f"      worst files: {top}", file=sys.stderr)
    print(
        "\n  Use a semantic token from apps/web/src/styles/tokens.css. If the "
        "growth is genuinely intended, raise the pin in scripts/check-web-drift.sh "
        "and say why in the same change — but WS-UI-G1 is driving these to zero.",
        file=sys.stderr,
    )

if fell:
    print("✗ the pin is above the real count, so it is not holding anything:", file=sys.stderr)
    for name, (now, pin) in fell.items():
        print(f"    {name}: pinned at {pin}, actually {now}  ({now - pin})", file=sys.stderr)
    print("\n  Paste this into PIN in scripts/check-web-drift.sh:\n", file=sys.stderr)
    print("PIN = {", file=sys.stderr)
    for name in COUNTERS:
        print(f'    "{name}": {counts[name]},', file=sys.stderr)
    print("}", file=sys.stderr)

if grew or fell:
    raise SystemExit(1)

print(
    f"OK: design-token drift pinned at {sum(counts.values())} across {len(files)} modules — "
    + ", ".join(f"{n} {counts[n]}" for n in COUNTERS)
)
PY
