#!/usr/bin/env bash
#
# A CSS class nothing applies is invisible to every gate this repo has.
#
# Tailwind v4 tree-shakes UTILITIES it generates, but a rule written by hand in
# `@layer components` is authored CSS: it is emitted whether or not a single
# element carries the class. `pnpm -C apps/web build` cannot notice, `tsc`
# cannot notice, and eslint never sees the stylesheet at all.
#
# Nine class selectors in `styles.css`'s `@layer components` block were in
# exactly that state — `.prose-chat`, five `.badge-*`, `.badge`, `.card` and
# `.card-hover` — a design system nothing had ever used, sitting next to the
# real one (`_shared.tsx` writes `bg-[var(--badge-warning-bg,…)]` inline). Two
# ways to style a badge, one of them fictional, and no way to tell them apart by
# reading either file.
#
# Scope is every class SELECTOR authored under `apps/web/src/styles/`, not just
# the components layer: a dead rule outside the layer is dead in the same way,
# and scoping to one block would let the next one be written one line lower.
#
# Usage is read from where a class can actually be applied — a `className` /
# `class` attribute, or a `classList.add/remove/toggle` argument. Deliberately
# NOT "the word appears somewhere in the tree": the first version of this sweep
# counted `title="Flag a problem with this card"` as a use of `.card` and
# reported the block as live. When a name is unused as a class but does occur in
# the source, the failure says where, so an ambiguous case is a decision rather
# than a silent pass.
#
# CI-wired. Fails on: a class declared in the app's stylesheets that no element
# is given.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import pathlib
import re
import sys

WEB = pathlib.Path("apps/web")
SRC = WEB / "src"
STYLESHEETS = sorted(SRC.rglob("*.css"))

# Classes that appear in markup this app does not author. Each is a dated
# decision, not a way to make the sweep quiet.
#
#   copilotKitAssistantMessage — emitted by @copilotkit/react-core's own chat
#   DOM. `styles.css` restyles it; nothing in apps/web/src can apply it.
#   dark — set on <html> by index.html's pre-paint script and by ThemeToggle
#   via `classList.toggle`, both of which this sweep reads, but it is also read
#   by Tailwind's own `dark:` variant, so it must never be reported.
THIRD_PARTY = {
    "copilotKitAssistantMessage": "@copilotkit/react-core chat DOM",
    "dark": "the theme flag on <html>; also Tailwind's `dark:` variant",
}

if not STYLESHEETS:
    print(f"✗ no stylesheets found under {SRC}", file=sys.stderr)
    raise SystemExit(1)


def strip_css_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def strip_ts_comments(text: str) -> str:
    """Same scanner as check-web-dead-code.sh: a class named only in a comment
    is not a use, and `"http://…"` must not be mistaken for one."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ('"', "'", "`"):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if text[i] == "\\":
                    out.append("  ")
                    i += 2
                    continue
                out.append(text[i])
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# What is declared.
# ---------------------------------------------------------------------------
declared: dict[str, str] = {}
for sheet in STYLESHEETS:
    body = strip_css_comments(sheet.read_text())
    # Selector text only — everything before the `{` of each rule. Without this
    # a declaration like `--color-canvas: var(--surface-bg)` contributes
    # nothing, but a content string or a url() could.
    for before_brace in re.findall(r"([^{}]*)\{", body):
        # Everything since the last brace, which at the top of a file is the
        # whole run of `@import ...;` statements glued to the first selector.
        # `.zombie` written directly under the imports was skipped for having
        # an `@` in it — a rule the sweep could not see, in the one file it
        # exists to read. At-rule STATEMENTS end in `;`; only what follows the
        # last one is a selector. Block at-rules (`@media`, `@layer`,
        # `@font-face`) carry no `;`, so they still contain their `@` here and
        # are still skipped.
        selector = before_brace.rsplit(";", 1)[-1]
        if "@" in selector:
            continue
        for cls in re.findall(r"\.(-?[A-Za-z_][A-Za-z0-9_-]*)", selector):
            declared.setdefault(cls, str(sheet))

# ---------------------------------------------------------------------------
# What is applied.
#
# `className` is followed either by a string literal or by a braced expression;
# in the braced case every string and template chunk inside it is a class list,
# which is how this app writes conditionals:
#     className={"grid h-10 " + (active ? "bg-accent-muted" : "text-ink-muted")}
# ---------------------------------------------------------------------------
STRINGS = re.compile(r'"([^"\n]*)"|\'([^\'\n]*)\'|`([^`]*)`', re.S)


def class_expressions(text: str) -> list[str]:
    found: list[str] = []
    for m in re.finditer(r'\b(?:className|class)\s*=\s*', text):
        i = m.end()
        while i < len(text) and text[i] in " \n\t":
            i += 1
        if i >= len(text):
            continue
        if text[i] in ('"', "'"):
            quote = text[i]
            j = text.find(quote, i + 1)
            if j != -1:
                found.append(text[i + 1 : j])
            continue
        if text[i] == "{":
            depth, j = 0, i
            while j < len(text):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            found.append(text[i : j + 1])
    return found


applied: set[str] = set()
sources = [p for p in SRC.rglob("*") if p.suffix in (".ts", ".tsx")] + [WEB / "index.html"]
raw_text: dict[str, str] = {}
for path in sources:
    if not path.is_file():
        continue
    text = strip_ts_comments(path.read_text()) if path.suffix != ".html" else path.read_text()
    raw_text[str(path)] = text
    for expr in class_expressions(text):
        for m in STRINGS.finditer(expr):
            chunk = m.group(1) or m.group(2) or m.group(3) or ""
            applied.update(t for t in chunk.split() if t)
        # A bare attribute value (the non-braced case) is itself the list.
        if not expr.startswith("{"):
            applied.update(t for t in expr.split() if t)
    for m in re.finditer(r'classList\.\w+\(\s*["\']([^"\']+)["\']', text):
        applied.update(m.group(1).split())

# ---------------------------------------------------------------------------
unused: list[str] = []
for cls, sheet in sorted(declared.items()):
    if cls in applied or cls in THIRD_PARTY:
        continue
    # Where else does the name occur? An occurrence outside a class position is
    # usually prose, and saying so turns a judgement call into a decision.
    elsewhere = [
        f"{name}:{text[: m.start()].count(chr(10)) + 1}"
        for name, text in raw_text.items()
        for m in [re.search(rf"\b{re.escape(cls)}\b", text)]
        if m
    ]
    hint = f"; the name also occurs at {', '.join(elsewhere[:3])}" if elsewhere else ""
    unused.append(f"{sheet}: .{cls} is declared and applied to nothing{hint}")

if unused:
    print("✗ dead CSS under apps/web/src:", file=sys.stderr)
    for line in unused:
        print(f"    {line}", file=sys.stderr)
    print(
        f"\n{len(unused)} class(es) ship in every bundle and style nothing. Delete the "
        "rule, apply it, or — if it belongs to third-party markup — add it to "
        "THIRD_PARTY in this script with the reason.",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(
    f"OK: {len(declared)} class selectors across {len(STYLESHEETS)} stylesheet(s), "
    f"all applied ({len(THIRD_PARTY)} third-party, by name)"
)
PY
