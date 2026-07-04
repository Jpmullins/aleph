"""Lens.org connector — registered but disabled by default (credential pending).

When the operator provides `LENS_API_KEY`, the connector becomes usable.
The registration exists so the research-loop registry sees it; the
default ConnectorBinding has `enabled=False`.
"""

from aleph_connectors.lens.register import LensConnector, LensMetadata

__all__ = ["LensConnector", "LensMetadata"]
