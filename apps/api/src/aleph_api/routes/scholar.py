"""Scholar API (WP-2 §4): DOI verification, scholarly search, citation
expansion, and Consensus evidence search.

All four endpoints are read-only POSTs (the body is a query, not a
mutation), so — like the other project GET routes — project membership via
`ProjectScopeDep` is the only gate; no role check beyond it.

Upstream failures are reported by *cause*, not by one blanket code. Every
non-2xx from OpenAlex or Crossref used to become `GatewayUnavailable` — HTTP
503, "the upstream service is unavailable" — including a 400 caused by a
filter Aleph itself built wrong. That tells an operator the internet is down
when the actual message is "your query syntax is wrong", and it is
unactionable in exactly the situation where the answer was one line away. The
split lives in `_UPSTREAM_STATUS_MAP` / `_upstream_response` below.

`consensus-search` additionally enforces the project's `ConnectorBinding`
for the `consensus` connector (disabled → 403 `connector_disabled`,
mirroring the in-process credential resolution) and binds the Consensus
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
from aleph_core.errors import NotFound, PermissionDenied
from aleph_rks.models import Connector, ConnectorBinding
from aleph_scholar import (
    ConsensusClient,
    ConsensusReconnectRequired,
    ScholarClientError,
    ScholarService,
    ScholarUnavailable,
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
# Upstream failure → HTTP status
# ---------------------------------------------------------------------------

#: Upstream 4xx → the status Aleph reports. Only these three are the *request's*
#: fault in a way the caller can act on, so only these three are echoed as-is:
#:
#:   400 — the upstream rejected the query (a filter Aleph built, a malformed
#:         DOI in the batch). The reason names the offending parameter.
#:   404 — the upstream has no such work. Search paths rarely see it; the
#:         single-work paths return `None` for it before `ensure_ok` runs.
#:   422 — the upstream parsed the request and refused the values.
_UPSTREAM_STATUS_MAP: dict[int, int] = {400: 400, 404: 404, 422: 422}

#: Every other upstream 4xx — 401/403 (a key or policy changed on their side),
#: 451, an upstream quirk. Not the caller's fault and not fixable by changing
#: the request, so it is not a 4xx *here*; but the upstream did answer, so it
#: is not "unavailable" either. 502 is the honest middle.
_UPSTREAM_DEFAULT_STATUS = 502

#: `Retry-After` when the upstream did not send one of its own. RFC 9110 allows
#: it on a 503 and clients back off on it; omitting the header entirely invites
#: an immediate retry into the same rate limit, which is how a 429 turns into a
#: block. Deliberately a plain number and not the request deadline — the
#: deadline is how long *this* request waited, not how long the upstream needs.
_DEFAULT_RETRY_AFTER_S = 30


def _upstream_response(
    request: Request, exc: ScholarUpstreamError, *, provider: str
) -> JSONResponse:
    """RFC 7807 problem detail for an upstream failure, split by cause.

    Returned rather than raised because a 503 must carry `Retry-After`, and
    `ErrorMiddleware._problem` builds a body with no headers. The body shape
    matches that middleware's so a client parses one thing either way.

    `str(exc)` carries the full upstream URL (query string included, which is
    the actionable part of a bad-filter 400) and goes to the log. The response
    body carries the upstream's own reason, without the URL — the caller does
    not need the deployment's `mailto=` echoed back at them.
    """
    if isinstance(exc, ScholarClientError):
        status = _UPSTREAM_STATUS_MAP.get(exc.status_code or 0, _UPSTREAM_DEFAULT_STATUS)
        code = "upstream_rejected"
        detail = (
            f"{provider} rejected the request (HTTP {exc.status_code}): {exc.reason}"
            if exc.reason
            else f"{provider} rejected the request (HTTP {exc.status_code})."
        )
        headers: dict[str, str] = {}
        _log.warning(
            "scholar upstream rejected the request",
            provider=provider,
            upstream_status=exc.status_code,
            error=str(exc)[:1000],
        )
    else:
        status = 503
        code = "upstream_unavailable"
        retry_after = exc.retry_after if isinstance(exc, ScholarUnavailable) else None
        seconds = max(1, round(retry_after)) if retry_after is not None else _DEFAULT_RETRY_AFTER_S
        detail = f"{provider} did not answer within the request budget — retry in {seconds}s."
        headers = {"Retry-After": str(seconds)}
        _log.warning(
            "scholar upstream unavailable",
            provider=provider,
            upstream_status=getattr(exc, "status_code", None),
            retry_after=seconds,
            error=str(exc)[:1000],
        )
    body: dict[str, Any] = {
        "type": f"about:blank#{code}",
        "title": code.replace("_", " ").capitalize(),
        "status": status,
        "detail": detail,
        "instance": str(request.url.path),
        "details": {"provider": provider, "upstream_status": exc.status_code},
    }
    return JSONResponse(
        body, status_code=status, media_type="application/problem+json", headers=headers
    )


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
    #: Wall-clock budget for the whole attempt sequence — rate-limit waiting,
    #: every retry, and every backoff between them. The retry budget used to be
    #: "three attempts", which is three of something whose duration nobody
    #: knows; a caller waiting on an interactive search and a caller running a
    #: batch have genuinely different answers, so the caller says.
    deadline_s: float | None = Field(None, ge=1.0, le=60.0)


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
) -> ScholarSearchOut | JSONResponse:
    """Bibliographic search — OpenAlex (discovery) or Crossref (metadata)."""
    svc = _scholar(request)
    try:
        if body.provider == "openalex":
            works = await svc.search_openalex(
                body.query, per_page=body.limit, deadline_s=body.deadline_s
            )
        else:
            works = await svc.crossref_lookup(
                body.query, rows=body.limit, deadline_s=body.deadline_s
            )
    except ScholarUpstreamError as exc:
        return _upstream_response(request, exc, provider=body.provider)
    return ScholarSearchOut(works=[WorkRefOut.from_ref(w) for w in works])


@router.post("/{project_id}/scholar/expand-citations", response_model=ExpandCitationsOut)
async def expand_citations(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[ExpandCitationsIn, Body()],
) -> ExpandCitationsOut | JSONResponse:
    """Citation-graph neighborhood of a DOI / OpenAlex id.

    `backward` = works it cites; `forward` = works that cite it.
    """
    try:
        expansion = await _scholar(request).expand_citations(
            body.ref, direction=body.direction, limit=body.limit
        )
    except ScholarUpstreamError as exc:
        return _upstream_response(request, exc, provider="openalex")
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
    scoping by binding, mirroring the research-loop binding enforcement).
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
        return _upstream_response(request, exc, provider="consensus")

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
