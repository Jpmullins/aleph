"""Service-token issuance + verification for the AIQ ↔ aleph-api boundary.

AIQ-side service tokens are HS256 JWTs signed by aleph-api. They carry:
  iss=aleph-api, sub=aiq-server, project_id, agent_run_id,
  principal_user_id, scope=aiq.full, exp ≤ 1h.

AIQ holds the token via env and presents it on every callback to
`/internal/v1/aiq/*`. Each callback verifies the token and uses the
claims as the authorization context.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from jose import jwt
from jose.exceptions import JWTError

from aleph_core.errors import PermissionDenied

_ISS: Final[str] = "aleph-api"
_AUD: Final[str] = "aiq-server"
_DEFAULT_TTL: Final[int] = 3600


@dataclass(frozen=True)
class AIQServiceToken:
    project_id: UUID
    agent_run_id: UUID
    principal_user_id: UUID
    scope: str
    iat: int
    exp: int


def issue_service_token(
    *,
    secret: str,
    project_id: UUID,
    agent_run_id: UUID,
    principal_user_id: UUID,
    scope: str = "aiq.full",
    ttl_seconds: int = _DEFAULT_TTL,
) -> str:
    if ttl_seconds <= 0 or ttl_seconds > _DEFAULT_TTL:
        msg = f"ttl_seconds out of range: {ttl_seconds}"
        raise ValueError(msg)
    iat = int(time.time())
    claims = {
        "iss": _ISS,
        "sub": _AUD,
        "aud": _AUD,
        "project_id": str(project_id),
        "agent_run_id": str(agent_run_id),
        "principal_user_id": str(principal_user_id),
        "scope": scope,
        "iat": iat,
        "exp": iat + ttl_seconds,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def verify_service_token(token: str, *, secret: str) -> AIQServiceToken:
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], audience=_AUD, issuer=_ISS)
    except JWTError as exc:
        msg = f"invalid aiq service token: {exc}"
        raise PermissionDenied(msg) from exc
    return AIQServiceToken(
        project_id=UUID(claims["project_id"]),
        agent_run_id=UUID(claims["agent_run_id"]),
        principal_user_id=UUID(claims["principal_user_id"]),
        scope=str(claims["scope"]),
        iat=int(claims["iat"]),
        exp=int(claims["exp"]),
    )
