#!/usr/bin/env bash
# Every row `acceptance.sh` runs has a row in `docs/acceptance.md`.
#
# The parts table is the scoreboard a person reads. The gate ran 64 checks
# while the table listed 46, and 18 of them — the ENTIRE plugin cluster
# A7-A11, which CLAUDE.md calls the product — appeared nowhere. Eight of
# CLAUDE.md's own "here is the evidence" citations named rows that did not
# exist: "acceptance A8", "acceptance A10", "acceptance C10/C11". A reader
# following one of those found nothing and had no way to tell whether the
# check was missing or the citation was.
#
# check-dead-refs.sh could not catch it: a row id is not a path. Rule 9's
# check-acceptance-claims.sh runs the other direction, doc -> test. This is
# the missing third edge, gate -> doc.
#
# The reverse direction is deliberately NOT enforced. A doc row may cite a
# test that CI runs rather than a gate row (rule 9 governs those), and
# demanding a gate row for every doc row would push checks into acceptance.sh
# that belong in pytest.
set -euo pipefail
cd "$(dirname "$0")/.."

doc=$(grep -oE '^\| [A-Z][0-9]+[a-z]*' docs/acceptance.md | sed 's/| //' | sort -u)
gate=$(grep -oE '^[[:space:]]*(run_shell|run_py|skip|red|missing) [A-Z][0-9]+[a-z]*' \
         scripts/acceptance.sh | awk '{print $2}' | sort -u)

missing=$(comm -13 <(echo "$doc") <(echo "$gate"))

# Third edge: prose that cites "acceptance A8" must name a row that exists.
# CLAUDE.md carried `acceptance C10/C11` and `acceptance F7/F8`; C10 and F7
# existed nowhere, in either the doc or the gate. A citation that names nothing
# reads as evidence and is an assertion — the same rule check-dead-refs.sh
# applies to paths, applied to row ids, which are not paths and so were invisible.
cited=$(grep -rhoE 'acceptance [A-Z][0-9]+[a-z]*' CLAUDE.md docs/*.md 2>/dev/null \
          | awk '{print $2}' | sort -u)
phantom=""
for r in $cited; do
  echo "$doc" | grep -qx "$r" || phantom="$phantom $r"
done
if [ -n "$phantom" ]; then
  echo "✗ prose cites acceptance rows that do not exist:" >&2
  for r in $phantom; do
    echo "    $r — cited in $(grep -rlE "acceptance $r([^0-9a-z]|\$)" CLAUDE.md docs/*.md 2>/dev/null | tr '\n' ' ')" >&2
  done
  echo >&2
  echo "A citation that names nothing reads as evidence and is an assertion." >&2
  exit 1
fi

if [ -n "$missing" ]; then
  echo "✗ rows the gate runs with no row in docs/acceptance.md:" >&2
  for r in $missing; do
    echo "    $r — $(grep -oE "(run_shell|run_py|skip) $r \"[^\"]*\"" scripts/acceptance.sh \
            | head -1 | sed 's/^[a-z_]* [A-Z0-9a-z]* //')" >&2
  done
  echo >&2
  echo "$(echo "$missing" | wc -l | tr -d ' ') check(s) run on every gate invocation and appear on no scoreboard." >&2
  exit 1
fi

echo "✓ acceptance rows: all $(echo "$gate" | wc -l | tr -d ' ') gate rows appear in docs/acceptance.md"
