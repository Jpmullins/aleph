#!/usr/bin/env bash
#
# A React module nothing imports still type-checks, still lints, and still
# builds.
#
# `pnpm -C apps/web build` runs `tsc --noEmit && vite build`. Neither can notice
# a file that is never reached: tsc type-checks everything `include` names
# whether or not it is in the graph, and Vite simply never visits it, so it is
# not even in the bundle it produces. The result was measurable — four modules
# under `apps/web/src` (716 lines) had zero importers anywhere in the repository
# and both gates were green on all of them.
#
# That is the defect class CLAUDE.md names as this codebase's dominant one, and
# it is worse on the web side than in Python because the dead file goes on
# reading as documentation: `ActivityCard.tsx` was the only code that ever
# consumed the agent's streamed `todos` plan, so grepping for `useAgent` told
# you Aleph consumed agent state when nothing did.
#
# This walks the real module graph from the app's entry point and calls anything
# it cannot reach an error. Two things it is careful about:
#
#   * The `@/` alias is READ from `apps/web/vite.config.ts` rather than assumed.
#     A hardcoded copy here would keep passing after someone repointed the
#     alias, and would then report the whole tree as dead — or, worse, resolve
#     nothing and report the whole tree as alive.
#   * `components/Icons.tsx` is a second entry point, because `Rail.tsx` does
#     `Icons[kind.icon]` — a runtime string lookup the walk cannot see. Icon
#     KEYS are checked separately in both directions below.
#
# Exemptions live in `apps/web/.deadcode-allow` and each one must carry a
# backlog id and an ISO date, so keeping a dead file is a dated decision rather
# than silence. A stale exemption — one naming a file that is gone, or one that
# is now reachable — is itself an error.
#
# CI-wired. Fails on: an unreachable module, a stale exemption, an import that
# resolves to nothing, an icon the server names that the client does not ship,
# or an icon the client ships that nothing names.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import pathlib
import re
import sys

WEB = pathlib.Path("apps/web")
SRC = WEB / "src"
ALLOW_FILE = WEB / ".deadcode-allow"
VITE_CONFIG = WEB / "vite.config.ts"
RAIL = SRC / "components/Rail.tsx"
ICONS = SRC / "components/Icons.tsx"
REGISTRY = pathlib.Path("packages/aleph-a2ui/src/aleph_a2ui/pane_registry.py")

problems: list[str] = []


