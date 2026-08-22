#!/usr/bin/env bash
#
# The number of wiki lint checks the docs claim must be the number lint.py
# emits.
#
# `docs/wiki-schema.md` said **13** while `aleph_wiki.lint` emitted **16**, and
# the same document listed all sixteen by name four sections further down. Both
# statements were in the file at once and nothing compared them. That is the
# documentation failure CLAUDE.md's opening paragraph exists to prevent — a doc
# asserting a number nobody re-derives is a doc that stops being evidence — and
# it is cheap to close, because the number is mechanically derivable.
#
# What counts as a check is the `check` field on a `Finding`, because that is
# the unit a reader of the report sees and filters on. Two flavours exist:
#
#   * a literal, `check="orphan"` — counted once per DISTINCT name, since
#     `stub-ready` is raised from two call sites and is still one check; and
#   * the schema family, `check=f"schema:{violation.field}"` — one check
#     ("schema violations"), whose sub-field is a detail of the message.
#
# Counting lines instead of distinct names gets 16 by cancelling two errors
# (double-counting `stub-ready`, missing the f-string family), which is the kind
# of agreement that stops being right the moment either side changes.
#
# Not CI-wired yet — see the WS-H8 report for the job to add.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DOC="docs/wiki-schema.md"
LINT="packages/aleph-wiki/src/aleph_wiki/lint.py"

for f in "$DOC" "$LINT"; do
  [ -f "$f" ] || { echo "check-lint-count: missing $f" >&2; exit 2; }
done

# --- what the code emits ---------------------------------------------------
literal=$(grep -o 'check="[a-z:-]*"' "$LINT" | sort -u | wc -l | tr -d ' ')
family=$(grep -c 'check=f"schema:' "$LINT" || true)
[ "$family" -gt 0 ] && family=1
actual=$((literal + family))

# --- what the shape table claims -------------------------------------------
row=$(grep -n '| Health |' "$DOC" || true)
if [ -z "$row" ]; then
  echo "check-lint-count: no '| Health |' row in $DOC — has the shape table moved?" >&2
  exit 2
fi
documented=$(printf '%s' "$row" | grep -oE '[0-9]+ read-only checks' | grep -oE '^[0-9]+' || true)
if [ -z "$documented" ]; then
  echo "check-lint-count: the '| Health |' row of $DOC states no check count" >&2
  exit 2
fi

# --- what the Lint section lists by name -----------------------------------
# The same claim, spelled out. It disagreed with the table for as long as both
# existed, so both are compared against the code rather than against each other.
# `·` is two bytes in UTF-8, so it is counted with `grep -o` rather than split
# with `tr`, which would split it in half and count every item twice.
separators=$(awk '/^Checks: /{flag=1} flag{printf "%s ", $0} flag && /^$/{exit}' "$DOC" \
  | grep -o '·' | wc -l | tr -d ' ')
if [ "$separators" -eq 0 ]; then
  echo "check-lint-count: no 'Checks: a · b · …' list in $DOC's Lint section" >&2
  exit 2
fi
listed=$((separators + 1))

status=0
if [ "$documented" != "$actual" ]; then
  echo "✗ $DOC's shape table says $documented lint checks; $LINT emits $actual" >&2
  status=1
fi
if [ "$listed" != "$actual" ]; then
  echo "✗ $DOC's Lint section names $listed checks; $LINT emits $actual" >&2
  status=1
fi
if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "  distinct check= literals: $literal" >&2
  echo "  schema: family present:   $family" >&2
  grep -o 'check="[a-z:-]*"' "$LINT" | sort -u | sed 's/^/    /' >&2
  exit 1
fi

echo "✓ $DOC and $LINT agree: $actual lint checks"
