"""A bad credential master key stops the process, and says which one.

docs/plan.md Part 4 correction #20 (which it says applies to WS-P7's criterion 4
too) rejects `create_app()` as the probe: `create_app` constructs no `Settings`
at all, so it boots fine with a completely empty environment. The settings
object is built inside the lifespan, by `get_settings()`. So this drives
`Settings()` itself, in a real subprocess with a real environment, and separately
pins that the lifespan is what calls it.

Subprocess rather than an in-process `pytest.raises` because the claim is about
a process refusing to start — exit status, and a message an operator can read.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

_GOOD_KEY = "a" * 64
_PLACEHOLDER = "CHANGE-ME-run-openssl-rand-hex-32"

#: Everything else `Settings` requires, so the only variable under test is the
#: master key. Values are syntactically valid and point nowhere — nothing here
#: opens a connection.
_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://aleph:x@127.0.0.1:1/aleph",
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "LANGFUSE_HOST": "http://127.0.0.1:1",
    "LANGFUSE_PUBLIC_KEY": "pk",
    "LANGFUSE_SECRET_KEY": "sk",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1",
    "LITELLM_BASE_URL": "http://127.0.0.1:1",
    "INSIGHTS_LITELLM_API_KEY": "k",
    "ALEPH_API_INTERNAL_URL": "http://127.0.0.1:1",
    "ALEPH_AGENT_TOKEN_SECRET": "b" * 64,
    # `Settings` reads a `.env` from the working directory. The subprocess runs
    # in a tmp dir so a developer's real .env cannot supply the value under test
    # and turn a failing case green.
    "PATH": "/usr/bin:/bin",
}

_SETTINGS_MODULES = {
    "api": ("aleph_api.settings", "Settings"),
    "workers": ("aleph_workers.settings", "WorkerSettings"),
}


def _boot(
    module: str,
    cls: str,
    master_key: str | None,
    tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    env = dict(_BASE_ENV)
    if master_key is not None:
        env["ALEPH_CREDENTIAL_MASTER_KEY"] = master_key
    return subprocess.run(
        [sys.executable, "-c", f"from {module} import {cls}; {cls}(); print('BOOTED OK')"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=tmp_path,
        timeout=120,
    )


@pytest.mark.parametrize("app", sorted(_SETTINGS_MODULES))
@pytest.mark.parametrize(
    ("case", "value"),
    [
        ("missing", None),
        ("short", "short"),
        # 33 characters, so a length check alone lets it through — and it is
        # published in this repository, so every deployment that keeps it shares
        # one key.
        ("placeholder", _PLACEHOLDER),
    ],
)
def test_a_bad_master_key_refuses_to_boot_and_names_the_setting(
    app: str, case: str, value: str | None, tmp_path: Path
) -> None:
    module, cls = _SETTINGS_MODULES[app]
    proc = _boot(module, cls, value, tmp_path)
    assert proc.returncode != 0, (
        f"{app} booted with a {case} ALEPH_CREDENTIAL_MASTER_KEY: {proc.stdout}"
    )
    combined = proc.stdout + proc.stderr
    assert "aleph_credential_master_key" in combined.lower(), (
        f"the failure must name the setting an operator has to fix; got:\n{combined}"
    )


@pytest.mark.parametrize("app", sorted(_SETTINGS_MODULES))
def test_a_real_master_key_boots(app: str, tmp_path: Path) -> None:
    """The other direction. A guard that rejects everything is not a guard."""
    module, cls = _SETTINGS_MODULES[app]
    proc = _boot(module, cls, _GOOD_KEY, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "BOOTED OK" in proc.stdout


def test_the_lifespan_is_what_builds_settings() -> None:
    """Without this the subprocess tests above prove only that a class can be
    constructed badly, not that starting the API runs the check. `create_app()`
    deliberately does not build `Settings` — see Part 4 correction #20 — so the
    lifespan is the boot path, and this pins that it still is."""
    tree = ast.parse((REPO_ROOT / "apps/api/src/aleph_api/lifespan.py").read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "get_settings" in called, (
        "apps/api/src/aleph_api/lifespan.py no longer calls get_settings(), so "
        "nothing validates ALEPH_CREDENTIAL_MASTER_KEY at boot"
    )
