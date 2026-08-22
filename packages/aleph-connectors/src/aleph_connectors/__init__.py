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
    ReencryptReport,
    credential_cipher,
    credential_cipher_from_env,
)
from aleph_connectors.keys import (
    CURRENT_KEY_VERSION,
    LEGACY_KEY_VERSION,
    MasterKeyRejected,
    derive_project_key,
)
from aleph_connectors.models import SynthesisProposal
from aleph_connectors.registry import ConnectorRegistry, get_registry

__all__ = [
    "CURRENT_KEY_VERSION",
    "LEGACY_KEY_VERSION",
    "ConnectorBase",
    "ConnectorContext",
    "ConnectorCredential",
    "ConnectorCredentialService",
    "ConnectorRegistry",
    "ConnectorResult",
    "CredentialCipher",
    "LibsodiumSealedBoxCipher",
    "MasterKeyRejected",
    "NotSupported",
    "RawPayload",
    "ReencryptReport",
    "SearchQuery",
    "SynthesisProposal",
    "credential_cipher",
    "credential_cipher_from_env",
    "derive_project_key",
    "get_registry",
]
