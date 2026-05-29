"""Model-profile endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import NotFound
from aleph_core.schemas.model_profile import (
    ModelBindingIn,
    ModelBindingOut,
    ModelProfileOut,
    ModelProfileUpdate,
)
from aleph_db.repos import model_profile as profile_repo
from aleph_observability.tracing import current_trace_id
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1", tags=["model-profile"])


def _to_out(profile_row) -> ModelProfileOut:
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
) -> ModelProfileOut:
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    p = await profile_repo.get_project_profile(session, project_id)
    if p is None:
        msg = f"project {project_id} has no profile"
        raise NotFound(msg)
    new_bindings = dict(p.bindings_jsonb)
    for cap, binding in body.bindings.items():
        new_bindings[cap.value] = ModelBindingIn.model_validate(binding).model_dump(mode="json")
    p.bindings_jsonb = new_bindings
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="model_profile.update",
        target_id=p.id,
        target_kind="model_profile",
        payload={"capabilities": sorted(body.bindings.keys()) if body.bindings else []},
        trace_id=current_trace_id(),
    )
    return _to_out(p)
