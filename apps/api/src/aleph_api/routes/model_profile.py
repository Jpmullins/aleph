"""Model-profile endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, Field

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_api.routes.gateway_endpoints import (
    gateway_catalogs,
    model_out,
    resolve_for_project,
)
from aleph_core.errors import NotFound
from aleph_core.schemas.model_profile import (
    GatewayModelOut,
    ModelBindingIn,
    ModelBindingOut,
    ModelProfileOut,
    ModelProfileUpdate,
)
from aleph_db.models.model_profile import ModelProfile
from aleph_db.repos import model_profile as profile_repo
from aleph_models.autoconfigure import autoconfigure_project
from aleph_models.endpoints import settings_endpoint
from aleph_observability.tracing import current_trace_id
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1", tags=["model-profile"])


class SwitchProfileIn(BaseModel):
    profile_name: str = Field(min_length=1, max_length=64)


def _embed_model(bindings: dict[str, Any]) -> str | None:
    embedding = bindings.get("embedding")
    model = embedding.get("model") if isinstance(embedding, dict) else None
    return model if isinstance(model, str) else None


def _to_out(profile_row: ModelProfile) -> ModelProfileOut:
    return ModelProfileOut(
        id=profile_row.id,
        name=profile_row.name,
        project_id=profile_row.project_id,
        is_template=profile_row.is_template,
        bindings={
            cap: ModelBindingOut.model_validate(b) for cap, b in profile_row.bindings_jsonb.items()
        },
        created_at=profile_row.created_at,
        updated_at=profile_row.updated_at,
    )


@router.get("/gateway/models", response_model=list[GatewayModelOut])
async def list_gateway_models(request: Request, refresh: bool = False) -> list[GatewayModelOut]:
    """What the configured gateway actually serves.

    Aleph has no committed model list to fall back on — the one it used to ship
    named six models, none of which existed on the real gateway — so an empty
    response here means the gateway is unreachable, and is reported as such
    rather than filled in with plausible names.

    **This route knows no project, so it can only ever describe the DEPLOYMENT
    DEFAULT.** It used to read a `GatewayCatalog` built once at boot, which made
    that limitation invisible: every project saw the same list whether or not it
    had an endpoint of its own. It now resolves the Settings endpoint through
    the same per-endpoint cache every project-scoped read uses, so a project
    with its own row and this route are visibly two different answers rather
    than accidentally one. The per-project list —
    `GET /v1/projects/{id}/gateway/models` — is the one to prefer.
    """
    s = request.app.state.settings
    catalog = gateway_catalogs(request).for_endpoint(
        settings_endpoint(base_url=s.litellm_base_url, api_key=s.insights_litellm_api_key)
    )
    return [model_out(m) for m in await catalog.models(force=refresh)]


class AutoconfigureOut(BaseModel):
    """What autoconfigure decided, including what it refused to decide."""

    profile: ModelProfileOut
    bound: dict[str, str]
    #: Capabilities left deliberately unbound because no model qualified.
    unbound: list[str]
    #: Advertised models that failed an actual invocation, with the error.
    unreachable: dict[str, str]


@router.post("/projects/{project_id}/model-profile/autoconfigure", response_model=AutoconfigureOut)
async def autoconfigure_profile(
    project_id: ProjectScopeDep,
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
    request: Request,
    probe: bool = True,
) -> AutoconfigureOut:
    """Bind every capability to the best model this gateway actually serves.

    This is where "defaults" really come from. The seeded `aleph-dev` /
    `aleph-production` templates name models chosen when the code was written,
    which is a guess about someone else's gateway — on the first real one, not
    a single name matched. Here the choice is derived from the gateway's own
    metadata: mode, context window, tool support, vision, and price.

    `probe` calls each model once before trusting it. A gateway's model list
    states configuration, not reachability: this deployment advertises two
    Sonnets that fail on invocation, and both would otherwise have been bound.
    The extra second at configuration time replaces a failure in the middle of
    a research run.

    Capabilities with no qualifying model are reported in `unbound` rather than
    pointed at something that cannot do the job.

    **It binds against THIS PROJECT'S endpoint.** It used to be handed the
    catalog and the credentials built at boot from `LITELLM_BASE_URL`, so a
    project with a gateway endpoint of its own was configured from a list its
    own gateway had never advertised and probed against a server it does not
    use — binding names that 404 on first call. `resolve_for_project` returns
    the project's row if it has one and the deployment default if it does not,
    and the catalog, the base URL and the key below all come from that one
    answer rather than from three places that can disagree.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)

    resolved = await resolve_for_project(request, session, project_id)
    # The selection logic is shared with the worker job project creation
    # enqueues (`autoconfigure_profile_job`), so the two cannot drift into
    # binding different models from the same gateway.
    p, outcome = await autoconfigure_project(
        session,
        project_id=project_id,
        catalog=gateway_catalogs(request).for_endpoint(resolved),
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        http_client=request.app.state.gateway_http,
        probe=probe,
    )
    settings = request.app.state.settings
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="model_profile.autoconfigure",
        target_id=p.id,
        target_kind="model_profile",
        payload={
            "bound": outcome.bound,
            "unbound": outcome.unbound,
            "unreachable": sorted(outcome.unreachable),
            "probed": probe,
            "reembed_enqueued": outcome.embed_changed,
            # WHICH gateway these names came from. Without it a binding that
            # 404s is unattributable after the fact: the same model id means
            # different things on two endpoints. The id and the source, never
            # the key.
            "endpoint_id": str(resolved.endpoint_id) if resolved.endpoint_id else None,
            "endpoint_source": resolved.source,
        },
        trace_id=current_trace_id(),
    )
    await session.refresh(p)
    result = _to_out(p)
    await session.commit()

    if outcome.embed_changed:
        try:
            pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            try:
                await pool.enqueue_job("reembed_job", str(project_id))
            finally:
                await pool.aclose()
        except Exception:  # reembed is eventual; a failed enqueue never blocks config
            pass

    return AutoconfigureOut(
        profile=result,
        bound=outcome.bound,
        unbound=outcome.unbound,
        unreachable=outcome.unreachable,
    )


