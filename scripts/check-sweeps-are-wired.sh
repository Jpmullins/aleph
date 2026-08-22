#!/usr/bin/env bash
#
# Every `scripts/check-*.sh` and `scripts/check-*.py` is run by something.
#
# This exists because the same defect landed three times in three consecutive
# batches of work, each time in a sweep whose whole purpose was to prevent that
# class of defect:
#
#   * `check-compose-hardening.sh` — written, correct, in no CI job, no
#     acceptance row, no self-check probe. Its own author noted "please wire".
#   * `check-web-dead-code.sh`, `check-web-dead-css.sh`, `check-web-drift.sh` —
#     same, all three, same change.
#   * `check-agent-catalog-covers-renderer.sh`, `check-confidence-vocabulary.sh`,
#     `check-project-scope.sh` — same again.
#
# In every case the sweep worked. It went red when the thing it guarded broke.
# Nothing ran it, so nobody found out. CLAUDE.md names "written correctly and
# read by nothing" as this codebase's dominant defect class, and a sweep with no
# consumer is that class eating its own antidote.
#
# Wiring them one at a time fixes three files and not the pattern. This fails
# the build on the fourth.
#
# A CONSUMER is: a CI step, a row in `scripts/acceptance.sh`, or a probe in
# `scripts/_acceptance/self_check.sh`. Being mentioned in prose is not a
# consumer — documentation does not execute.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# A sweep counts as wired if one of these names it. The three top-level gates,
# plus the acceptance probes: `check-okf.py` needs an exported vault before it
# has anything to read, so its consumer is `okf_export_probe.py`, which builds
# one and is itself run by acceptance.sh. A probe that nothing runs is caught
# by acceptance.sh's own MISSING status, so this does not open a hole.
CONSUMERS=(
  ".github/workflows/ci.yml"
  "scripts/acceptance.sh"
  "scripts/_acceptance/self_check.sh"
)
for probe in scripts/_acceptance/*.py scripts/_acceptance/*.mjs; do
  [ -e "$probe" ] && CONSUMERS+=("$probe")
done

# Sweeps that legitimately have no consumer, each with a reason. Keep this
# empty if you can: an allowlist is where this check goes to die.
declare_allowed() {
  case "$1" in
    # `--self-check` invokes acceptance.sh, which invokes the rest; a check
    # that ran itself would recurse.
    check-sweeps-are-wired.sh) return 0 ;;
    *) return 1 ;;
  esac
}

unwired=()
total=0
# `.sh` AND `.py`. The glob was `.sh` only, so `scripts/check-okf.py` — six
# validation rules for the export format — was invisible to the sweep whose one
# job is finding sweeps nobody runs. It had no consumer at all: not ci.yml, not
# acceptance.sh, not self_check.sh, only a mention in prose.
for path in scripts/check-*.sh scripts/check-*.py; do
  [ -e "$path" ] || continue
  name="$(basename "$path")"
  total=$((total + 1))
  declare_allowed "$name" && continue
  found=0
  for consumer in "${CONSUMERS[@]}"; do
    [ -f "$consumer" ] || continue
    if grep -q -- "$name" "$consumer"; then
      found=1
      break
    fi
  done
  [ $found -eq 0 ] && unwired+=("$name")
done

if [ ${#unwired[@]} -gt 0 ]; then
  printf '\033[31m✗\033[0m %d sweep(s) that nothing runs:\n' "${#unwired[@]}" >&2
  for name in "${unwired[@]}"; do
    printf '    %s\n' "$name" >&2
  done
  cat >&2 <<'WHY'

  A sweep with no consumer is the defect class it was written to catch. Add it
  to one of:
    .github/workflows/ci.yml
    scripts/acceptance.sh          (run_shell, or run_expected_red_shell if it
                                    is red today for a known reason)
    scripts/_acceptance/self_check.sh
WHY
  exit 1
fi

printf '\033[32m✓\033[0m every sweep is run by something (%d checked)\n' "$total"
