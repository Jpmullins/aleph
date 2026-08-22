"""The kernel, reachable. WS-A2.

CLAUDE.md's first substantive line is that Aleph is "an agent that authors
plugins for itself and activates or deactivates them as needed, on a kernel
whose composability model makes that safe — with guardrails preventing it from
removing load-bearing capability. The kernel is the product."

The kernel exists. `AgentPluginAPI` exists, with blast-radius refusal, an
addressability guardrail and 153 tests. And until this file,
`grep -rn "AgentPluginAPI" apps/api/src` returned **0**: no HTTP route, no agent
tool, no graph node. The product was a library with one non-test importer, and
that importer was an acceptance probe.

**Preview and refusal read the same graph, deliberately.** `inspect()` computes
`would_also_stop` as a pure function over the declarations, so showing it costs
nothing and changes nothing — and a refusal the operator could not have
predicted is indistinguishable from a broken button. `test_preview_matches_the_refusal`
is the pin.

**Core capability is not addressable here and cannot be made so.** A capability
mounted from the boot manifest has `plugin_id = None`, so there is no id to put
in a URL. That is the kernel's own guardrail — "deactivating core capability is
not refused, it is unexpressible" — and this route surface inherits it by
carrying ids rather than names.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException, Request, status
from pydantic import BaseModel

from aleph_api.deps import PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import NotFound
from aleph_runtime.ui_contributions import TrustTier
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1/projects", tags=["plugins"])


class CapabilityOut(BaseModel):
    name: str
    state: str
    protected: bool
    provides: list[str]
    requires: list[str]
    #: `null` for anything mounted from the boot manifest. The agent and the
    #: operator learn that core capability is not merely protected but
    #: UNNAMEABLE, without being handed a value they could try.
    plugin_id: str | None
    removable: bool
    would_also_stop: list[str]
    #: Where this capability came from. `core` is anything mounted from the boot
    #: manifest; `verified` was reviewed by a person; `authored` was written by
    #: an agent. Derived, never asserted by the plugin: a capability with no
    #: `plugin_id` is core BECAUSE it has no handle, and everything else falls
    #: back to `authored` rather than to the safest-looking value.
    #:
    #: A DISPLAY attribute, deliberately. Nothing branches on it — see
    #: `aleph_runtime.ui_contributions` for why the approval gate the plan
    #: sketched was not built here.
    trust: TrustTier


class AuthorPluginIn(BaseModel):
    name: str
    instructions: str
    code: str = ""
    requires: list[str] = []
    major_version: int = 1


def _api(request: Request) -> Any:
    from aleph_kernel.agent_api import AgentPluginAPI

    kernel = getattr(request.app.state, "kernel", None)
    if kernel is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the kernel is not mounted on this process",
        )
    return AgentPluginAPI(kernel)


def _declared_trust() -> dict[str, TrustTier]:
    """Capability name → the trust its UI contribution declared.

    Keyed by CAPABILITY name, not row name. A plugin row called `base-notes` is
    mounted as `skill.base-notes` while its contribution is registered under
    `base-notes`, and looking it up by the row name matches nothing — the same
    mismatch that once made `PluginService.disable` skip the kernel guardrail
    entirely, which is why `skill_capability_name` exists rather than an
    f-string at each site.
    """
    from aleph_kernel.skills import skill_capability_name
    from aleph_runtime.ui_contributions import UI_CONTRIBUTIONS

    return {skill_capability_name(c.plugin_id): c.trust for c in UI_CONTRIBUTIONS.all()}


def _trust_of(view: Any, declared: dict[str, TrustTier]) -> TrustTier:
    """`core` is DERIVED from unaddressability, and is not claimable.

    A capability reports `core` exactly when it has no `plugin_id`, and a
    `plugin_id` is minted only by `register_dynamic`. So the tier rests on the
    same guardrail as the rest of this surface rather than on a second, weaker
    one: a contribution that sets `trust="core"` on itself is downgraded here,
    because an agent-installed plugin describing itself as shipped-with-Aleph is
    the one direction in which a wrong answer flatters.

    `verified` — reviewed by a person — passes through, and today nothing
    writes it: `PluginService._contribute_ui` is the only registration site in
    the tree and it always stamps `authored`. This reads the registry rather
    than hardcoding that, because the registry is where the answer will come
    from when a reviewed contribution exists, and a hardcoded `authored` would
    then be wrong in silence.
    """
    if view.plugin_id is None:
        return "core"
    claimed = declared.get(view.name)
    return "authored" if claimed is None or claimed == "core" else claimed


def _view(view: Any, declared: dict[str, TrustTier] | None = None) -> CapabilityOut:
    declared = _declared_trust() if declared is None else declared
    return CapabilityOut(
        name=view.name,
        state=view.state,
        protected=view.protected,
        provides=list(view.provides),
        requires=list(view.requires),
        plugin_id=view.plugin_id,
        removable=view.removable,
        would_also_stop=list(getattr(view, "would_also_stop", ()) or ()),
        trust=_trust_of(view, declared),
    )


@router.get("/{project_id}/plugins", response_model=list[CapabilityOut])
async def list_capabilities(
    project_id: ProjectScopeDep, request: Request, principal: PrincipalDep
) -> list[CapabilityOut]:
    """Every capability, with the cost of removing it already computed."""
    require_at_least(principal, project_id, at_least=ProjectRole.VIEWER)
    declared = _declared_trust()
    return [_view(v, declared) for v in _api(request).inspect()]


@router.get("/{project_id}/plugins/{plugin_id}/removal-preview", response_model=CapabilityOut)
async def preview_removal(
    project_id: ProjectScopeDep,
    plugin_id: UUID,
    request: Request,
    principal: PrincipalDep,
) -> CapabilityOut:
    """What turning this off would also stop.

    Read-only and free: the blast radius is a pure function over the declaration
    graph. The whole point is that the answer here and the refusal from `DELETE`
    come from the same computation, so an operator is never surprised.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.VIEWER)
    for view in _api(request).inspect():
        if view.plugin_id == str(plugin_id):
            return _view(view)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=(
            f"no addressable plugin {plugin_id}. Core capability is mounted from "
            "the boot manifest and has no plugin id — it is not refused, it is "
            "unexpressible."
        ),
    )


