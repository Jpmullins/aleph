#!/usr/bin/env bash
#
# A mutation that changes nothing proves nothing.
#
# `scripts/_acceptance/self_check.sh` breaks a subject and requires its check to
# notice. That test has three ways to lie, and the file already closes one of
# them:
#
#   1. the check was ALREADY red, so it goes red under mutation no matter what
#      the mutation did — closed by the green-first rule in `probe()`;
#   2. the mutation did not apply. The expression matched nothing (a `^` anchor
#      under `perl -0` without `/m`; a literal that a refactor renamed; a path
#      that a newer file displaced) and the subject is byte-identical to the
#      backup. The check then reports honestly on unbroken code, i.e. green, and
#      the probe prints "CANNOT" — sending you to fix a check that is fine. Or,
#      worse, the check is flaky-red for an unrelated reason and the probe
#      prints "can fail" having broken nothing at all;
#   3. the mutation command itself errored — a malformed perl expression, an
#      unwritable file — which under `set -uo pipefail` (no `-e`) is silent.
#
# This closes 2 and 3. Three of these have been found in this repo, all real:
# a perl substitution that matched nothing after a refactor; a probe naming a
# migration file that a newer migration displaced, so the check it drove no
# longer executed that file at all; and a probe whose subject had moved.
#
# The general rule this encodes: a mutation test must verify that the mutation
# APPLIED before it believes anything about the result. Only the green-first
# rule caught the migration case, and only by accident.
#
# Usage (sourced, not executed):
#
#     source scripts/_lib/mutation.sh
#     if ! apply_mutation "$file" "$backup" "$perl_expr"; then
#       case $? in
#         2) echo "the mutation changed nothing" ;;
#         3) echo "the mutation command failed" ;;
#       esac
#     fi
#
# Exit codes are distinct on purpose: "matched nothing" and "perl blew up" want
# different fixes, and collapsing them to 1 loses the one piece of information
# the reader needs.

# apply_mutation FILE BACKUP PERL_EXPR
#
#   0 — the mutation applied and the file changed.
#   2 — NO-OP: perl succeeded and the file is byte-identical. The expression
#       matched nothing. Never treat this as a result.
#   3 — the mutation command failed outright.
#
# Writes the backup itself, so the backup is guaranteed to be the pre-mutation
# bytes rather than whatever a caller happened to copy earlier.
apply_mutation() {
  local file="$1" backup="$2" expr="$3"

  cp "$file" "$backup" || return 3
  if ! perl -0pi -e "$expr" "$file" 2>/dev/null; then
    # Restore before returning: a half-applied mutation left on disk is worse
    # than no mutation, because the next probe in the run inherits it.
    cp "$backup" "$file"
    return 3
  fi
  if cmp -s "$file" "$backup"; then
    return 2
  fi
  return 0
}

# describe_mutation_failure CODE — one line naming what to do about it.
describe_mutation_failure() {
  case "$1" in
    2) printf 'the mutation matched nothing — the file is unchanged, so this probe tested the UNBROKEN tree. Fix the expression (a `^` anchor needs /m under perl -0), not the check.' ;;
    3) printf 'the mutation command itself failed — malformed expression or unwritable file.' ;;
    *) printf 'unknown mutation failure (%s).' "$1" ;;
  esac
}
