"""Aleph connectors: framework + the Inc 3 plugin roster.

Connectors are typed source-kind plugins. The document-output research
set is registered into the shared registry at worker startup and feeds
the native research loop, bound per-project by the analyst's allowlist.
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
