"""The boot manifest — where "protected" comes from, and the only place.

The owner's requirement is that guardrails prevent the agent removing
load-bearing capability. The primary guard is not a check; it is the absence of
an addressable name.

Core capabilities are mounted from this manifest into the kernel **before any
session context exists**, and they never receive a ``PluginId``. The deactivate
API accepts nothing else. So deactivating core capability is not refused after
deliberation — it is unexpressible, because no argument value the agent can
construct names it. A refusal path exists (``ProtectedCapability``) purely to
catch a kernel-internal mistake.

``protected = true`` is settable only here. There is no API that sets it, no
column a plugin row can carry, and ``register_dynamic`` rejects a spec that
claims it. Three independent places would have to be wrong at once.

The manifest is also the loader's ONLY input. There is deliberately no
directory-scan fallback: a scan means a file appearing on disk becomes a booted
capability, which hands anything that can write a file the ability to mount
code at the trust level of core.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aleph_kernel.errors import KernelError

if TYPE_CHECKING:
    from aleph_kernel.kernel import Kernel
    from aleph_kernel.spec import CapabilitySpec

__all__ = ["ManifestEntry", "ManifestError", "load_manifest", "mount_manifest"]


class ManifestError(KernelError):
    """The manifest is malformed, or names something that cannot be mounted."""


@dataclass(frozen=True)
class ManifestEntry:
    """One capability the system boots with.

    ``factory`` is ``module:callable`` returning a ``CapabilitySpec``. A factory
    rather than a module-level object so a capability can close over settings
    without a module-import side effect — and so importing the manifest's
    modules cannot itself start anything.
    """

    name: str
    factory: str
    protected: bool = False
    config: dict[str, Any] | None = None


def load_manifest(path: Path) -> tuple[ManifestEntry, ...]:
    """Parse and validate a manifest. Does not import anything."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"boot manifest not found: {path}"
        raise ManifestError(msg) from exc
    except tomllib.TOMLDecodeError as exc:
        msg = f"boot manifest is not valid TOML: {exc}"
        raise ManifestError(msg) from exc

    rows = raw.get("capability")
    if not isinstance(rows, list) or not rows:
        msg = f"{path} declares no [[capability]] entries"
        raise ManifestError(msg)

    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            msg = f"[[capability]] #{index} is not a table"
            raise ManifestError(msg)
        name = row.get("name")
        factory = row.get("factory")
        if not isinstance(name, str) or not name.strip():
            msg = f"[[capability]] #{index} has no name"
            raise ManifestError(msg)
        if not isinstance(factory, str) or ":" not in factory:
            msg = f"{name!r} needs factory = 'module:callable', got {factory!r}"
            raise ManifestError(msg)
        if name in seen:
            msg = f"{name!r} is declared twice"
            raise ManifestError(msg)
        seen.add(name)
        entries.append(
            ManifestEntry(
                name=name,
                factory=factory,
                protected=bool(row.get("protected", False)),
                config=row.get("config"),
            )
        )
    return tuple(entries)


def _resolve(factory: str) -> Any:
    module_name, _, attr = factory.partition(":")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        msg = f"cannot import {module_name!r} for factory {factory!r}: {exc}"
        raise ManifestError(msg) from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        msg = f"{module_name!r} has no attribute {attr!r}"
        raise ManifestError(msg) from exc


def mount_manifest(kernel: Kernel, entries: tuple[ManifestEntry, ...], **kwargs: Any) -> None:
    """Register every manifest capability as core. None gets a ``PluginId``.

    ``kwargs`` are passed to any factory that accepts them, so a capability can
    take settings without the manifest carrying secrets.
    """
    import inspect

    for entry in entries:
        factory = _resolve(entry.factory)
        signature = inspect.signature(factory)
        accepted = {k: v for k, v in kwargs.items() if k in signature.parameters}
        spec: CapabilitySpec = factory(**accepted)
        if spec.name != entry.name:
            msg = (
                f"manifest declares {entry.name!r} but its factory produced "
                f"{spec.name!r}; the manifest is the authority on what is mounted"
            )
            raise ManifestError(msg)
        if entry.protected and not spec.protected:
            from dataclasses import replace

            spec = replace(spec, protected=True)
        kernel.register_core(spec)
