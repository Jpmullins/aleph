"""Result types for the scholar package.

Frozen dataclasses (spec WP-2 §1). `ConsensusResult` is a tagged result —
quota exhaustion and reconnect-required are ordinary return values, not
exceptions, so the route layer never uses exceptions for control flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class DoiVerdict:
    """Tri-state verification verdict for a single DOI.

    ``ok`` is ``False`` only when BOTH Crossref and OpenAlex answered an
    authoritative 404; any network failure downgrades to ``None``
    (network-unverifiable — consumers must not flag it).
    """

    doi: str  # normalized lowercase, no https://doi.org/ prefix
    ok: bool | None  # True=resolves, False=authoritative 404 on both, None=unverifiable
    retracted: bool | None  # True=retraction detected, False=verified-not, None=unverifiable
    title: str | None
    year: int | None
    openalex_id: str | None
    checked_via: str  # "crossref+openalex" | "crossref" | "openalex"


@dataclass(frozen=True)
class WorkRef:
    """A scholarly work reference (Crossref or OpenAlex provenance)."""

    doi: str | None
    openalex_id: str | None
    title: str
    year: int | None
    venue: str | None
    authors: list[str] = field(default_factory=list[str])
    cited_by_count: int | None = None
    # Open-access locations (OpenAlex `best_oa_location` / `primary_location`).
    # Ingest should prefer pdf_url — paywalled landing pages serve bots empty
    # documents and fail normalization.
    pdf_url: str | None = None
    landing_url: str | None = None


@dataclass(frozen=True)
class CitationExpansion:
    """Citation-graph neighborhood of a work.

    ``backward`` = works it cites (referenced_works); ``forward`` = works
    that cite it (OpenAlex ``cites:`` filter).
    """

    backward: list[WorkRef] = field(default_factory=list[WorkRef])
    forward: list[WorkRef] = field(default_factory=list[WorkRef])


@dataclass(frozen=True)
class ConsensusHit:
    """One search hit from the Consensus MCP `search` tool."""

    title: str
    url: str
    doi: str | None = None
    snippet: str | None = None


ConsensusStatus = Literal["ok", "quota_exhausted", "reconnect_required"]


@dataclass(frozen=True)
class ConsensusResult:
    """Tagged result of a Consensus search.

    ``status == "ok"`` carries hits; ``"quota_exhausted"`` means the monthly
    cap was reached (no upstream call was made); ``"reconnect_required"``
    means the stored OAuth grant is dead and the user must re-run the
    connect bootstrap.
    """

    status: ConsensusStatus
    hits: list[ConsensusHit] = field(default_factory=list[ConsensusHit])
    message: str | None = None

    @property
    def quota_exhausted(self) -> bool:
        return self.status == "quota_exhausted"

    @property
    def reconnect_required(self) -> bool:
        return self.status == "reconnect_required"
