"""ScholarService — the package facade (spec WP-2 §1 public API).

Wires the shared polite HTTP transport, the Crossref/OpenAlex clients, and
(optionally) the Consensus MCP client. The service is DB-free: Consensus
credentials flow through injected load/save callbacks and quota/locking
through an injected redis client, so the API layer owns all persistence.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

import httpx

from aleph_scholar import dois as _dois
from aleph_scholar.consensus import (
    CONSENSUS_MCP_URL,
    ConsensusClient,
    ConsensusSearchFn,
    CredentialLoader,
    CredentialSaver,
    RedisLike,
)
from aleph_scholar.crossref import CrossrefClient
from aleph_scholar.http import ScholarHttp
from aleph_scholar.openalex import OpenAlexClient
from aleph_scholar.style import style_pass as _style_pass
from aleph_scholar.types import CitationExpansion, ConsensusResult, DoiVerdict, WorkRef

ExpandDirection = Literal["backward", "forward", "both"]


class ScholarService:
    """Facade over DOI verification, scholarly search, citations, Consensus."""

    def __init__(
        self,
        *,
        mailto: str,
        http: ScholarHttp | None = None,
        redis: RedisLike | None = None,
        consensus_monthly_cap: int = 200,
        load_credential: CredentialLoader | None = None,
        save_credential: CredentialSaver | None = None,
        consensus_token_http: httpx.AsyncClient | None = None,
        consensus_mcp_url: str = CONSENSUS_MCP_URL,
        consensus_search_fn: ConsensusSearchFn | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.http = http or ScholarHttp(mailto=mailto)
        self.openalex = OpenAlexClient(self.http)
        self.crossref = CrossrefClient(self.http)
        self._redis = redis
        self._consensus_monthly_cap = consensus_monthly_cap
        self._load_credential = load_credential
        self._save_credential = save_credential
        self._consensus_token_http = consensus_token_http
        self._consensus_mcp_url = consensus_mcp_url
        self._consensus_search_fn = consensus_search_fn
        self._now = now

    # -- pure functions (re-exposed for callers holding a service) ---------

    @staticmethod
    def extract_dois(text: str) -> list[str]:
        return _dois.extract_dois(text)

    @staticmethod
    def style_pass(markdown: str) -> str:
        return _style_pass(markdown)

    # -- verification -------------------------------------------------------

    async def verify_dois(self, dois: list[str]) -> list[DoiVerdict]:
        return await _dois.verify_dois(dois, openalex=self.openalex, crossref=self.crossref)

    # -- discovery ------------------------------------------------------------

    async def crossref_lookup(
        self, query: str, *, rows: int = 10, deadline_s: float | None = None
    ) -> list[WorkRef]:
        return await self.crossref.search_bibliographic(query, rows=rows, deadline_s=deadline_s)

    async def search_openalex(
        self, query: str, *, per_page: int = 10, deadline_s: float | None = None
    ) -> list[WorkRef]:
        return await self.openalex.search(query, per_page=per_page, deadline_s=deadline_s)

    async def expand_citations(
        self, ref: str, *, direction: ExpandDirection = "both", limit: int = 25
    ) -> CitationExpansion:
        backward: list[WorkRef] = []
        forward: list[WorkRef] = []
        if direction in ("backward", "both"):
            backward = await self.openalex.referenced_works(ref, limit=limit)
        if direction in ("forward", "both"):
            forward = await self.openalex.citing_works(ref, limit=limit)
        return CitationExpansion(backward=backward, forward=forward)

    # -- Consensus -----------------------------------------------------------

    def consensus_client(self, project_id: str) -> ConsensusClient:
        """Build the per-project Consensus client (requires injected deps)."""
        if self._redis is None or self._load_credential is None or self._save_credential is None:
            msg = (
                "Consensus is not configured on this ScholarService "
                "(redis + load_credential + save_credential are required)"
            )
            raise RuntimeError(msg)
        return ConsensusClient(
            project_id=project_id,
            redis=self._redis,
            load_credential=self._load_credential,
            save_credential=self._save_credential,
            token_http=self._consensus_token_http,
            monthly_cap=self._consensus_monthly_cap,
            mcp_url=self._consensus_mcp_url,
            search_fn=self._consensus_search_fn,
            now=self._now,
        )

    async def search_consensus(
        self, project_id: str, query: str, *, filters: dict[str, Any] | None = None
    ) -> ConsensusResult:
        return await self.consensus_client(project_id).search(query, filters=filters)
