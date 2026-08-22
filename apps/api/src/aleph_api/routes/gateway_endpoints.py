"""A project's gateway endpoints, over HTTP. WS-MEP-4's consumer half.

The table, the cipher, the resolver and the probe all landed and **nothing
called them** — `grep -rn "GatewayEndpointService" apps packages` outside the
module itself returned nothing at all. That is the producer-with-no-consumer
defect CLAUDE.md names as the dominant class in this codebase, reproduced
inside the workstream that set out to remove it. This file is the read path
that makes it real: five routes, and two of them replace a process-wide
singleton rather than adding a new surface beside it.

Three properties, each of which is a defect this repository has already shipped
in some other form:

* **The key never leaves the server.** No response model on this router carries
  the api key, at any nesting depth. `GatewayEndpointOut` reports
  `has_api_key`, `cipher_scheme` and `key_version` — enough to tell "no key
  configured" from "a key this deployment cannot open", and nothing more. The
  settings-card defect was exactly this: a field hidden on screen and written
  verbatim to two append-only tables.
* **The endpoint's own words survive.** `POST …/test` reports what the gateway
  actually said. "Connection failed" sends an operator looking for a network
  fault when the answer was "invalid api key" — and a wrong `base_url` and a
  wrong key are indistinguishable without the upstream text.
  `redact_secret` takes the one value we know is a secret back out of that
  text first, because some gateways echo the bearer token in a 401 body.
* **Reads are scoped, not just writes.** Every route resolves `ProjectScopeDep`
  and every single-row lookup goes through `GatewayEndpointService.get_by_id`,
  which filters on `project_id`. A route that loads by id alone hands another
  tenant's endpoint to whoever guesses a UUID, and answers 200 while doing it.

**Writes are OWNER.** So are reads: a base URL, a probe error and the shape of
somebody's model estate are operator data, and `connector_credentials.py` gates
its own list the same way for the same reason.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Request, status
from pydantic import BaseModel, ConfigDict, Field

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_connectors.credentials import credential_cipher
from aleph_core.schemas.model_profile import GatewayModelOut
from aleph_models.discovery import DiscoveredModel, capabilities_for
from aleph_models.endpoints import (
    GatewayEndpointService,
    ProjectGatewayCatalogs,
    ResolvedEndpoint,
)
from aleph_security.roles import ProjectRole, require_at_least

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_db.models.gateway_endpoint import GatewayEndpoint

router = APIRouter(prefix="/v1/projects", tags=["gateway-endpoints"])


# ---------------------------------------------------------------------------
# Schemas. Note what is absent from every one of them.
# ---------------------------------------------------------------------------


class GatewayEndpointIn(BaseModel):
    """Create-or-replace one endpoint, addressed by its name within the project."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=2048)
    #: Three distinct intentions, and all three have to stay expressible.
    #: Omitted (`None`) keeps whatever key the row already has — the operator
    #: cannot read it back, so an edit to the URL must not require retyping it.
    #: `""` clears it: a gateway on a private network needs no key. A value
    #: sets or rotates it.
    api_key: str | None = Field(default=None, max_length=8192)
    is_default: bool = False


class GatewayEndpointOut(BaseModel):
    """Everything about an endpoint except the one thing that must not leave.

    `has_api_key` + `key_version` rather than a masked prefix of the key. A
    hint is still a disclosure, and the two questions an operator actually has
    — "is a key set?" and "was it encrypted under a generation this deployment
    still holds?" — are both answered without one.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    base_url: str
    is_default: bool
    has_api_key: bool
    cipher_scheme: str | None
    key_version: str | None
    last_probe_at: datetime | None
    last_probe_ok: bool | None
    last_probe_error: str | None
    last_probe_model_count: int | None


class EndpointProbeOut(BaseModel):
    """What the endpoint said, in its own words."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    model_count: int
    #: `False` is the NORMAL answer for a LiteLLM virtual key restricted to
    #: `llm_api_routes`, and it is not a failure — it is why the model list
    #: carries ids and no metadata. `None` means the endpoint never answered.
    model_info_allowed: bool | None
    models: list[str]
    error: str | None
    endpoint: GatewayEndpointOut


