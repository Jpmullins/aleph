"""Aleph connectors: framework + the Inc 3 plugin roster.

Connectors are typed source-kind plugins. Each one is also registered
inside AIQ's `data_source_registry` so the AIQ DeepResearcher can pick
from the same set the analyst allowlists.
"""

from aleph_connectors.base import (
    ConnectorBase,
    ConnectorContext,
    ConnectorResult,
    NotSupported,
    RawPayload,
    SearchQuery,
)
from aleph_connectors.credentials import (
    ConnectorCredential,
    ConnectorCredentialService,
    CredentialCipher,
    LibsodiumSealedBoxCipher,
)
from aleph_connectors.models import SynthesisProposal
from aleph_connectors.registry import ConnectorRegistry, get_registry

__all__ = [
    "ConnectorBase",
    "ConnectorContext",
    "ConnectorCredential",
    "ConnectorCredentialService",
    "ConnectorRegistry",
    "ConnectorResult",
    "CredentialCipher",
    "LibsodiumSealedBoxCipher",
    "NotSupported",
    "RawPayload",
    "SearchQuery",
    "SynthesisProposal",
    "get_registry",
]
