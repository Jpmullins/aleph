"""Short-lived agent tokens.

Issued by `aleph-api` to authorize a worker to call back
into the API on behalf of a specific `AgentRun`. Signed HS256 with
ALEPH_AGENT_TOKEN_SECRET. Verifying side is `verify_agent_token`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import jwt
from jwt import PyJWTError

from aleph_core.errors import PermissionDenied

AgentActorKind = Literal["aleph_agent"]

_DEFAULT_TTL_SECONDS = 3600


@dataclass(frozen=True)
class AgentTokenClaims:
    user_id: UUID
    """The user who initiated the work the agent is doing."""
    project_id: UUID
    agent_run_id: UUID
    actor_kind: AgentActorKind
    correlation_id: str
    exp: int
    iat: int


def mint_agent_token(
    *,
    secret: str,
    user_id: UUID,
    project_id: UUID,
    agent_run_id: UUID,
    actor_kind: AgentActorKind,
    correlation_id: str,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> str:
    if ttl_seconds <= 0 or ttl_seconds > _DEFAULT_TTL_SECONDS:
        msg = f"ttl_seconds out of range: {ttl_seconds}"
        raise ValueError(msg)
    iat = int(time.time())
    claims = {
        "sub": str(user_id),
        "project_id": str(project_id),
        "agent_run_id": str(agent_run_id),
        "actor_kind": actor_kind,
        "correlation_id": correlation_id,
        "iat": iat,
        "exp": iat + ttl_seconds,
        "iss": "aleph-api",
        "aud": "aleph-agent",
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def verify_agent_token(token: str, *, secret: str) -> AgentTokenClaims:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="aleph-agent",
            issuer="aleph-api",
        )
    except PyJWTError as exc:
        msg = f"invalid agent token: {exc}"
        raise PermissionDenied(msg) from exc

    return AgentTokenClaims(
        user_id=UUID(claims["sub"]),
        project_id=UUID(claims["project_id"]),
        agent_run_id=UUID(claims["agent_run_id"]),
        actor_kind=claims["actor_kind"],
        correlation_id=claims["correlation_id"],
        iat=int(claims["iat"]),
        exp=int(claims["exp"]),
    )
