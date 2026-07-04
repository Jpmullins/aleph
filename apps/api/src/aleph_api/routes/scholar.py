"""Scholar API (WP-2 §4): DOI verification, scholarly search, citation
expansion, and Consensus evidence search.

All four endpoints are read-only POSTs (the body is a query, not a
mutation), so — like the other project GET routes — project membership via
`ProjectScopeDep` is the only gate; no role check beyond it.

`consensus-search` additionally enforces the project's `ConnectorBinding`
for the `consensus` connector (disabled → 403 `connector_disabled`,
mirroring the aiq_internal credential callback) and binds the Consensus
OAuth credential load/save callbacks to `ConnectorCredentialService`
per-request. A refresh-token rotation persists through the service's
ledgered rotate/upsert path — plaintext never appears in ledger payloads.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, cast

import structlog
from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_connectors.credentials import (
    ConnectorCredentialService,
    LibsodiumSealedBoxCipher,
)
from aleph_core.errors import GatewayUnavailable, NotFound, PermissionDenied
from aleph_rks.models import Connector, ConnectorBinding
from aleph_scholar import (
    ConsensusClient,
    ConsensusReconnectRequired,
    ScholarService,
    ScholarUpstreamError,
)
from aleph_scholar.types import DoiVerdict, WorkRef

router = APIRouter(prefix="/v1/projects", tags=["scholar"])

_log = structlog.get_logger(__name__)

_RECONNECT_DETAIL = (
    "Consensus authorization is not usable — reconnect with scripts/connect-consensus.py."
)


def _scholar(request: Request) -> ScholarService:
    return request.app.state.scholar


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DoiVerdictOut(BaseModel):
    doi: str
    ok: bool | None
    retracted: bool | None
    title: str | None
    year: int | None
    openalex_id: str | None
    checked_via: str

    @classmethod
    def from_verdict(cls, v: DoiVerdict) -> DoiVerdictOut:
        return cls(
            doi=v.doi,
            ok=v.ok,
            retracted=v.retracted,
            title=v.title,
            year=v.year,
            openalex_id=v.openalex_id,
            checked_via=v.checked_via,
        )


class VerifyDoisIn(BaseModel):
    dois: list[str] = Field(min_length=1, max_length=200)


class VerifyDoisOut(BaseModel):
    verdicts: list[DoiVerdictOut]


class WorkRefOut(BaseModel):
    doi: str | None
    openalex_id: str | None
    title: str
    year: int | None
    venue: str | None
    authors: list[str]
    cited_by_count: int | None
    pdf_url: str | None
    landing_url: str | None

    @classmethod
    def from_ref(cls, w: WorkRef) -> WorkRefOut:
        return cls(
            doi=w.doi,
            openalex_id=w.openalex_id,
            title=w.title,
            year=w.year,
            venue=w.venue,
            authors=list(w.authors),
            cited_by_count=w.cited_by_count,
            pdf_url=w.pdf_url,
            landing_url=w.landing_url,
        )


class ScholarSearchIn(BaseModel):
    provider: Literal["openalex", "crossref"]
    query: str = Field(min_length=1, max_length=2048)
    limit: int = Field(10, ge=1, le=50)


class ScholarSearchOut(BaseModel):
    works: list[WorkRefOut]


class ExpandCitationsIn(BaseModel):
    ref: str = Field(min_length=1, max_length=512)
    direction: Literal["backward", "forward", "both"] = "both"
    limit: int = Field(25, ge=1, le=100)


class ExpandCitationsOut(BaseModel):
    backward: list[WorkRefOut]
    forward: list[WorkRefOut]


class ConsensusSearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=2048)
    filters: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/{project_id}/scholar/verify-dois", response_model=VerifyDoisOut)
async def verify_dois(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[VerifyDoisIn, Body()],
) -> VerifyDoisOut:
    """Tri-state DOI verification (Crossref + OpenAlex, batched).

    Network trouble never 5xxs here: `verify_dois` folds it into
    `ok=None` verdicts (network-unverifiable — consumers must not flag).
    """
    verdicts = await _scholar(request).verify_dois(body.dois)
    return VerifyDoisOut(verdicts=[DoiVerdictOut.from_verdict(v) for v in verdicts])


@router.post("/{project_id}/scholar/search", response_model=ScholarSearchOut)
async def search(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[ScholarSearchIn, Body()],
) -> ScholarSearchOut:
    """Bibliographic search — OpenAlex (discovery) or Crossref (metadata)."""
    svc = _scholar(request)
    try:
        if body.provider == "openalex":
            works = await svc.search_openalex(body.query, per_page=body.limit)
        else:
            works = await svc.crossref_lookup(body.query, rows=body.limit)
    except ScholarUpstreamError as exc:
        raise GatewayUnavailable(str(exc)) from exc
    return ScholarSearchOut(works=[WorkRefOut.from_ref(w) for w in works])


@router.post("/{project_id}/scholar/expand-citations", response_model=ExpandCitationsOut)
async def expand_citations(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[ExpandCitationsIn, Body()],
) -> ExpandCitationsOut:
    """Citation-graph neighborhood of a DOI / OpenAlex id.

    `backward` = works it cites; `forward` = works that cite it.
    """
    try:
        expansion = await _scholar(request).expand_citations(
            body.ref, direction=body.direction, limit=body.limit
        )
    except ScholarUpstreamError as exc:
        raise GatewayUnavailable(str(exc)) from exc
    return ExpandCitationsOut(
        backward=[WorkRefOut.from_ref(w) for w in expansion.backward],
        forward=[WorkRefOut.from_ref(w) for w in expansion.forward],
    )


def _credential_service(request: Request, session: Any) -> ConnectorCredentialService:
    secret = request.app.state.settings.aleph_agent_token_secret.encode("utf-8")
    master = secret if len(secret) >= 32 else secret.ljust(32, b"0")
    cipher = LibsodiumSealedBoxCipher(master_secret=master)
    # No dev-default fallback: the Consensus OAuth blob is always
    # project-specific (bootstrapped by scripts/connect-consensus.py).
    return ConnectorCredentialService(session, cipher=cipher)


def _reconnect_response(message: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"status": "reconnect_required", "message": message or _RECONNECT_DETAIL},
    )


@router.post("/{project_id}/scholar/consensus-search")
async def consensus_search(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[ConsensusSearchIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> JSONResponse:
    """Quota-metered Consensus evidence search over MCP.

    Tagged results map to HTTP: `ok` → 200 `{status, hits}`;
    `quota_exhausted` → 200 (no upstream call was made); a dead OAuth grant
    → 409 `{status: "reconnect_required"}`. A disabled project binding for
    the `consensus` connector → 403 `connector_disabled` (per-project
    scoping by binding, mirroring the aiq_internal enforcement).
    """
    connector = (
        await session.execute(select(Connector).where(Connector.kind == "consensus"))
    ).scalar_one_or_none()
    if connector is None:
        msg = "unknown connector: consensus"
        raise NotFound(msg)
    binding = (
        await session.execute(
            select(ConnectorBinding).where(
                ConnectorBinding.project_id == project_id,
                ConnectorBinding.connector_id == connector.id,
            )
        )
    ).scalar_one_or_none()
    enabled = binding.enabled if binding is not None else connector.enabled_by_default
    if not enabled:
        msg = "connector_disabled"
        raise PermissionDenied(msg)

    svc = _credential_service(request, session)
    connector_id = connector.id

    async def _load_credential() -> dict[str, Any]:
        plaintext = await svc.decrypt_for_callback(
            project_id=project_id,
            connector_id=connector_id,
            connector_kind="consensus",
        )
        try:
            parsed: Any = json.loads(plaintext)
        except ValueError as exc:
            msg = "consensus credential blob is not valid JSON"
            raise ConsensusReconnectRequired(msg) from exc
        if not isinstance(parsed, dict):
            msg = "consensus credential blob is not a JSON object"
            raise ConsensusReconnectRequired(msg)
        return cast("dict[str, Any]", parsed)

    async def _save_credential(blob: dict[str, Any]) -> None:
        # Rotation on refresh (spec WP-2 §3): re-upsert the full blob through
        # the credential service with the acting principal — `rotated_at`
        # bumped, `connector_credential.*` ledger event in this transaction.
        await svc.rotate(
            ledger=ledger,
            principal=principal,
            project_id=project_id,
            connector_id=connector_id,
            connector_kind="consensus",
            new_plaintext=json.dumps(blob),
        )
        # Commit NOW: the AS has already invalidated the old one-time-use
        # refresh token. If the MCP search after this raises and the request
        # transaction rolls back, the rotated token would be lost and the
        # credential bricked (reconnect required). The rotation is a fact
        # regardless of how the rest of this request fares.
        await session.commit()

    settings = request.app.state.settings
    client = ConsensusClient(
        project_id=str(project_id),
        redis=request.app.state.redis,
        load_credential=_load_credential,
        save_credential=_save_credential,
        token_http=request.app.state.consensus_token_http,
        monthly_cap=settings.aleph_consensus_monthly_search_cap,
    )
    try:
        result = await client.search(body.query, filters=body.filters)
    except ConsensusReconnectRequired as exc:
        return _reconnect_response(str(exc) or None)
    except NotFound:
        # No stored credential at all — the analyst has not connected yet.
        return _reconnect_response(
            "No Consensus credential for this project — run scripts/connect-consensus.py."
        )
    except ScholarUpstreamError as exc:
        _log.warning(
            "consensus search upstream failure",
            project_id=str(project_id),
            error=str(exc)[:500],
        )
        raise GatewayUnavailable(str(exc)) from exc

    if result.reconnect_required:
        return _reconnect_response(result.message)
    return JSONResponse(
        status_code=200,
        content={
            "status": result.status,
            "hits": [
                {"title": h.title, "url": h.url, "doi": h.doi, "snippet": h.snippet}
                for h in result.hits
            ],
            "message": result.message,
        },
    )