def die(msg: str) -> None:
    print(f"✗ {msg}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Strip comments before looking for imports.
#
# A commented-out import is not an edge. Without this, deleting the last real
# importer of a module and leaving `// import { X } from "./X"` behind keeps the
# module "reachable" forever — the sweep would then be certifying exactly the
# state it exists to find. A regex cannot do this safely on its own: `"http://"`
# contains `//`, and a template literal can contain anything at all.
# ---------------------------------------------------------------------------
def strip_comments(text: str) -> str:
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


# `import x from "s"`, `import "s"`, `export … from "s"`, and `import("s")`.
STATIC = re.compile(
    r'(?:^|[\s;}])(?:import|export)\s*(?:[^;\'"]*?\bfrom\s*)?["\']([^"\']+)["\']',
    re.M,
)
DYNAMIC = re.compile(r'\bimport\s*\(\s*["\']([^"\']+)["\']\s*\)')

# ---------------------------------------------------------------------------
# The `@/` alias, read from the config that actually defines it.
# ---------------------------------------------------------------------------
if not VITE_CONFIG.is_file():
    die(f"{VITE_CONFIG} is gone — this sweep reads the `@/` alias from it")

alias_match = re.search(
    r'["\']@["\']\s*:\s*new URL\(\s*["\']([^"\']+)["\']', VITE_CONFIG.read_text()
)
if not alias_match:
    die(
        f"could not read the `@/` alias from {VITE_CONFIG}. A hardcoded fallback "
        "here would resolve every `@/…` import to nothing and report the whole "
        "app as dead, so this is fatal rather than a default."
    )
ALIAS_ROOT = (WEB / alias_match.group(1)).resolve()
if not ALIAS_ROOT.is_dir():
    die(f"the `@/` alias in {VITE_CONFIG} points at {ALIAS_ROOT}, which is not a directory")

# ---------------------------------------------------------------------------
# Entry points.
#
# The real entry is whatever `index.html` loads; taking it from there rather
# than assuming `main.tsx` means renaming the entry cannot silently turn the
# whole app into unreachable code.
# ---------------------------------------------------------------------------
index_html = WEB / "index.html"
if not index_html.is_file():
    die(f"{index_html} is gone — it names the app's entry module")
entry_match = re.search(
    r'<script[^>]*type=["\']module["\'][^>]*src=["\']([^"\']+)["\']', index_html.read_text()
)
if not entry_match:
    die(f"no <script type=module src=…> in {index_html}; cannot find the entry point")
entry_rel = entry_match.group(1).lstrip("/")
entry = (WEB / entry_rel).resolve()
if not entry.is_file():
    die(f"{index_html} loads {entry_rel}, which does not exist")

if not ICONS.is_file():
    die(f"{ICONS} is gone — Rail.tsx resolves icons out of it by name")

ENTRIES = [entry, ICONS.resolve()]

EXTS = (".tsx", ".ts", ".jsx", ".js", ".css", ".json")


def resolve(spec: str, frm: pathlib.Path) -> pathlib.Path | None | str:
    """Return the file a specifier names, None for a package, or a reason string."""
    if spec.startswith("@/"):
        base = ALIAS_ROOT / spec[2:]
    elif spec.startswith("."):
        base = (frm.parent / spec).resolve()
    else:
        return None  # a bare specifier is a node_modules package
    candidates = [base]
    candidates += [base.with_name(base.name + e) for e in EXTS]
    candidates += [base / ("index" + e) for e in EXTS]
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return "unresolvable"


reached: set[pathlib.Path] = set()
stack = list(ENTRIES)
while stack:
    mod = stack.pop()
    if mod in reached:
        continue
    reached.add(mod)
    if mod.suffix not in (".ts", ".tsx", ".js", ".jsx"):
        continue
    body = strip_comments(mod.read_text())
    for spec in set(STATIC.findall(body)) | set(DYNAMIC.findall(body)):
        target = resolve(spec, mod)
        if target is None:
            continue
        if isinstance(target, str):
            problems.append(
                f"{mod.relative_to(pathlib.Path.cwd())}: imports {spec!r}, "
                "which resolves to no file"
            )
            continue
        stack.append(target)

# ---------------------------------------------------------------------------
# Test files are not product modules.
#
# A `*.test.ts(x)` is unreachable from `main.tsx` BY CONSTRUCTION — vitest is its
# entry point, not the app — so requiring reachability would make every new test
# an error and force an allowlist line per test file. That tax is how a project
# ends up with a test runner and no tests.
#
# They are excluded from the list of modules that must be reached, and
# deliberately NOT added as entry points. A module imported only by a test is
# still dead product code, and making tests confer reachability is how a deleted
# feature survives as a tested orphan.
# ---------------------------------------------------------------------------
def is_test(path: pathlib.Path) -> bool:
    return path.name.endswith((".test.ts", ".test.tsx"))


modules = sorted(
    p.resolve() for p in SRC.rglob("*") if p.suffix in (".ts", ".tsx") and not is_test(p)
)
if not modules:
    die(f"found no non-test .ts/.tsx under {SRC} — the sweep would pass on an empty tree")

# ---------------------------------------------------------------------------
# The allowlist. A line is `<path relative to apps/web/src>  <BACKLOG-ID>  <ISO date>  # why`.
# ---------------------------------------------------------------------------
ENTRY_RE = re.compile(
    r"^(?P<path>\S+)\s+(?P<id>[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s*(?:#\s*(?P<why>.*))?$"
)
allowed: dict[pathlib.Path, str] = {}
if ALLOW_FILE.is_file():
    for lineno, raw in enumerate(ALLOW_FILE.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = ENTRY_RE.match(line)
        if not m:
            problems.append(
                f"{ALLOW_FILE}:{lineno}: not `<path> <BACKLOG-ID> <YYYY-MM-DD> # why` — "
                "an exemption without a backlog id and a date is silence with extra steps"
            )
            continue
        if not m.group("why"):
            problems.append(f"{ALLOW_FILE}:{lineno}: no reason given after `#`")
            continue
        target = (SRC / m.group("path")).resolve()
        if not target.is_file():
            problems.append(
                f"{ALLOW_FILE}:{lineno}: exempts {m.group('path')}, which does not exist — "
                "delete the line; a stale exemption hides the next real one"
            )
            continue
        allowed[target] = f"{m.group('id')} {m.group('date')}"

unreachable = [p for p in modules if p not in reached]
for path in unreachable:
    if path in allowed:
        continue
    lines = len(path.read_text().splitlines())
    problems.append(
        f"{path.relative_to(pathlib.Path.cwd())}: {lines} lines, reached by nothing from "
        f"{entry.relative_to(pathlib.Path.cwd())}"
    )

for path, tag in allowed.items():
    if path in reached:
        problems.append(
            f"{ALLOW_FILE}: exempts {path.relative_to(pathlib.Path.cwd())} ({tag}), which IS "
            "reachable — delete the line"
        )

# ---------------------------------------------------------------------------
# Icon keys, both directions.
#
# `Rail.tsx:56` does `Icons[kind.icon] ?? Icons.notes`, so an icon the SERVER
# names and the client does not ship renders as a note and reports nothing —
# every pane in the rail silently wearing the same wrong glyph is the shape this
# catches. The drawer tuple in the same file has no fallback at all: an unknown
# key there is `undefined` used as a component, which throws at render.
#
# The reverse direction is ordinary dead code: an icon nothing names is 6 lines
# of SVG that ships in every bundle and renders nowhere.
# ---------------------------------------------------------------------------
icons_body = strip_comments(ICONS.read_text())
decl = re.search(r"export const Icons\s*=\s*\{(.*?)\n\}", icons_body, re.S)
if not decl:
    die(f"could not parse `export const Icons = {{…}}` out of {ICONS}")
icon_keys = set(re.findall(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*)\s*:", decl.group(1), re.M))
if not icon_keys:
    die(f"parsed zero icon keys out of {ICONS}")

if not REGISTRY.is_file():
    die(f"{REGISTRY} is gone — it is where the server declares each pane's icon")
server_icons = set(re.findall(r'icon="([^"]+)"', REGISTRY.read_text()))
if not server_icons:
    die(f"parsed zero `icon=` values out of {REGISTRY}")

rail_body = strip_comments(RAIL.read_text())
drawer_block = re.search(r"\[\s*(\[\s*\"settings\".*?)\]\s*as const", rail_body, re.S)
if not drawer_block:
    die(
        f"could not find the drawer tuple in {RAIL}. It is the one place icons are "
        "resolved WITHOUT a fallback, so an unchecked rename there throws at render."
    )
drawer_icons = {m[1] for m in re.findall(r'\[\s*"([^"]+)"\s*,\s*"([^"]+)"', drawer_block.group(1))}

# Anything written as a literal `Icons.foo` anywhere in the app.
static_icons: set[str] = set()
for path in modules:
    if path in unreachable and path not in allowed:
        continue  # a dead file's references do not keep an icon alive
    static_icons |= set(re.findall(r"\bIcons\.([A-Za-z_][A-Za-z0-9_]*)\b", strip_comments(path.read_text())))

named = server_icons | drawer_icons | static_icons
for missing in sorted((server_icons | drawer_icons) - icon_keys):
    where = "the pane registry" if missing in server_icons else f"{RAIL}'s drawer tuple"
    problems.append(
        f"{ICONS}: {where} names icon {missing!r}, which Icons does not ship — "
        "the rail falls back to `notes` and nothing reports it"
    )
for orphan in sorted(icon_keys - named):
    problems.append(
        f"{ICONS}: icon {orphan!r} is named by nothing — not the pane registry, "
        f"not {RAIL}'s drawer tuple, not any `Icons.{orphan}` in live code"
    )

if problems:
    print("✗ dead code under apps/web/src:", file=sys.stderr)
    for problem in problems:
        print(f"    {problem}", file=sys.stderr)
    print(
        f"\n{len(problems)} problem(s). Delete the module, wire it up, or add a line to "
        f"{ALLOW_FILE} carrying a backlog id and an ISO date.",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(
    f"OK: {len(modules)} non-test modules under {SRC}, all reachable from "
    f"{entry.relative_to(pathlib.Path.cwd())}"
    + (f" ({len(allowed)} exempt)" if allowed else "")
    + f"; {len(icon_keys)} icons, all named and all shipped"
)
PY
