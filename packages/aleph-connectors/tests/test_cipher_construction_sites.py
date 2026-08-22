"""No cipher is ever built from the agent-token signing secret, checked by AST.

docs/plan.md Part 4 correction #21 replaces the original criterion
(`grep -rn 'aleph_agent_token_secret' apps/.../routes/ | wc -l == 0`) because
that grep returns nine hits in `routes/` and seven of them are the secret doing
its correct job — minting agent tokens. Driving it to zero would mean removing
agent-token minting from the API.

The thing that must be zero is narrower and is a property of the *call site*,
not of the file: nowhere may a credential cipher take its master key from the
signing secret. That is a shape in the syntax tree, so it is checked as one.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAN_ROOTS = (REPO_ROOT / "apps", REPO_ROOT / "packages")

FACTORY = "credential_cipher"
CIPHER_CLASS = "LibsodiumSealedBoxCipher"
#: Only the module that defines the cipher may construct it directly.
CIPHER_HOME = "packages/aleph-connectors/src/aleph_connectors/credentials.py"
#: The setting a master key is allowed to come from, and nothing else.
MASTER_SETTING = "aleph_credential_master_key"
FORBIDDEN_SOURCE = "agent_token_secret"

#: Call sites that read a settings object. Each must pass the master key
#: straight off it, so a rename of the setting breaks the test rather than
#: silently reading `None`.
SETTINGS_CALL_SITES = (
    "apps/api/src/aleph_api/routes/scholar.py",
    "apps/api/src/aleph_api/routes/connector_credentials.py",
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


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        named = (isinstance(func, ast.Name) and func.id == name) or (
            isinstance(func, ast.Attribute) and func.attr == name
        )
        if named:
            out.append(node)
    return out


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def test_the_cipher_class_is_constructed_only_where_it_is_defined() -> None:
    """Three call sites each built their own `LibsodiumSealedBoxCipher`, and
    that is how three of them came to disagree about the master secret. The
    factory is now the only door in, so there is one place to get it wrong."""
    offenders: list[str] = []
    for path in _python_files():
        rel = str(path.relative_to(REPO_ROOT))
        if rel == CIPHER_HOME or "/tests/" in f"/{rel}":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls_named(tree, CIPHER_CLASS):
            offenders.append(f"{rel}:{call.lineno}")
    assert offenders == [], (
        f"{CIPHER_CLASS} must be constructed only in {CIPHER_HOME}; use "
        f"{FACTORY}(master_key=...) instead. Offenders: {offenders}"
    )


def test_no_cipher_takes_its_master_key_from_the_signing_secret() -> None:
    """The defect, stated as a tree shape.

    `legacy_key=` is deliberately exempt: v1 rows really were encrypted from the
    agent-token secret, so reading them requires it. Writing with it is what
    must never happen again.
    """
    offenders: list[str] = []
    for path in _python_files():
        rel = str(path.relative_to(REPO_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in [*_calls_named(tree, FACTORY), *_calls_named(tree, CIPHER_CLASS)]:
            for key in ("master_key", "master_secret"):
                value = _kwarg(call, key)
                if value is None:
                    continue
                if FORBIDDEN_SOURCE in ast.unparse(value):
                    offenders.append(f"{rel}:{call.lineno} {key}={ast.unparse(value)}")
    assert offenders == [], (
        "a credential cipher must never be keyed by the agent-token signing "
        "secret — rotating that secret would destroy every stored credential. "
        f"Offenders: {offenders}"
    )


def test_settings_call_sites_pass_the_credential_master_key_setting() -> None:
    """The positive half. Absence of the wrong source is not presence of the
    right one: a call site that passed a literal, or a differently-named
    setting, would pass the negative test above."""
    for rel in SETTINGS_CALL_SITES:
        path = REPO_ROOT / rel
        assert path.exists(), f"{rel} does not exist — update SETTINGS_CALL_SITES"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = _calls_named(tree, FACTORY)
        assert calls, f"{rel} no longer builds a credential cipher via {FACTORY}"
        for call in calls:
            value = _kwarg(call, "master_key")
            assert value is not None, f"{rel}:{call.lineno} calls {FACTORY} without master_key="
            rendered = ast.unparse(value)
            assert rendered.endswith(MASTER_SETTING), (
                f"{rel}:{call.lineno} passes master_key={rendered}; it must come "
                f"from settings.{MASTER_SETTING}"
            )


def test_the_worker_binder_does_not_key_credentials_on_the_token_secret() -> None:
    """`resolve_bound_tools` still takes `agent_token_secret` — it is the v1 read
    key and its callers live in another workstream — so the check that matters
    is where that argument goes: into `legacy_read_key`, never into
    `master_key`."""
    rel = "packages/aleph-research/src/aleph_research/tools.py"
    tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
    calls = _calls_named(tree, FACTORY)
    assert calls, f"{rel} no longer builds a credential cipher via {FACTORY}"
    for call in calls:
        master = _kwarg(call, "master_key")
        assert master is not None
        assert "credential_master_key" in ast.unparse(master), (
            f"{rel}:{call.lineno} passes master_key={ast.unparse(master)}"
        )
        legacy = _kwarg(call, "legacy_key")
        assert legacy is not None and "legacy_read_key" in ast.unparse(legacy), (
            f"{rel}:{call.lineno} must resolve its v1 read key through "
            "legacy_read_key, so the fallback rule lives in one place"
        )
