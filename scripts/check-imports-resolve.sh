#!/usr/bin/env bash
# Every module a tracked file imports is itself tracked.
#
# `main.py` was committed importing `routes.background_tasks` and
# `routes.catalogs`; neither file was added. A clean checkout of that commit
# raises `ImportError: cannot import name 'background_tasks'` inside
# `create_app()`, so the API could not start at all — while every local gate
# stayed green, because the files were sitting untracked in the working tree
# where pytest, ruff and pyright all read them.
#
# That is the failure mode this exists for: the caller lands and the callee
# does not. Nothing else notices, because nothing else looks at the repository
# as somebody else would receive it. Both a `git add -p` that missed a new file
# and a partially-staged commit produce it.
#
# It reads the WORKING TREE content of tracked files and asks git which files
# are tracked. In CI those are the same thing, so the answer is exact. Locally
# it warns as soon as you write the import, before the commit that would strand
# it — which is the point at which the mistake is cheap.
#
# It resolves imports against the WORKSPACE (`apps/*/src`, `packages/*/src`),
# not against site-packages: a third-party import is not this check's business
# and a workspace import is.
#
# Exit 0 pass · 1 fail.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - "$@" <<'PY'
import ast
import pathlib
import subprocess
import sys

ROOT = pathlib.Path.cwd()


def tracked() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    return {p for p in out.split("\0") if p}


TRACKED = tracked()
if not TRACKED:
    print("✗ git reports no tracked .py files — this check would pass on nothing",
          file=sys.stderr)
    raise SystemExit(1)

#: Every import root the workspace itself provides, mapped to the source roots
#: it can live under. Derived from the tree rather than listed, so a new
#: package is covered the day it is added.
SOURCE_ROOTS = sorted(
    {p for p in ROOT.glob("packages/*/src") if p.is_dir()}
    | {p for p in ROOT.glob("apps/*/src") if p.is_dir()}
)
WORKSPACE_TOP = {
    child.name
    for root in SOURCE_ROOTS
    for child in root.iterdir()
    if child.is_dir() and not child.name.startswith((".", "_"))
}

TRACKED_PATHS = {ROOT / p for p in TRACKED}


def resolves(dotted: str) -> bool:
    """True if `dotted` names a tracked module or package in the workspace."""
    for root in SOURCE_ROOTS:
        base = root.joinpath(*dotted.split("."))
        if base.with_suffix(".py") in TRACKED_PATHS:
            return True
        if (base / "__init__.py") in TRACKED_PATHS:
            return True
    return False


def exists_exactly(candidate: pathlib.Path) -> bool:
    """`candidate.exists()`, but case-sensitively.

    macOS is case-insensitive, so `Path("…/aleph_kernel/Context.py").exists()`
    is True because `context.py` is there — which turned every
    `from aleph_kernel import Kernel, Context` into a reported missing module.
    Every path component from the source root down is checked against the real
    directory listing, so a Linux CI run and a mac run agree.
    """
    if not candidate.exists():
        return False
    for parent, name in zip(candidate.parents, reversed(candidate.parts)):
        if parent == candidate:
            continue
        try:
            if name not in {entry.name for entry in parent.iterdir()}:
                return False
        except (NotADirectoryError, PermissionError, FileNotFoundError):
            return False
    return True


def on_disk(dotted: str) -> pathlib.Path | None:
    """The file `dotted` would load from, if one is there but untracked.

    This is what separates `from aleph_api.routes import background_tasks` —
    a MODULE somebody forgot to `git add` — from `from aleph_wiki.export import
    PageEvidence`, a symbol. Statically the two are the same syntax; the
    difference is whether a file by that name exists.
    """
    for root in SOURCE_ROOTS:
        base = root.joinpath(*dotted.split("."))
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if exists_exactly(candidate):
                return candidate
    return None


problems: list[str] = []
for rel in sorted(TRACKED):
    path = ROOT / rel
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError) as exc:
        problems.append(f"{rel}: will not parse ({exc.__class__.__name__})")
        continue

    package = None
    for root in SOURCE_ROOTS:
        try:
            package = path.relative_to(root).parent.parts
            break
        except ValueError:
            continue

    def report(dotted: str, lineno: int) -> None:
        found = on_disk(dotted)
        hint = f" (it is at {found.relative_to(ROOT)} — `git add` it)" if found else ""
        problems.append(f"{rel}:{lineno}: imports `{dotted}`, which is not tracked{hint}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in WORKSPACE_TOP and not resolves(alias.name):
                    report(alias.name, node.lineno)
            continue

        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level:
            # Relative: resolve against this file's own package.
            if package is None:
                continue
            base_parts = list(package[: len(package) - (node.level - 1)] or package)
            module = ".".join([*base_parts, *(node.module.split(".") if node.module else [])])
        else:
            module = node.module or ""
        if not module or module.split(".")[0] not in WORKSPACE_TOP:
            continue

        # The module itself. Report it once rather than once per symbol.
        if not resolves(module):
            report(module, node.lineno)
            continue

        # The module resolves, so each imported name is either a symbol it
        # re-exports (fine, and not statically checkable) or a SUBMODULE. Only
        # a submodule has a file, and only an untracked one is a problem.
        for alias in node.names:
            if alias.name == "*":
                continue
            dotted = f"{module}.{alias.name}"
            if resolves(dotted):
                continue
            if on_disk(dotted) is not None:
                report(dotted, node.lineno)

if problems:
    print("✗ tracked code imports untracked modules:", file=sys.stderr)
    for problem in sorted(set(problems)):
        print(f"    {problem}", file=sys.stderr)
    print(
        f"\n{len(set(problems))} import(s) would raise ImportError in a clean "
        "checkout. The caller landed without the callee.",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(f"OK: every workspace import in {len(TRACKED)} tracked .py files resolves to tracked code")
PY
