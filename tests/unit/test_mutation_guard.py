"""A mutation that changes nothing must be a hard error, not a silent pass.

`scripts/_acceptance/self_check.sh` breaks a subject file and requires the
matching check to notice. Three real probes in this repo have been found lying,
and all three lied the same way: the perl expression matched nothing, so the
check ran against the UNBROKEN tree and the probe reported on that.

  * one expression stopped matching after a refactor moved the code it named;
  * one named a migration file that a newer migration displaced, so
    `check-migration-roundtrip.sh --last` no longer executed the mutated file
    at all — it printed "can fail" while breaking nothing, and occupied the slot
    where a real probe would have gone;
  * one targeted an inline `permissions=[` literal that had been moved into a
    function.

`scripts/_lib/mutation.sh::apply_mutation` closes it by comparing the file with
its own backup after the substitution. These tests are the check on the check —
without them the guard is one more piece of machinery nobody has watched fail.
"""

from __future__ import annotations

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]
LIB = REPO / "scripts/_lib/mutation.sh"


def _apply(target: pathlib.Path, backup: pathlib.Path, expr: str) -> int:
    """Run `apply_mutation` exactly as self_check.sh would, and return its code."""
    script = f'set -uo pipefail; source "{LIB}"; apply_mutation "{target}" "{backup}" \'{expr}\''
    return subprocess.run(["bash", "-c", script], check=False).returncode


def test_a_real_substitution_applies_and_reports_success(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "subject.py"
    target.write_text("value = 1\n")
    backup = tmp_path / "backup"

    assert _apply(target, backup, "s/value = 1/value = 2/") == 0
    assert target.read_text() == "value = 2\n"
    # The backup is written by apply_mutation itself, so restoration is always
    # from the true pre-mutation bytes rather than from whatever the caller
    # copied earlier.
    assert backup.read_text() == "value = 1\n"


def test_an_expression_that_matches_nothing_is_a_hard_error(tmp_path: pathlib.Path) -> None:
    """Exit 2 — the case that produced three false "can fail" reports."""
    target = tmp_path / "subject.py"
    target.write_text("value = 1\n")

    assert _apply(target, tmp_path / "backup", "s/no_such_text_anywhere/x/") == 2
    assert target.read_text() == "value = 1\n"


def test_the_caret_anchor_trap_is_caught(tmp_path: pathlib.Path) -> None:
    """`perl -0` slurps the whole file, so `^` without /m anchors only at byte 0.

    This is the exact shape of the first silent no-op found: an expression that
    reads correctly, applies to nothing past the first line, and reports a
    working check.
    """
    target = tmp_path / "subject.py"
    target.write_text("first line\nexport const THING = 1;\n")

    assert _apply(target, tmp_path / "backup", "s/^export const THING/export const OTHER/") == 2
    # …and the same expression WITH /m is a real mutation.
    assert _apply(target, tmp_path / "backup2", "s/^export const THING/export const OTHER/m") == 0


def test_a_broken_expression_is_reported_and_leaves_the_file_intact(
    tmp_path: pathlib.Path,
) -> None:
    """Exit 3, and the subject is restored.

    Under `set -uo pipefail` (self_check.sh does not use `-e`) a failing perl
    is otherwise silent, and a half-written file would be inherited by every
    later probe in the same run.
    """
    target = tmp_path / "subject.py"
    target.write_text("value = 1\n")

    assert _apply(target, tmp_path / "backup", "s/[unterminated/x/") == 3
    assert target.read_text() == "value = 1\n"


def test_every_self_check_mutation_expression_actually_applies() -> None:
    """The probes in self_check.sh, checked without running their checks.

    This is the cheap half of the guard and it needs no subject to be broken:
    for each `probe NAME FILE EXPR CMD`, apply EXPR to a COPY of FILE and require
    the copy to change. A probe whose expression matches nothing is reported here
    by name, in a unit test that runs on every push, rather than being noticed
    the next time somebody reads the self-check output carefully.

    Skipped rather than failed if the probe list cannot be parsed — a parser
    that silently matches zero probes and passes would be this file's own
    failure mode.
    """
    import pytest

    self_check = REPO / "scripts/_acceptance/self_check.sh"
    if not self_check.is_file():
        pytest.skip(f"{self_check} is not there")

    probes = _parse_probes(self_check.read_text())
    if not probes:
        pytest.skip("could not parse any `probe` invocations — update this parser")

    import shutil
    import tempfile

    noops: list[str] = []
    for name, rel, expr in probes:
        subject = REPO / rel
        if not subject.is_file():
            # A probe naming a file that is not there is check-dead-refs's
            # business, not this test's; do not double-report it.
            continue
        with tempfile.TemporaryDirectory() as tmp:
            copy = pathlib.Path(tmp) / "subject"
            shutil.copy(subject, copy)
            backup = pathlib.Path(tmp) / "backup"
            if _apply(copy, backup, expr) != 0:
                noops.append(f"{name} → {rel}")

    assert not noops, (
        "these self_check.sh probes apply a mutation that changes nothing, so "
        "they test the UNBROKEN tree and their verdict is meaningless: " + "; ".join(noops)
    )


def _parse_probes(source: str) -> list[tuple[str, str, str]]:
    """(name, file, perl expression) for each `probe` call in self_check.sh.

    The call shape is fixed and line-oriented:

        probe "name" \\
          path/to/file \\
          's/a/b/' \\
          "command"

    so a small line-continuation join is enough and nothing here needs a shell
    parser. Any probe whose FILE is a shell variable (the migration head is
    resolved at run time on purpose) is skipped — its path is not knowable
    statically, which is exactly why it is a variable.
    """
    import re
    import shlex

    joined = re.sub(r"\\\n\s*", " ", source)
    out: list[tuple[str, str, str]] = []
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped.startswith("probe "):
            continue
        try:
            parts = shlex.split(stripped)
        except ValueError:
            continue
        if len(parts) < 4:
            continue
        _, name, path, expr = parts[0], parts[1], parts[2], parts[3]
        if "$" in path or "$" in expr:
            continue
        out.append((name, path, expr))
    return out