class ResolvedEndpointOut(BaseModel):
    """Which gateway a project's calls go to, and whether it chose it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    endpoint_id: UUID | None
    #: `row` or `settings`. "This project chose this gateway" and "nobody has
    #: configured one" bill the same way and mean different things.
    source: str


class ProjectGatewayModelsOut(BaseModel):
    """The models THIS project's endpoint serves.

    An object rather than a bare list because the failure has to be sayable. An
    unreachable gateway returning `[]` is indistinguishable from a gateway
    serving nothing, and a model picker rendered from the first is an empty box
    with no explanation — which is exactly how the dead-embedder defect
    presented for weeks.
    """

    model_config = ConfigDict(extra="forbid")

    endpoint: ResolvedEndpointOut
    models: list[GatewayModelOut]
    error: str | None = None


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _service(request: Request, session: AsyncSession) -> GatewayEndpointService:
    """One cipher, built the one way. See `connector_credentials._cipher`.

    `credential_cipher` is the only cipher factory in the repo on purpose: three
    call sites used to derive their own master secret separately, which is a
    rotation story with three halves.
    """
    s = request.app.state.settings
    return GatewayEndpointService(
        session,
        cipher=credential_cipher(
            master_key=s.aleph_credential_master_key,
            legacy_key=s.credential_legacy_key,
        ),
    )


def gateway_catalogs(request: Request) -> ProjectGatewayCatalogs:
    """The process's per-endpoint catalog cache, created on first use.

    Lazily attached to `app.state` rather than published as a kernel capability
    because it holds no resource and needs no inverse — it is a bounded dict of
    cached model lists, and its one collaborator (the shared connection pool)
    is owned and closed by the `http` capability.

    `gateway_http` is read directly, and it is worth being precise about what
    that read is: a bare `httpx.AsyncClient()` with no `base_url`, no auth
    header and no gateway identity of any kind — a connection pool. It is not
    the process-wide *model client* MEP-4 exists to remove; sharing a pool
    across endpoints is correct and opening one per probe would not be.
    """
    existing = getattr(request.app.state, "project_gateway_catalogs", None)
    if isinstance(existing, ProjectGatewayCatalogs):
        return existing
    catalogs = ProjectGatewayCatalogs(client=request.app.state.gateway_http)
    request.app.state.project_gateway_catalogs = catalogs
    return catalogs


async def resolve_for_project(
    request: Request, session: AsyncSession, project_id: UUID
) -> ResolvedEndpoint:
    """Where this project's model calls go. A row if it has one, else Settings.

    Shared with `routes/model_profile.py` so that the model list an operator
    picks from and the gateway autoconfigure probes cannot come from two
    different places — which is the entire class of bug this route exists to
    close.
    """
    s = request.app.state.settings
    return await _service(request, session).resolve(
        project_id=project_id,
        fallback_base_url=s.litellm_base_url,
        fallback_api_key=s.insights_litellm_api_key,
    )


def _out(row: GatewayEndpoint) -> GatewayEndpointOut:
    return GatewayEndpointOut(
        id=row.id,
        name=row.name,
        base_url=row.base_url,
        is_default=row.is_default,
        has_api_key=row.api_key_cipher is not None,
        cipher_scheme=row.cipher_scheme,
        key_version=row.key_version,
        last_probe_at=row.last_probe_at,
        last_probe_ok=row.last_probe_ok,
        last_probe_error=row.last_probe_error,
        last_probe_model_count=row.last_probe_model_count,
    )


def _resolved_out(resolved: ResolvedEndpoint) -> ResolvedEndpointOut:
    return ResolvedEndpointOut(
        name=resolved.name,
        base_url=resolved.base_url,
        endpoint_id=resolved.endpoint_id,
        source=resolved.source,
    )


def model_out(model: DiscoveredModel) -> GatewayModelOut:
    """A `DiscoveredModel` in the shape the Settings picker already reads."""
    return GatewayModelOut(
        id=model.id,
        mode=model.mode,
        max_input_tokens=model.max_input_tokens,
        input_per_token=model.input_per_token,
        output_per_token=model.output_per_token,
        supports_vision=model.supports_vision,
        supports_function_calling=model.supports_function_calling,
        supports_reasoning=model.supports_reasoning,
        supports_prompt_caching=model.supports_prompt_caching,
        is_priced=model.is_priced,
        capabilities=capabilities_for(model),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/{project_id}/gateway-endpoints", response_model=list[GatewayEndpointOut])
async def list_endpoints(
    request: Request,
    project_id: ProjectScopeDep,
    session: SessionDep,
    principal: PrincipalDep,
) -> list[GatewayEndpointOut]:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    rows = await _service(request, session).list_for_project(project_id)
    return [_out(r) for r in rows]


@router.put("/{project_id}/gateway-endpoints", response_model=GatewayEndpointOut)
async def upsert_endpoint(
    request: Request,
    project_id: ProjectScopeDep,
    body: Annotated[GatewayEndpointIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> GatewayEndpointOut:
    """Create or replace the endpoint named in the body.

    `PUT` on the collection rather than on `…/{name}`, because the name is the
    natural key and putting it in the path would make renaming it look like
    editing it.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    row = await _service(request, session).upsert(
        ledger=ledger,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        project_id=project_id,
        name=body.name,
        base_url=body.base_url,
        api_key=body.api_key,
        is_default=body.is_default,
    )
    # `created_at`/`updated_at` have server-side defaults, so they are expired
    # after the flush; reading one then lazy-loads, and a lazy load on an async
    # session raises MissingGreenlet. Same refresh, same reason, as the three
    # model-profile write paths.
    await session.refresh(row)
    return _out(row)


