"""Auth boundary: Principal, JWT verify, role gates, agent tokens."""

from aleph_security.agent_token import (
    AgentTokenClaims,
    mint_agent_token,
    verify_agent_token,
)
from aleph_security.jwt import JWKSCache, verify_user_jwt
from aleph_security.principal import Principal
from aleph_security.request_context import (
    bind_principal,
    current_principal,
    require_project_access,
    reset_principal,
)
from aleph_security.roles import ProjectRole, rank, require_at_least

__all__ = [
    "AgentTokenClaims",
    "JWKSCache",
    "Principal",
    "ProjectRole",
    "bind_principal",
    "current_principal",
    "mint_agent_token",
    "rank",
    "require_at_least",
    "require_project_access",
    "reset_principal",
    "verify_agent_token",
    "verify_user_jwt",
]
