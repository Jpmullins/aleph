"""Connector registry.

In-process registry keyed by `kind`. AIQ's `data_source_registry` is
populated from this when the AIQ config is emitted (see aleph_aiq).
"""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aleph_connectors.base import ConnectorBase


class ConnectorRegistry:
    def __init__(self) -> None:
        self._lock = RLock()
        self._by_kind: dict[str, ConnectorBase] = {}

    def register(self, connector: ConnectorBase) -> None:
        with self._lock:
            kind = getattr(connector, "kind", None)
            if not isinstance(kind, str) or not kind:
                msg = f"connector missing kind: {connector!r}"
                raise ValueError(msg)
            self._by_kind[kind] = connector

    def get(self, kind: str) -> ConnectorBase | None:
        return self._by_kind.get(kind)

    def all(self) -> dict[str, ConnectorBase]:
        return dict(self._by_kind)

    def kinds(self) -> list[str]:
        return sorted(self._by_kind.keys())


_registry: ConnectorRegistry | None = None


def get_registry() -> ConnectorRegistry:
    global _registry
    if _registry is None:
        _registry = ConnectorRegistry()
    return _registry
