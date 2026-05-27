"""GET /v1/me — returns the resolved Principal."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from aleph_api.deps import PrincipalDep

router = APIRouter(prefix="/v1", tags=["me"])


@router.get("/me")
async def get_me(principal: PrincipalDep) -> dict[str, Any]:
    return {
        "user_id": str(principal.user_id),
        "subject": principal.subject,
        "email": principal.email,
        "actor_kind": principal.actor_kind,
    }
