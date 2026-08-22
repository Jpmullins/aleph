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
# Nothing counted these, so the number moved in whichever direction the last
# person happened to push it. This pins each count.
#
# WS-G drove all six to zero on 2026-08-22, so the pin below is all zeros and
# this sweep is now a hard gate rather than a ratchet: one reintroduced
# `rounded-lg`, one `text-slate-500`, one `var(--accent, #f97316)` fails the
# build and is named with its file. There is no --zero mode because a pin of
# zero IS zero mode — a second flag would be a second source of truth about the
# same number.
#
# The pin stays two-sided. "It may only fall" is a claim, and a pin allowed to
# sit above the real number does not enforce it — a pin still reading 53 after
# `rounded-*` reached zero would silently permit all 53 back. So a count that
# has FALLEN is also an error, with the new block printed ready to paste.
# Lowering the pin is part of the change that earned it.
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
from typing import Any

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
#   2026-08-22  WS-G     256 -> 0. What each category became:
#
#                        rounded (99: 53 sized + 46 bare) — deleted, not
#                          swapped. The design is squared: `--radius: 0px` in
#                          tokens.css and `--radius-none` in styles.css, so
#                          there is no radius token to move to and the property
#                          itself is the violation.
#                        shadow (9) — replaced by a hairline. Three modals and a
#                          drawer used `shadow-xl` for separation, which is
#                          invisible over the near-black ground; they carry
#                          `border-line-strong` now. `Block`'s unselected
#                          `var(--shadow-sm)` became `none` and the three dead
#                          `--shadow-*` tokens were removed from tokens.css.
#                        palette-scale colour (96) — onto the semantic tokens.
#                          Most of it was a status badge: tokens.css already
#                          defined --badge-{idle,running,completed,failed,
#                          warning}-{bg,fg} for BOTH themes and nothing reached
#                          them, because `bg-[var(--badge-failed-bg)]` is longer
#                          to type than `bg-red-50`. styles.css maps all ten
#                          into @theme inline, so the token is now the shorter
#                          thing to write. Filled action buttons went to
#                          bg-good / bg-bad; links and highlight rings to the
#                          single accent.
#                        var(--token, LITERAL) (25) + raw hex (27) — fourteen of
#                          these named the accent with an orange literal after
#                          the comma, an accent from a design Aleph no longer
#                          has, which is what rendered whenever the token was
#                          absent. ChartCard's two axis hexes could not become
#                          classes at all — Vega paints onto a canvas, which has
#                          no cascade — so they resolve through
#                          `lib/theme-tokens.ts` at embed time and re-resolve on
#                          a theme change.
#
#                        The first pass reported 258 and "shadow (7)". Both were
#                          artefacts of the counters, not measurements:
#
#                          Two of the 258 were PROSE — a doc comment and a live
#                          model prompt in `lib/copilot.tsx` using the word
#                          "rounded". The counters read whole files, so English
#                          was indistinguishable from a utility class, and the
#                          only way to reach zero was to reword a string the
#                          model reads. They now read only what a `className=`,
#                          `class=`, `clsx(`, `cn(`, `twMerge(` or `cva(` is
#                          actually given, so prose is out of scope and both
#                          strings were restored to their original wording.
#
#                          The other two were UNDERCOUNTING. `shadow-(sm|md|lg|
#                          xl|2xl)` could not see bare `shadow` or Tailwind v4's
#                          `shadow-xs`/`shadow-2xs`, and `rounded-(sm|md|lg|...)`
#                          could not see `rounded-t-lg`, `rounded-tl-*` or
#                          `rounded-[6px]`. A live drop shadow survived the
#                          sweep on the primary button in App.tsx — on a line
#                          the same change had edited — while the counter read
#                          zero. Both now match the whole family and exempt only
#                          the conformant `-none`.
#
# ---------------------------------------------------------------------------
PIN = {
    "rounded-* (any)": 0,
    "rounded (bare)": 0,
    "shadow-* (any)": 0,
    "palette-scale colour": 0,
    "var(--token, LITERAL)": 0,
    "raw hex / rgba()": 0,
    # 2026-08-22 WS-UI-4 c3/c4. Both were counted by hand in the audit — 2 and
    # 7 — driven to zero, and pinned here so they stay there. Neither is a
    # design-token violation in the strict sense; both are the same FAILURE as
    # one, a class written to express a distinction that the rendered page does
    # not make.
    "identical-arm ternary": 0,
    "inert hover/focus class": 0,
}

