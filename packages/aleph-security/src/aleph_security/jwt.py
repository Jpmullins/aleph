"""User JWT verification against an OIDC IdP's JWKS endpoint.

The cache holds public keys indexed by `kid`. A token whose `kid` is
missing from the cache triggers a one-shot refresh; if still missing
the token is rejected.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWK, PyJWTError

from aleph_core.errors import PermissionDenied


@dataclass
class _CacheEntry:
    keys_by_kid: dict[str, dict[str, Any]]
    fetched_at: float


class JWKSCache:
    """JWKS public-key cache.

    Hot path: read from the dict by `kid`, no I/O.
    Cold path / kid miss: refresh from the JWKS URL with httpx.
    Cache lifetime: 10 minutes; refresh also on kid miss.
    """

    def __init__(
        self,
        *,
        jwks_url: str,
        http_client: httpx.AsyncClient,
        max_age_seconds: float = 600.0,
    ) -> None:
        self._url = jwks_url
        self._http = http_client
        self._max_age = max_age_seconds
        self._entry: _CacheEntry | None = None

    async def get(self, kid: str) -> dict[str, Any]:
        if (
            self._entry
            and kid in self._entry.keys_by_kid
            and time.monotonic() - self._entry.fetched_at < self._max_age
        ):
            return self._entry.keys_by_kid[kid]
        await self._refresh()
        if self._entry and kid in self._entry.keys_by_kid:
            return self._entry.keys_by_kid[kid]
        msg = f"jwks key not found: kid={kid}"
        raise PermissionDenied(msg)

    async def _refresh(self) -> None:
        resp = await self._http.get(self._url, timeout=5.0)
        resp.raise_for_status()
        body = resp.json()
        keys: list[dict[str, Any]] = body.get("keys", [])
        self._entry = _CacheEntry(
            keys_by_kid={k["kid"]: k for k in keys if "kid" in k},
            fetched_at=time.monotonic(),
        )


# Pinned signing-algorithm allowlist. Verifying with the algorithm named in the
# token's own `alg` header is the classic algorithm-confusion foot-gun; only
# asymmetric OIDC signatures are ever accepted here.
_ALLOWED_JWT_ALGS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")


async def verify_user_jwt(
    token: str,
    *,
    jwks_cache: JWKSCache,
    issuer: str,
    audience: str,
    leeway_seconds: int = 30,
) -> dict[str, Any]:
    """Verify an OIDC bearer JWT and return its claims."""
    try:
        header = jwt.get_unverified_header(token)
    except PyJWTError as exc:
        msg = f"invalid jwt header: {exc}"
        raise PermissionDenied(msg) from exc

    kid = header.get("kid")
    if not kid:
        msg = "jwt missing kid header"
        raise PermissionDenied(msg)

    jwk_dict = await jwks_cache.get(kid)
    try:
        # PyJWT needs a key object, not a raw JWK dict; PyJWK builds the right
        # algorithm-specific key (RSA/EC) from the JWKS entry.
        signing_key = PyJWK.from_dict(jwk_dict).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=list(_ALLOWED_JWT_ALGS),
            audience=audience,
            issuer=issuer,
            leeway=leeway_seconds,
            options={"require": ["exp"]},
        )
    except PyJWTError as exc:
        msg = f"invalid jwt: {exc}"
        raise PermissionDenied(msg) from exc

    now = int(time.time())
    if claims.get("exp", 0) + leeway_seconds < now:
        msg = "jwt expired"
        raise PermissionDenied(msg)
    if claims.get("nbf", 0) - leeway_seconds > now:
        msg = "jwt not yet valid"
        raise PermissionDenied(msg)
    return claims