@router.delete(
    "/{project_id}/gateway-endpoints/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_endpoint(
    request: Request,
    project_id: ProjectScopeDep,
    endpoint_id: UUID,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> None:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    await _service(request, session).delete(
        ledger=ledger,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        project_id=project_id,
        endpoint_id=endpoint_id,
    )


@router.post(
    "/{project_id}/gateway-endpoints/{endpoint_id}/test",
    response_model=EndpointProbeOut,
)
async def test_endpoint(
    request: Request,
    project_id: ProjectScopeDep,
    endpoint_id: UUID,
    session: SessionDep,
    principal: PrincipalDep,
) -> EndpointProbeOut:
    """Ask the endpoint what it serves, and report the answer verbatim.

    Answers **200 whether or not the gateway did**. The question is "what did it
    say", and a 502 carrying the upstream text would be indistinguishable at the
    client from Aleph itself failing — the operator would not know which of the
    two servers to go and look at.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    svc = _service(request, session)
    probe = await svc.probe(
        project_id=project_id,
        endpoint_id=endpoint_id,
        client=request.app.state.gateway_http,
    )
    row = await svc.get_by_id(project_id=project_id, endpoint_id=endpoint_id)
    return EndpointProbeOut(
        ok=probe.ok,
        model_count=probe.model_count,
        model_info_allowed=probe.model_info_allowed,
        models=list(probe.models),
        error=probe.error,
        endpoint=_out(row),
    )


@router.get("/{project_id}/gateway/models", response_model=ProjectGatewayModelsOut)
async def list_project_gateway_models(
    request: Request,
    project_id: ProjectScopeDep,
    session: SessionDep,
    principal: PrincipalDep,
    refresh: bool = False,
) -> ProjectGatewayModelsOut:
    """What THIS project's gateway serves.

    The un-scoped `GET /v1/gateway/models` answered from a catalog built once at
    boot out of `LITELLM_BASE_URL`, so two projects on two endpoints saw one
    list — the single most user-visible way per-project endpoints fail. This
    resolves the project's row first and caches per endpoint, so two projects
    sharing a gateway still share one cache and one TTL.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    resolved = await resolve_for_project(request, session, project_id)
    catalog = gateway_catalogs(request).for_endpoint(resolved)
    try:
        models = await catalog.models(force=refresh)
    except (httpx.HTTPError, ValueError) as exc:
        return ProjectGatewayModelsOut(
            endpoint=_resolved_out(resolved),
            models=[],
            error=f"{type(exc).__name__}: {exc}",
        )
    return ProjectGatewayModelsOut(
        endpoint=_resolved_out(resolved),
        models=[model_out(m) for m in models],
        error=None,
    )