@router.post(
    "/{project_id}/plugins", status_code=status.HTTP_201_CREATED, response_model=CapabilityOut
)
async def author_plugin(
    project_id: ProjectScopeDep,
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    body: Annotated[AuthorPluginIn, Body()],
) -> CapabilityOut:
    """Install a plugin, durably.

    OWNER, not EDITOR. A plugin is code this process will execute and an
    instruction the model will follow — the AST gate makes storing it safe, and
    it does not make installing it an editing action.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_runtime.plugin_service import PluginDraft, PluginService

    service = PluginService(session)
    row = await service.install(
        project_id=project_id,
        actor_id=principal.user_id,
        draft=PluginDraft(
            name=body.name,
            instructions=body.instructions,
            code=body.code,
            major_version=body.major_version,
            requires=tuple(body.requires),
        ),
        ledger=LedgerWriter(session),
        kernel=getattr(request.app.state, "kernel", None),
    )
    await session.commit()

    # The kernel mounts a skill as `skill.<row name>`, so comparing `view.name`
    # to `row.name` never matches. It did exactly that, which meant this route
    # ALWAYS fell through to the "installed but not mounted" branch below and
    # always answered `plugin_id: null` — for a plugin that had in fact mounted
    # successfully. The caller was therefore never given the handle, so the
    # DELETE route was unreachable from this API's own output, which is why the
    # second half of this mismatch (`disable_plugin`, below) went unnoticed.
    from aleph_kernel.skills import skill_capability_name

    capability = skill_capability_name(row.name)
    for view in _api(request).inspect():
        if view.name == capability:
            return _view(view)
    # Installed durably but not mounted — the honest report, and the state a
    # process without a kernel legitimately reaches.
    return CapabilityOut(
        name=row.name,
        state="installed",
        protected=False,
        provides=list(row.provides or ()),
        requires=list(row.requires or ()),
        plugin_id=None,
        removable=True,
        would_also_stop=[],
        # Not `core`. This branch is reached only by `author_plugin`, so the
        # thing being described was written by an agent — reporting the
        # trusted tier for a plugin that failed to mount would be the one
        # place the tier could be wrong in the flattering direction.
        trust="authored",
    )


@router.delete("/{project_id}/plugins/{plugin_id}")
async def disable_plugin(
    project_id: ProjectScopeDep,
    plugin_id: UUID,
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    force: bool = False,
) -> dict[str, Any]:
    """Turn it off, and refuse if something is standing on it.

    `force` accepts breaking other agent-installed plugins. It can never reach
    protected capability, because protected capability has no id to pass here.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)

    from aleph_kernel.kernel import PluginId

    api = _api(request)
    known = {v.plugin_id for v in api.inspect() if v.plugin_id}
    if str(plugin_id) not in known:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no addressable plugin {plugin_id}. Core capability is mounted "
                "from the boot manifest and has no plugin id."
            ),
        )

    try:
        outcome = await api.disable(PluginId(plugin_id), force=force)
    except NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    # `installed=True` after a disable means REFUSED — the plugin is still
    # installed. Inverted from the intuitive reading, and worth stating: the
    # field answers "is it installed", not "did the call succeed", so
    # `if not outcome.installed` reads as failure and means success.
    if outcome.installed:
        # A 409, not a 500: the operator asked for something the graph will not
        # allow, and `detail` names exactly what would break.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": outcome.detail},
        )

    from aleph_db.repos.ledger import LedgerWriter
    from aleph_kernel.skills import skill_name_from_capability
    from aleph_runtime.plugin_service import PluginService

    # The ROW name, not the capability name. `inspect()` reports
    # `skill.<name>`; `PluginService.disable` selects on `Plugin.name`, which
    # holds `<name>`. Passing the capability name matched no row, so the row
    # stayed `installed` and the `plugin.disable` ledger event was never
    # written — while this route still returned `{"disabled": true}`, because
    # the kernel half really had succeeded.
    capability = next(
        (v.name for v in api.inspect() if v.plugin_id == str(plugin_id)),
        None,
    )
    name = skill_name_from_capability(capability) if capability else None
    if name:
        await PluginService(session).disable(
            project_id=project_id,
            actor_id=principal.user_id,
            name=name,
            ledger=LedgerWriter(session),
            # Passed even though `PluginApi.disable` already gated: a service
            # method whose safety depends on every caller remembering to check
            # first is a convention, not a guardrail. The call is idempotent,
            # so gating twice costs nothing.
            kernel=getattr(request.app.state, "kernel", None),
            force=force,
        )
        await session.commit()
    return {"plugin_id": str(plugin_id), "disabled": True}


@router.get("/{project_id}/plugins/health")
async def plugin_health(
    project_id: ProjectScopeDep, request: Request, principal: PrincipalDep
) -> dict[str, str]:
    """Each capability's own probe, re-run.

    A capability that cannot answer a live query must not be reported up, and
    this is how an operator finds out before the agent does.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.VIEWER)
    return await _api(request).check_health()
