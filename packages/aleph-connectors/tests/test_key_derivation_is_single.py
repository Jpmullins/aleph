"""The credential-key derivation exists exactly once, checked by AST.

docs/plan.md Part 4 correction #22 replaces the original grep
(`grep -rn 'sha256(.*master' | wc -l == 1`) for a reason worth restating: a grep
cannot tell a comment from code. Reaching 1 that way meant deleting a docstring
line, a fourth copy hidden inside a comment would have satisfied it, and a
legitimate rename would have broken it.

This walks the syntax tree instead. It is the check that would have caught the
original defect: four copies of `sha256(master || project_id)` that had drifted
apart on padding while each one looked correct on its own.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAN_ROOTS = (REPO_ROOT / "apps", REPO_ROOT / "packages")

#: The one function allowed to derive a credential key.
DERIVATION = "derive_project_key"
DERIVATION_MODULE = "aleph_connectors.keys"

#: The three modules that each carried their own copy before WS-P7. Named
#: explicitly rather than discovered: the point is that these specific files
#: reach the shared implementation, and a check over "whatever exists today"
#: would pass just as happily if one of them were deleted.
FORMER_CALL_SITES = (
    "apps/api/src/aleph_api/routes/scholar.py",
    "apps/api/src/aleph_api/routes/connector_credentials.py",
    "packages/aleph-research/src/aleph_research/tools.py",
)

#: Names the shared key module publishes. A former call site must import at
#: least one of them — that is what "reaches the single implementation" means
#: in a tree, as opposed to happening not to contain a sha256 call today.
SHARED_KEY_NAMES = frozenset(
    {
        "credential_cipher",
        "credential_cipher_from_env",
        "derive_project_key",
        "legacy_read_key",
        "master_key_bytes",
    }
)

_SKIP_PARTS = frozenset({".venv", "__pycache__", "node_modules", "versions", "build", "dist"})


def _python_files() -> list[Path]:
    out: list[Path] = []
    for root in SCAN_ROOTS:
        for path in root.rglob("*.py"):
            if _SKIP_PARTS & set(path.parts):
                continue
            out.append(path)
    return sorted(out)


def _mentions_master(node: ast.AST) -> bool:
    """Does any identifier in this subtree name a master secret?"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and "master" in sub.id.lower():
            return True
        if isinstance(sub, ast.Attribute) and "master" in sub.attr.lower():
            return True
    return False


def _is_sha256_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "sha256":
        return True
    return isinstance(func, ast.Name) and func.id == "sha256"


def _functions_deriving_from_a_master_secret() -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our code
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for sub in ast.walk(node):
                if _is_sha256_call(sub) and any(
                    _mentions_master(a) for a in [*sub.args, *(k.value for k in sub.keywords)]
                ):
                    found.append((str(path.relative_to(REPO_ROOT)), node.name, node.lineno))
                    break
    return found


def test_exactly_one_function_derives_a_key_from_a_master_secret() -> None:
    found = _functions_deriving_from_a_master_secret()
    assert len(found) == 1, (
        "the credential-key derivation must exist exactly once; found "
        f"{len(found)}: {found}. Call aleph_connectors.keys.derive_project_key "
        "instead of writing a second copy — four copies that had drifted apart "
        "on padding is the defect WS-P7 exists to remove."
    )
    rel, name, _ = found[0]
    assert name == DERIVATION, f"the one derivation should be {DERIVATION}, found {name}"
    assert rel.endswith("aleph_connectors/keys.py"), (
        f"the derivation belongs in {DERIVATION_MODULE} — the package that owns "
        f"the cipher — not in {rel}"
    )


#: Every module that handles credential key material. `.ljust(...)` on a secret
#: is the original defect in one call — it ran BEFORE the cipher's own length
#: guard, so the guard was unreachable — and none of these may contain one.
KEY_HANDLING_MODULES = (
    "packages/aleph-connectors/src/aleph_connectors/keys.py",
    "packages/aleph-connectors/src/aleph_connectors/credentials.py",
    "packages/aleph-connectors/src/aleph_connectors/reencrypt.py",
    *FORMER_CALL_SITES,
)


def test_no_key_handling_module_pads_a_secret_to_length() -> None:
    """AST, not grep — and this file is why.

    The first version of this check was `"ljust(32" in source`, and it failed on
    three files whose only mention of it was a comment explaining the defect.
    That is the same weakness Part 4 correction #22 identifies in the criterion's
    original grep, discovered the hard way.
    """
    offenders: list[str] = []
    for rel in KEY_HANDLING_MODULES:
        path = REPO_ROOT / rel
        assert path.exists(), f"{rel} does not exist — update KEY_HANDLING_MODULES"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"ljust", "rjust", "zfill"}
            ):
                offenders.append(f"{rel}:{node.lineno} .{node.func.attr}()")
    assert offenders == [], (
        f"key material must not be padded to length; {offenders} does. "
        "A short key is refused at boot instead — padding is what made the "
        "cipher's own >= 32 bytes guard unreachable."
    )


def test_the_three_former_call_sites_reach_the_shared_implementation() -> None:
    """Each imports a shared key function from `aleph_connectors` by name.

    The negative half of this ("contains no sha256") is already covered by the
    uniqueness test above; this is the positive half, and without it a call site
    could satisfy that test by simply not encrypting anything any more.
    """
    for rel in FORMER_CALL_SITES:
        path = REPO_ROOT / rel
        assert path.exists(), f"{rel} does not exist — update FORMER_CALL_SITES"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "aleph_connectors"
            ):
                imported.update(alias.name for alias in node.names)
        assert imported & SHARED_KEY_NAMES, (
            f"{rel} must obtain its cipher from the shared key module — it "
            f"imports {sorted(imported)} from aleph_connectors, none of which is "
            f"one of {sorted(SHARED_KEY_NAMES)}"
        )
