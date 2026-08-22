"""Diff every place that spells out the confidence vocabulary.

How confident the system is in a claim was written down in **four** vocabularies
that did not agree, and every one of them looked correct in isolation:

* the derived engine emitted six underscore-spelled states;
* the A2UI catalog permitted six *different* words — ``cited``, ``uncited`` and
  ``retracted`` among them — two of the overlapping ones hyphenated;
* ``ClaimCard.tsx`` branched on four literals and rendered the other three as
  grey, so a claim the evidence had DISPROVED looked like one nobody had
  assessed;
* ``GroundingSurface.tsx`` keyed its badge on ``cited | inferred | contested |
  retracted``, of which exactly one is a confidence at all (``inferred`` is an
  ``evidence_tier``), so that surface never showed a colour that meant anything.

None of that could fail a test. Each component agreed with itself, the strings
crossed the boundary as free-form text, and the only symptom was a badge with
the wrong colour on a page nobody was diffing. This sweep is the thing that can
fail: it reads the canonical enum out of ``aleph-core`` and compares every other
spelling of the set against it.

It is deliberately static — no database, no running app, no import of the
application packages — so it works in a bare checkout and cannot be fooled by a
value that happens to be absent from the live data today.

Used by ``scripts/check-confidence-vocabulary.sh`` and by
``tests/unit/test_confidence_vocabulary.py`` — one implementation, two callers.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
from dataclasses import dataclass

from sweep_subject import require_subject

__all__ = [
    "Mismatch",
    "canonical_values",
    "catalog_enums",
    "compare",
    "grounding_surface_keys",
    "html_compiler_keys",
    "run",
    "web_switch_cases",
    "web_union_values",
]

ROOT = pathlib.Path(__file__).resolve().parents[2]

CANONICAL_PY = ROOT / "packages/aleph-core/src/aleph_core/confidence.py"
CATALOG_JSON = ROOT / "packages/aleph-a2ui/src/aleph_a2ui/catalog.json"
WEB_CONFIDENCE_TS = ROOT / "apps/web/src/a2ui/confidence.ts"
HTML_COMPILER_PY = ROOT / "packages/aleph-wiki/src/aleph_wiki/html_compiler.py"
GROUNDING_TSX = ROOT / "apps/web/src/a2ui/components/GroundingSurface.tsx"


@dataclass(frozen=True, slots=True)
class Mismatch:
    where: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: {self.detail}"


def canonical_values() -> list[str]:
    """The values of ``Confidence`` in ``aleph_core.confidence``, in order.

    Parsed rather than imported. A sweep that imports the package it checks
    fails for reasons unrelated to the check — an unrelated import error in a
    sibling module, a missing optional dependency — and the usual reaction to a
    gate that fails for the wrong reason is to switch it off.
    """
    src = require_subject(
        CANONICAL_PY, "it defines the canonical `Confidence` enum this sweep diffs against"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Confidence":
            values: list[str] = []
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign | ast.AnnAssign)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    values.append(stmt.value.value)
            return values
    msg = f"{CANONICAL_PY} defines no class named Confidence"
    raise ValueError(msg)


def _walk_enums(node: object, path: tuple[str, ...] = ()) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "confidence" and isinstance(value, dict) and "enum" in value:
                found.append((".".join([*path, key]), list(value["enum"])))
            found.extend(_walk_enums(value, (*path, str(key))))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.extend(_walk_enums(value, (*path, str(i))))
    return found


def catalog_enums() -> list[tuple[str, list[str]]]:
    """Every ``confidence`` enum in the canonical A2UI catalog, by json path.

    There are two — the renderer's schema and the agent-facing one — and they
    are read separately on purpose: they disagreed with each other once already
    (`ClaimCard.confidence` listed no `"cited"` in any copy while both wiki
    writers hardcoded it), which is what `scripts/gen_catalog.py` exists to
    prevent for the *generated* copies and nothing prevented for these two.
    """
    raw = require_subject(CATALOG_JSON, "it holds the A2UI `ClaimCard.confidence` enums").read_text(
        encoding="utf-8"
    )
    return _walk_enums(json.loads(raw))


_TS_ARRAY = re.compile(r"export const CONFIDENCE = \[(?P<body>.*?)\] as const;", re.DOTALL)
_TS_STRING = re.compile(r'"([a-z_]+)"')
_TS_CASE = re.compile(r'^\s*case "([a-z_]+)":', re.MULTILINE)
_TS_RECORD_KEY = re.compile(r"^\s*([a-z_]+):", re.MULTILINE)


def web_union_values() -> list[str]:
    """The `CONFIDENCE` tuple the client's `Confidence` union is built from."""
    src = require_subject(
        WEB_CONFIDENCE_TS, "it declares the client-side `Confidence` union"
    ).read_text(encoding="utf-8")
    match = _TS_ARRAY.search(src)
    if match is None:
        msg = f"{WEB_CONFIDENCE_TS} has no `export const CONFIDENCE = [...] as const;`"
        raise ValueError(msg)
    return _TS_STRING.findall(match.group("body"))


