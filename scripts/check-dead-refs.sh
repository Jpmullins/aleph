#!/usr/bin/env bash
#
# Every path this repository names in prose must resolve.
#
# CLAUDE.md's "Fixed, with the test that pins each" section is the mechanism that
# stops a fixed bug from silently un-fixing. Four of its pins pointed at files
# deleted in the harness reset, and `apps/api/src/aleph_api/routes/assets.py`
# made the same claim about a fifth. A pin that names nothing is worse than no
# pin: it reads as evidence and is an assertion.
#
# The same rot has three other shapes, all of which were live in this tree:
#   * `pnpm-workspace.yaml` declaring a member directory that does not exist;
#   * a dangling symlink under `audit/` or `tests/`, which made six e2e checks
#     SKIP forever while `audit/run.sh` reported no failures;
#   * `docs/operations.md` naming a `scripts/check-*.sh` that was deleted.
#
# The token regex is anchored at a path boundary on purpose. A naive
# `tests/[a-z_/]+\.py` slices `tests/test_rrf.py` out of the middle of
# `packages/aleph-rks/tests/test_rrf.py` and then reports a file that was never
# named as missing — a sweep that can only fail is as useless as one that cannot.
#
# CI-wired. Fails on: a path named in prose or in a gate that does not exist.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import pathlib
import re
import sys

ROOT = pathlib.Path(".").resolve()

# Where prose makes claims about the tree. Deliberately not "every file": a
# sweep that reads the whole repo spends its life on false positives from
# generated code and vendored text, and the response to a noisy sweep is to
# switch it off.
#
# Scoped to documents that assert what IS TRUE. `docs/plan.md` and
# `docs/backlog.md` are deliberately excluded: a plan names files it intends to
# create, so sweeping it reports the whole plan as broken. `docs/research/` and
# `docs/update/` are design studies and superseded audits — history, not claims
# about the tree.
PROSE = [
    pathlib.Path("CLAUDE.md"),
    pathlib.Path("docs/acceptance.md"),
    pathlib.Path("docs/architecture.md"),
    pathlib.Path("docs/operations.md"),
    pathlib.Path("docs/decisions.md"),
    pathlib.Path("docs/belief-engine.md"),
    pathlib.Path("docs/wiki-schema.md"),
    pathlib.Path("scripts/acceptance.sh"),
    pathlib.Path("scripts/_acceptance/self_check.sh"),
    pathlib.Path("deploy/README.md"),
]
PROSE += sorted(p for p in pathlib.Path("apps").rglob("*.py") if "__pycache__" not in p.parts)
PROSE += sorted(p for p in pathlib.Path("packages").rglob("*.py") if "__pycache__" not in p.parts)

# A path token, anchored so it starts at a real boundary rather than in the
# middle of a longer path.
TOKEN = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_/.-]))"
    r"((?:tests|packages|apps|scripts|docs|deploy|audit|skills)/[A-Za-z0-9_./+-]*[A-Za-z0-9_+-])"
    r"(::[A-Za-z0-9_]+)?"
)

# Paths that legitimately do not resolve, each with a reason. An entry here is a
# dated decision, not a way to make the sweep quiet.
ALLOW = {
    # Illustrative paths in prose that describe a shape rather than a file.
    "packages/aleph-xxx",
    "apps/api/alembic/versions/YYYYMMDD_HHMM_",
}

problems: list[str] = []
for path in PROSE:
    if not path.is_file():
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in TOKEN.finditer(line):
            token = match.group(1)
            # An elided or glob path describes a shape, not a file:
            # `apps/api/.../lifespan.py`, `scripts/check-*.sh`. The glob case is
            # detected by looking one character PAST the match, because `*` is
            # not in the token character class and would otherwise leave the
            # truncated prefix `scripts/check-` behind as a false positive.
            after = line[match.end(1) : match.end(1) + 1]
            if "..." in token or "*" in token or after == "*":
                continue
            if token in ALLOW or any(token.startswith(a) for a in ALLOW):
                continue
            # Trailing punctuation from prose.
            token = token.rstrip(".,;:)\"'`")
            if not token or (ROOT / token).exists():
                continue
            problems.append(f"{path}:{lineno}: names {token}, which does not exist")

# pnpm workspace members must be real directories. `tests/playwright` was
# declared here for a directory deleted in the harness reset, so `pnpm -C
# tests/playwright ...` was a documented command that could never run.
workspace = pathlib.Path("pnpm-workspace.yaml")
if workspace.is_file():
    for lineno, line in enumerate(workspace.read_text().splitlines(), 1):
        member = line.strip()
        if not member.startswith("- "):
            continue
        member = member[2:].strip().strip("'\"")
        if "*" in member:
            continue
        if not (ROOT / member).is_dir():
            problems.append(
                f"{workspace}:{lineno}: declares workspace member {member}, "
                "which is not a directory"
            )

# Dangling symlinks. `audit/checks/e2e/node_modules` pointed at a deleted
# directory, so `audit/run.sh` never set E2E_OK and six checks silently skipped.
for root in ("audit", "tests", "scripts"):
    base = pathlib.Path(root)
    if not base.is_dir():
        continue
    for link in base.rglob("*"):
        if link.is_symlink() and not link.exists():
            problems.append(f"{link}: dangling symlink → {link.readlink()}")

if problems:
    print("✗ dead references:", file=sys.stderr)
    for problem in problems:
        print(f"    {problem}", file=sys.stderr)
    print(
        f"\n{len(problems)} reference(s) name something that is not there. "
        "Restore the subject, fix the reference, or — if it is genuinely "
        "illustrative — add it to ALLOW with a reason.",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(f"OK: every path named across {len(PROSE)} prose and gate files resolves")
PY
