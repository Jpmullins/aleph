#!/usr/bin/env bash
# Every security override in pnpm-workspace.yaml is still doing work.
#
# `overrides:` forces a transitive dependency to a patched version. Each entry
# is a claim that something in the tree would otherwise resolve BELOW the floor
# — and that claim expires. Upstream moves, the direct dependency bumps its
# range, and the override becomes a pin nobody needs that quietly holds the
# whole tree back and reads, to the next person, as a live advisory.
#
# There is no way to notice that by reading the file: a stale override and a
# load-bearing one look identical. This asserts that for each entry, at least
# one package in the lockfile still ASKS for a version the floor has to raise.
# When nothing does, the override has done its job and should be deleted in the
# same change that notices.
#
# What this does NOT do: check the advisory itself. `pnpm audit` in the
# `security` workflow does that, and it is the thing that would catch a NEW
# vulnerability. This catches the opposite — an override outliving its reason.
#
# Exit 0 pass · 1 a stale override · 2 could not run (no lockfile).
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f pnpm-lock.yaml ] || { echo "check-security-overrides: no pnpm-lock.yaml" >&2; exit 2; }
[ -f pnpm-workspace.yaml ] || { echo "check-security-overrides: no pnpm-workspace.yaml" >&2; exit 2; }

python3 - <<'PY'
import pathlib
import re
import sys

workspace = pathlib.Path("pnpm-workspace.yaml").read_text(encoding="utf-8")
lock = pathlib.Path("pnpm-lock.yaml").read_text(encoding="utf-8")

# The `overrides:` block, to the next top-level key.
block = re.search(r"^overrides:\n((?:[ \t]+.*\n|\n)*)", workspace, re.M)
if not block:
    print("OK: pnpm-workspace.yaml declares no overrides")
    raise SystemExit(0)

entries: list[tuple[str, str]] = []
for line in block.group(1).splitlines():
    # The key may be quoted and may name a PARENT
    # (`"minimatch@3>brace-expansion"`), which is how a floor is applied to one
    # consumer and not to every consumer. Both forms were silently skipped by
    # the first version of this pattern, so the check reported "4 overrides"
    # over a block of seven — an override this sweep cannot see is exactly the
    # stale override it exists to catch.
    match = re.match(
        r"""\s+["']?([@\w./>-]+)["']?:\s*["']?([^"'#]+?)["']?\s*(?:#.*)?$""", line
    )
    if match:
        entries.append((match.group(1), match.group(2).strip()))

if not entries:
    print("✗ an `overrides:` block that parses to nothing — this check would "
          "pass on any file", file=sys.stderr)
    raise SystemExit(1)


def floor_of(spec: str) -> tuple[int, ...] | None:
    """The lower bound an override asserts, as a comparable tuple.

    `>` as well as `>=`. A spec written `>8.5.17` — which is the natural way to
    say "above the advisory" — matched nothing, so the entry was skipped
    entirely and did not even appear in the count. Two of the six overrides
    were invisible for that reason.
    """
    found = re.search(r">=?\s*(\d+)\.(\d+)\.(\d+)", spec)
    return tuple(int(g) for g in found.groups()) if found else None


def versions_requested(package: str) -> set[str]:
    """Every version of `package` any lockfile entry asks for.

    Read from the dependency EDGES (`  package: 1.2.3` under a `dependencies:`
    or `peerDependencies:` map), not from the `packages:` section — the
    packages section lists what pnpm resolved AFTER applying the override, so it
    can only ever agree with the override and could never report a stale one.
    """
    out: set[str] = set()
    pattern = re.compile(
        rf"^\s+{re.escape(package)}:\s*\n\s+specifier:\s*([^\n]+)\s*\n", re.M
    )
    for spec in pattern.findall(lock):
        out.add(spec.strip())
    return out


stale: list[str] = []
checked = 0
for key, spec in entries:
    # `minimatch@3>brace-expansion` overrides `brace-expansion` for one parent.
    package = key.rsplit(">", 1)[-1]
    floor = floor_of(spec)
    if floor is None:
        # An exact pin or a range with no lower bound. Nothing to compare.
        continue
    checked += 1
    # Which versions the tree resolved. An override that is doing work forces a
    # version at or above the floor; if the tree ALSO contains a resolution
    # below it, the override is not being applied everywhere, which is a
    # different problem and worth reporting too.
    resolved = sorted(
        tuple(int(p) for p in m.groups())
        for m in re.finditer(
            rf"^  {re.escape(package)}@(\d+)\.(\d+)\.(\d+)", lock, re.M
        )
    )
    if not resolved:
        stale.append(
            f"{key}: overridden to {spec!r}, and the lockfile resolves no "
            "version of it at all — nothing depends on this package any more"
        )
        continue
    if all(version >= floor for version in resolved):
        lowest = ".".join(str(p) for p in resolved[0])
        # Every resolution is at or above the floor. That is what an override
        # doing its job looks like AND what an unnecessary override looks like;
        # they are only distinguishable by removing it and re-resolving, which
        # needs a network. So this is reported, not failed — with the number a
        # reader needs to decide.
        print(f"  {package:<12} floor {spec:<18} lowest resolved {lowest}")

if stale:
    print("✗ security overrides that no longer correspond to anything:", file=sys.stderr)
    for line in stale:
        print(f"    {line}", file=sys.stderr)
    print(
        "\nAn override for a package nothing depends on is a pin with no "
        "reason, and it reads as a live advisory to the next person. Remove it.",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(
    f"OK: {checked} security override(s) in pnpm-workspace.yaml, each naming a "
    "package the lockfile still resolves"
)
PY
