"""The boot manifest — where core capability and pins come from."""

from __future__ import annotations

from pathlib import Path

import pytest

from aleph_kernel.kernel import Kernel
from aleph_kernel.manifest import ManifestError, load_manifest, mount_manifest

pytestmark = pytest.mark.asyncio

GOOD = """
[[capability]]
name = "db"
factory = "aleph_kernel.testing:provider"

[[capability]]
name = "reports"
factory = "aleph_kernel.testing:consumer"
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "aleph.toml"
    path.write_text(body)
    return path


async def test_a_manifest_parses(tmp_path: Path) -> None:
    entries = load_manifest(write(tmp_path, GOOD))
    assert [e.name for e in entries] == ["db", "reports"]


async def test_loading_does_not_import_anything(tmp_path: Path) -> None:
    """Parsing a manifest must not run a factory — importing must not start things."""
    body = '[[capability]]\nname = "x"\nfactory = "nonexistent.module:thing"\n'
    entries = load_manifest(write(tmp_path, body))  # no ImportError
    assert entries[0].factory == "nonexistent.module:thing"


async def test_a_missing_manifest_is_an_error_not_an_empty_boot(tmp_path: Path) -> None:
    """Booting with no capability at all must be loud, not silently degraded."""
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path / "absent.toml")


async def test_an_empty_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="declares no"):
        load_manifest(write(tmp_path, "# nothing here\n"))


async def test_malformed_toml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="valid TOML"):
        load_manifest(write(tmp_path, "[[capability\nname ="))


async def test_a_factory_must_be_module_colon_callable(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="module:callable"):
        load_manifest(write(tmp_path, '[[capability]]\nname="x"\nfactory="notacallable"\n'))


async def test_a_duplicate_name_is_refused(tmp_path: Path) -> None:
    body = '[[capability]]\nname="x"\nfactory="m:f"\n[[capability]]\nname="x"\nfactory="m:g"\n'
    with pytest.raises(ManifestError, match="declared twice"):
        load_manifest(write(tmp_path, body))


async def test_a_factory_that_renames_itself_is_refused() -> None:
    """The manifest is the authority on what is mounted, not the factory."""
    from aleph_kernel.manifest import ManifestEntry

    # The factory produces a spec named "db"; the manifest declares "declared".
    entry = ManifestEntry(name="declared", factory="aleph_kernel.testing:provider")
    with pytest.raises(ManifestError, match="the manifest is the authority"):
        mount_manifest(Kernel(), (entry,))


async def test_manifest_capabilities_get_no_plugin_id(tmp_path: Path) -> None:
    """THE guardrail: core is mounted, and is therefore unaddressable."""
    from aleph_kernel.agent_api import AgentPluginAPI

    kernel = Kernel()
    mount_manifest(kernel, load_manifest(write(tmp_path, GOOD)))
    await kernel.boot()

    views = {v.name: v for v in AgentPluginAPI(kernel).inspect()}
    assert views["db"].protected and views["db"].plugin_id is None
    # An unprotected manifest entry is still core — it was not registered by an
    # agent, so it has no id either.
    assert views["reports"].plugin_id is None