def web_switch_cases() -> set[str]:
    """The states `confidenceTone` actually has a branch for.

    The `never` check in that switch already makes a MISSING branch a compile
    error, so this looks for the opposite failure: a branch for a state the
    enum no longer has, which compiles fine and is dead code that reads as
    support for a value nothing can produce.
    """
    src = require_subject(
        WEB_CONFIDENCE_TS, "it holds the renderer's confidence branch labels"
    ).read_text(encoding="utf-8")
    return set(_TS_CASE.findall(src))


def grounding_surface_keys() -> set[str]:
    """`GroundingSurface.CONFIDENCE_STYLES` keys — the badge classes."""
    src = require_subject(
        GROUNDING_TSX, "it maps each confidence state to a badge class"
    ).read_text(encoding="utf-8")
    start = src.find("const CONFIDENCE_STYLES")
    if start < 0:
        msg = f"{GROUNDING_TSX} no longer declares CONFIDENCE_STYLES"
        raise ValueError(msg)
    end = src.find("};", start)
    return set(_TS_RECORD_KEY.findall(src[start:end]))


def html_compiler_keys() -> set[str]:
    """`_CONF_STYLE` keys in the HTML compiler, as `Confidence.MEMBER` names.

    Returned as *values* by looking the member names back up in the enum, so a
    caller compares like with like. The compiler keys on the enum member rather
    than on a string precisely so this cannot drift silently — but a member
    deleted from `_CONF_STYLE` is still only a KeyError at render time for that
    one state, which is a bad place to find out.
    """
    src = require_subject(
        HTML_COMPILER_PY, "it maps each confidence state to a badge colour"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    member_to_value = dict(zip(_canonical_member_names(), canonical_values(), strict=True))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if "_CONF_STYLE" not in names or not isinstance(node.value, ast.Dict):
            continue
        keys: set[str] = set()
        for key in node.value.keys:
            if isinstance(key, ast.Attribute):
                keys.add(member_to_value.get(key.attr, f"<unknown member {key.attr}>"))
        return keys
    msg = f"{HTML_COMPILER_PY} no longer declares _CONF_STYLE"
    raise ValueError(msg)


def _canonical_member_names() -> list[str]:
    src = CANONICAL_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Confidence":
            names: list[str] = []
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                    names.extend(t.id for t in stmt.targets if isinstance(t, ast.Name))
                elif (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and isinstance(stmt.value, ast.Constant)
                ):
                    names.append(stmt.target.id)
            return names
    msg = "no Confidence class"
    raise ValueError(msg)


def compare() -> list[Mismatch]:
    """Every disagreement with the canonical enum, as a list."""
    canonical = canonical_values()
    canonical_set = set(canonical)
    problems: list[Mismatch] = []

    if not canonical:
        problems.append(Mismatch("aleph_core.confidence.Confidence", "declares no members"))
        return problems

    # Ordered comparisons: the two lists are written out by hand, and order is
    # part of the contract so a human diffing them sees a real difference.
    for path, values in catalog_enums():
        if values != canonical:
            problems.append(
                Mismatch(
                    f"catalog.json {path}",
                    f"{values} != canonical {canonical}"
                    + _legacy_note(set(values) - canonical_set),
                )
            )
    if not catalog_enums():
        problems.append(Mismatch("catalog.json", "declares no `confidence` enum at all"))

    web = web_union_values()
    if web != canonical:
        problems.append(
            Mismatch(
                "apps/web/src/a2ui/confidence.ts CONFIDENCE",
                f"{web} != canonical {canonical}" + _legacy_note(set(web) - canonical_set),
            )
        )

    # Unordered: these are branch labels and dict keys, where order is
    # meaningless and only coverage matters.
    for where, keys in (
        ("apps/web/src/a2ui/confidence.ts confidenceTone", web_switch_cases()),
        ("apps/web/.../GroundingSurface.tsx CONFIDENCE_STYLES", grounding_surface_keys()),
        ("aleph_wiki/html_compiler.py _CONF_STYLE", html_compiler_keys()),
    ):
        missing = canonical_set - keys
        extra = keys - canonical_set
        if missing:
            problems.append(
                Mismatch(where, f"no branch for {sorted(missing)} — these render as the default")
            )
        if extra:
            problems.append(
                Mismatch(where, f"branches for {sorted(extra)}, which the enum cannot produce")
            )
    return problems


def _legacy_note(unknown: set[str]) -> str:
    if not unknown:
        return ""
    return f" (not in the vocabulary: {sorted(unknown)})"


def run() -> int:
    """Print findings; return the process exit code."""
    problems = compare()
    for problem in problems:
        print(f"✗ {problem}")
    if problems:
        print(
            "\nOne vocabulary. Edit packages/aleph-core/src/aleph_core/confidence.py, then "
            "packages/aleph-a2ui/src/aleph_a2ui/catalog.json + `uv run python "
            "scripts/gen_catalog.py`, apps/web/src/a2ui/confidence.ts, "
            "apps/web/src/a2ui/components/GroundingSurface.tsx and "
            "packages/aleph-wiki/src/aleph_wiki/html_compiler.py."
        )
        return 1
    sources = len(catalog_enums()) + 4  # + the TS union, the switch, the two badge maps
    n = len(canonical_values())
    print(f"✓ confidence vocabulary: {n} states, {sources} readers, all in agreement")
    return 0
