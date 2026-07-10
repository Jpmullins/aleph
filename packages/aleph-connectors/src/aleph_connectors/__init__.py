"""Aleph connectors: framework + the Inc 3 plugin roster.

Connectors are typed source-kind plugins. The native research loop binds
each project's enabled connectors by direct factory lookup
(``aleph_research.tools.RESEARCH_CONNECTOR_FACTORIES``), per-project by the
analyst's allowlist — there is no global in-process registry.
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

__all__ = [
    "ConnectorBase",
    "ConnectorContext",
    "ConnectorCredential",
    "ConnectorCredentialService",
    "ConnectorResult",
    "CredentialCipher",
    "LibsodiumSealedBoxCipher",
    "NotSupported",
    "RawPayload",
    "SearchQuery",
    "SynthesisProposal",
]
