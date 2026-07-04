"""Upload connector re-registered in the Inc 3 framework.

The Inc 1 Upload connector lives in `aleph_wiki/connectors/upload.py`
because it's an in-process push from the API layer. Inc 3 re-registers
it under the unified ConnectorBase Protocol so the connector registry
sees it as a peer of the other connectors.

The Inc 1 implementation continues to be used by `POST /sources/upload`;
this module is a thin Protocol-conforming wrapper for the registry's view.
"""

from aleph_connectors.upload.register import UploadConnectorAdapter

__all__ = ["UploadConnectorAdapter"]
