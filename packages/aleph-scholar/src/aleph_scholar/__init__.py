"""Verified scholarship for Aleph — pure-HTTP, zero LLM calls.

DOI verification (Crossref + OpenAlex, tri-state verdicts), retraction
detection, citation-graph expansion, deterministic citation style pass, and
Consensus evidence search over MCP with OAuth refresh + monthly quota.
"""

from aleph_scholar.consensus import (
    CONSENSUS_MCP_URL,
    ConsensusClient,
    ConsensusSearchFn,
    CredentialLoader,
    CredentialSaver,
    RedisLike,
    parse_search_result,
)
from aleph_scholar.crossref import CrossrefClient, CrossrefWork
from aleph_scholar.dois import extract_dois, normalize_doi, verify_dois
from aleph_scholar.errors import ConsensusReconnectRequired, ScholarUpstreamError
from aleph_scholar.http import ScholarHttp
from aleph_scholar.openalex import OPENALEX_DOI_BATCH_SIZE, OpenAlexClient, OpenAlexWork
from aleph_scholar.service import ScholarService
from aleph_scholar.style import style_pass
from aleph_scholar.types import (
    CitationExpansion,
    ConsensusHit,
    ConsensusResult,
    ConsensusStatus,
    DoiVerdict,
    WorkRef,
)

__all__ = [
    "CONSENSUS_MCP_URL",
    "OPENALEX_DOI_BATCH_SIZE",
    "CitationExpansion",
    "ConsensusClient",
    "ConsensusHit",
    "ConsensusReconnectRequired",
    "ConsensusResult",
    "ConsensusSearchFn",
    "ConsensusStatus",
    "CredentialLoader",
    "CredentialSaver",
    "CrossrefClient",
    "CrossrefWork",
    "DoiVerdict",
    "OpenAlexClient",
    "OpenAlexWork",
    "RedisLike",
    "ScholarHttp",
    "ScholarService",
    "ScholarUpstreamError",
    "WorkRef",
    "extract_dois",
    "normalize_doi",
    "parse_search_result",
    "style_pass",
    "verify_dois",
]
