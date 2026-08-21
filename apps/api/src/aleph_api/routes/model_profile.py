"""Model-profile endpoints."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, Field

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import NotFound, ValidationFailed
from aleph_core.schemas.model_profile import (
    GatewayModelOut,
    ModelBindingIn,
    ModelBindingOut,
    ModelProfileOut,
    ModelProfileUpdate,
)
from aleph_db.models.model_profile import ModelProfile
from aleph_db.repos import model_profile as profile_repo
from aleph_models.discovery import (
    capabilities_for,
    probe_model,
    select_default_bindings,
    unbound_capabilities,
)
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

    The source of truth for the Settings model picker. Aleph has no committed
    model list to fall back on — the one it used to ship named six models, none
    of which existed on the real gateway — so an empty response here means the
    gateway is unreachable, and is reported as such rather than filled in with
    plausible names.
    """
    catalog = request.app.state.gateway_catalog
    models = await catalog.models(force=refresh)
    return [
        GatewayModelOut(
            id=m.id,
            mode=m.mode,
            max_input_tokens=m.max_input_tokens,
            input_per_token=m.input_per_token,
            output_per_token=m.output_per_token,
            supports_vision=m.supports_vision,
            supports_function_calling=m.supports_function_calling,
            supports_reasoning=m.supports_reasoning,
            supports_prompt_caching=m.supports_prompt_caching,
            is_priced=m.is_priced,
            capabilities=capabilities_for(m),
        )
        for m in models
    ]


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
    """
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    p = await profile_repo.get_project_profile(session, project_id)
    if p is None:
        msg = f"project {project_id} has no profile"
        raise NotFound(msg)

    catalog = request.app.state.gateway_catalog
    settings = request.app.state.settings
    models = await catalog.models(force=True)
    if not models:
        msg = "model gateway advertises no models; cannot configure a profile from it"
        raise ValidationFailed(msg)

    unreachable: dict[str, str] = {}
    if probe:
        errors = await asyncio.gather(
            *[
                probe_model(
                    base_url=settings.litellm_base_url,
                    api_key=settings.insights_litellm_api_key,
                    model=m,
                    client=request.app.state.gateway_http,
                )
                for m in models
            ]
        )
        unreachable = {m.id: e for m, e in zip(models, errors, strict=True) if e is not None}

    bindings = select_default_bindings(models, unreachable=frozenset(unreachable))
    if not bindings:
        msg = "no model on this gateway qualified for any capability"
        raise ValidationFailed(msg)

    old_embed = _embed_model(p.bindings_jsonb)
    p.bindings_jsonb = {
        cap: ModelBindingIn.model_validate(b).model_dump(mode="json") for cap, b in bindings.items()
    }
    embed_changed = _embed_model(p.bindings_jsonb) != old_embed
    unbound = [c.value for c in unbound_capabilities(bindings)]
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="model_profile.autoconfigure",
        target_id=p.id,
        target_kind="model_profile",
        payload={
            "bound": {c: b["model"] for c, b in bindings.items()},
            "unbound": unbound,
            "unreachable": sorted(unreachable),
            "probed": probe,
            "reembed_enqueued": embed_changed,
        },
        trace_id=current_trace_id(),
    )
    await session.refresh(p)
    result = _to_out(p)
    await session.commit()

    if embed_changed:
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
        bound={c: b["model"] for c, b in bindings.items()},
        unbound=unbound,
        unreachable=unreachable,
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