PALETTE_PROPS = (
    "bg|text|border|ring|from|to|via|fill|stroke|divide|outline|decoration|"
    "shadow|accent|caret|placeholder"
)
PALETTE_HUES = (
    "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|"
    "teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose"
)

class _InertStateClasses:
    """A `hover:`/`focus:` utility whose bare form is already in the same list.

    Not expressible as one regex: it is a relation between two tokens of the
    SAME class list, and `re` cannot say "this group's value also appears
    elsewhere in the match". The counter loop only needs `.findall(text)`, so
    this satisfies that and does the membership test itself.

    Scoped to class lists (see `CLASS_SCOPED`), which is what makes the
    "same list" part meaningful — two utilities in two different `className`s
    are two different elements.
    """

    _VARIANT = re.compile(r"(?<![\w:-])(hover|focus|active|group-hover|focus-visible):([\w\[\]./%-]+)")

    def findall(self, text: str) -> list[str]:
        out: list[str] = []
        for line in text.splitlines():
            bare = {tok.split(":")[-1] for tok in line.split() if ":" not in tok}
            for match in self._VARIANT.finditer(line):
                if match.group(2) in bare:
                    out.append(match.group(0))
        return out


# Each counter says, in its name, what a reader should write instead.
COUNTERS: dict[str, tuple[Any, str]] = {
    "rounded-* (any)": (
        re.compile(r"(?<![\w-])rounded(?:-(?!none(?![\w-]))[\w\[\]().%/-]+)(?![\w-])"),
        "tokens.css sets --radius: 0px; Aleph is squared",
    ),
    # Tailwind's bare `rounded` is 0.25rem, which is still not zero. The
    # backlog's counting rules excluded it and it is 50 real violations.
    "rounded (bare)": (
        re.compile(r"\brounded\b(?!-)"),
        "bare `rounded` is 0.25rem, not --radius: 0px",
    ),
    "shadow-* (any)": (
        re.compile(r"(?<![\w-])shadow(?:-(?!none(?![\w-]))[\w\[\]().%/-]+)?(?![\w-])"),
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
    # `error ? "building" : "building"` — the reader is told a distinction
    # exists and the page does not make it. Two shipped: the Board's lifecycle,
    # where `connected` decided nothing, and the pipeline strip's stage dot,
    # where a stage every source had reached and one half of them had reached
    # painted identically.
    #
    # A backreference, so it matches only when the arms are byte-identical.
    # `re.S` because the formatter breaks a nested ternary across lines.
    "identical-arm ternary": (
        re.compile(r"\?\s*(\"[^\"\n]*\"|'[^'\n]*')\s*:\s*\1(?![\w-])", re.S),
        "both arms are the same string; the distinction is not rendered",
    ),
    # `className="border border-line-strong … hover:border-line-strong"`. The
    # state variant sets the value the element already has, so the affordance
    # was written, reviewed, shipped, and never appears. Seven of these.
    "inert hover/focus class": (
        _InertStateClasses(),
        "the state variant sets a value the base class already sets",
    ),
}

files = sorted(p for p in SRC.rglob("*") if p.suffix in (".ts", ".tsx"))
if not files:
    print(f"✗ no .ts/.tsx under {SRC} — this pin would pass on an empty tree", file=sys.stderr)
    raise SystemExit(1)

# `rounded` and `shadow` are utility classes only inside a class list. The word
# also occurs in English ("refuses a plugin component that would shadow a core
# one") and as an identifier, and counting those would make the pin unfixable
# for a reason that is not a design violation. So those two counters read only
# class-list strings, while the colour counters stay file-wide on purpose — a
# raw hex in a chart config is a real hardcoded colour wherever it sits.
#
# A class-list string is a quoted or backticked run containing at least one
# token of Tailwind shape (`bg-ink`, `px-6`, `hover:bg-x`). That is what
# separates `"bg-ink px-6 shadow hover:bg-ink-soft"` from `"shadow"` passed as
# a plugin name. A className whose ONLY content is a bare `rounded`/`shadow`
# and nothing else is therefore invisible here; it has never occurred, and the
# built-stylesheet check below is what would catch it.
CLASS_SCOPED = {
    "rounded-* (any)",
    "rounded (bare)",
    "shadow-* (any)",
    "inert hover/focus class",
}

#: Where a class list can actually live. Anything else in a .tsx file is prose,
#: an identifier, or a model prompt.
CLASS_SITE = re.compile(r"(?:className|class)\s*=\s*|(?:clsx|cn|twMerge|cva)\s*\(")
STRING_LITERAL = re.compile(r"\"([^\"\n]*)\"|'([^'\n]*)'|`([^`]*)`", re.S)


def class_lists(text: str) -> str:
    """Every string that a class attribute or class helper actually receives.

    A first attempt guessed instead — "any string literal containing a token of
    Tailwind shape" — and that is not a definition, it is a resemblance.
    `shadcn-flavoured` in a doc comment and `no rounded pills` in a live model
    prompt both resemble one, so the guess kept flagging English while the real
    violation on the primary button went uncounted for a different reason.

    This reads the region after each `className=` / `class=` / `clsx(` / `cn(`
    and takes the string literals inside it, following balanced braces and
    parens so a conditional class expression is covered whole:

        className={clsx("px-2", open && "shadow-lg")}

    Both literals are read. A class name assembled at runtime from fragments
    that are individually not utilities is invisible here; nothing in this tree
    does that, and the built-stylesheet is the backstop if something starts.
    """
    out: list[str] = []
    for site in CLASS_SITE.finditer(text):
        i = site.end()
        while i < len(text) and text[i] in " \t\n":
            i += 1
        if i >= len(text):
            continue
        if text[i] in "\"'`":
            quote = text[i]
            closer = text.find(quote, i + 1)
            region = text[i : closer + 1] if closer != -1 else text[i:]
        else:
            # A braced or parenthesised expression: walk to its matching close
            # so `clsx("a", cond && "b")` is taken as one region.
            opener = "{" if text[i] == "{" else "("
            closer_ch = "}" if opener == "{" else ")"
            depth = 0
            j = i if text[i] in "{(" else site.end() - 1
            start_j = j
            while j < len(text):
                if text[j] == opener:
                    depth += 1
                elif text[j] == closer_ch:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            region = text[start_j : j + 1]
        for match in STRING_LITERAL.finditer(region):
            body = next((g for g in match.groups() if g is not None), "")
            out.append(body)
    return "\n".join(out)


counts: dict[str, int] = {}
tokens: dict[str, Counter[str]] = {}
where: dict[str, Counter[str]] = {}
for name, (pattern, _) in COUNTERS.items():
    tok: Counter[str] = Counter()
    loc: Counter[str] = Counter()
    for path in files:
        text = path.read_text()
        if name in CLASS_SCOPED:
            text = class_lists(text)
        hits = pattern.findall(text)
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
        "\n  Use a semantic token. apps/web/src/styles.css maps every one into "
        "the Tailwind theme, so the class you want already exists: bg-surface / "
        "text-ink / border-line for chrome, text-good / text-bad for state, "
        "bg-badge-<state>-bg + text-badge-<state>-fg for a status chip, "
        "bg-accent / text-accent for the one signal. Corners are squared — "
        "delete the `rounded`, do not swap it. A popover or modal separates with "
        "border-line-strong, not a shadow. This pin is ZERO: raising it needs a "
        "reason written into scripts/check-web-drift.sh in the same change, and "
        "'the design changed' is the only one that qualifies.",
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