@router.get("/model-profile-templates", response_model=list[ModelProfileOut])
async def list_templates(session: SessionDep) -> list[ModelProfileOut]:
    rows = await profile_repo.list_templates(session)
    return [_to_out(r) for r in rows]


@router.get("/projects/{project_id}/model-profile", response_model=ModelProfileOut)
async def get_project_profile(project_id: ProjectScopeDep, session: SessionDep) -> ModelProfileOut:
    p = await profile_repo.get_project_profile(session, project_id)
    if p is None:
        msg = f"project {project_id} has no profile"
        raise NotFound(msg)
    return _to_out(p)


@router.patch("/projects/{project_id}/model-profile", response_model=ModelProfileOut)
async def update_project_profile(
    project_id: ProjectScopeDep,
    body: Annotated[ModelProfileUpdate, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
    request: Request,
) -> ModelProfileOut:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    p = await profile_repo.get_project_profile(session, project_id)
    if p is None:
        msg = f"project {project_id} has no profile"
        raise NotFound(msg)
    old_embed = _embed_model(p.bindings_jsonb)
    new_bindings = dict(p.bindings_jsonb)
    for cap, binding in body.bindings.items():
        new_bindings[cap.value] = ModelBindingIn.model_validate(binding).model_dump(mode="json")
    p.bindings_jsonb = new_bindings
    embed_changed = _embed_model(new_bindings) != old_embed
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="model_profile.update",
        target_id=p.id,
        target_kind="model_profile",
        payload={
            "capabilities": sorted(body.bindings.keys()) if body.bindings else [],
            "reembed_enqueued": embed_changed,
        },
        trace_id=current_trace_id(),
    )
    # Same refresh the other two write paths do, and for the same reason:
    # `updated_at` has a server-side `onupdate`, so the flush above leaves it
    # expired. Reading it then lazy-loads, and a lazy load on an async session
    # raises MissingGreenlet — saving a model binding returned a 500 with no
    # usable message, and because CORS sat inside the error middleware the
    # browser reported it as a CORS failure instead.
    await session.refresh(p)
    result = _to_out(p)
    await session.commit()
    # If the embedding model changed via a per-capability edit, repair drift too
    # (mirrors the named-profile switch route).
    if embed_changed:
        try:
            pool = await create_pool(RedisSettings.from_dsn(request.app.state.settings.redis_url))
            try:
                await pool.enqueue_job("reembed_job", str(project_id))
            finally:
                await pool.aclose()
        except Exception:  # reembed is eventual; a failed enqueue never blocks the edit
            pass
    return result


@router.post("/projects/{project_id}/model-profile/switch", response_model=ModelProfileOut)
async def switch_project_profile(
    project_id: ProjectScopeDep,
    body: Annotated[SwitchProfileIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
    request: Request,
) -> ModelProfileOut:
    """Switch the project to a named profile template (aleph-dev / aleph-production).

    Copies the template's bindings onto the project's profile (ledgered). If the
    embedding model changed, enqueues `reembed_job` to repair embedder-model drift.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    p = await profile_repo.get_project_profile(session, project_id)
    if p is None:
        msg = f"project {project_id} has no profile"
        raise NotFound(msg)
    template = await profile_repo.get_template(session, body.profile_name)
    if template is None:
        msg = f"no model-profile template named {body.profile_name!r}"
        raise NotFound(msg)
    old_embed = _embed_model(p.bindings_jsonb)
    new_embed = _embed_model(template.bindings_jsonb)
    embed_changed = old_embed != new_embed
    p.bindings_jsonb = dict(template.bindings_jsonb)
    p.name = template.name
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="model_profile.switch",
        target_id=p.id,
        target_kind="model_profile",
        payload={"profile_name": template.name, "reembed_enqueued": embed_changed},
        trace_id=current_trace_id(),
    )
    # Reload the server-managed columns (updated_at onupdate) in the async
    # context, then build the response BEFORE committing — a plain attribute
    # access can't lazy-load an expired attr outside the request greenlet.
    await session.refresh(p)
    result = _to_out(p)
    await session.commit()

    if embed_changed:
        try:
            pool = await create_pool(RedisSettings.from_dsn(request.app.state.settings.redis_url))
            try:
                await pool.enqueue_job("reembed_job", str(project_id))
            finally:
                await pool.aclose()
        except Exception:  # reembed is eventual; a failed enqueue never blocks the switch
            pass
    return result
