"""Auth middleware.

Behavior depends on `settings.aleph_auth_mode`:

  * `local` (default for local dev) — JWT path is skipped entirely. Every
    non-public request is associated with a fixed `dev@aleph.local`
    user, JIT-provisioned on first sight (ledgered as `user.create`).
    Agent tokens (HS256) are still accepted in this mode so
    aleph-workers and aiq-server can present scoped credentials.
  * `oidc` — accepts two token forms:
      - User OIDC bearer (RS256 against JWKS) → `Principal(actor_kind="user")`.
        JIT-provisions the `User` row on first sight.
      - Agent token (HS256, signed by aleph-api) →
        `Principal(actor_kind="aleph_agent"|"aiq_agent")`.

Unauthenticated routes (`/healthz`, `/readyz`, `/docs`, `/openapi.json`)
bypass in both modes.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from aleph_core.errors import PermissionDenied
from aleph_core.ids import uuid7
from aleph_db.models.identity import User
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability.logging import bind_request_context
from aleph_observability.tracing import current_trace_id
from aleph_security.agent_token import verify_agent_token
from aleph_security.jwt import verify_user_jwt
from aleph_security.principal import Principal

_log = structlog.get_logger(__name__)

_PUBLIC_PATHS = frozenset(
    {
        "/healthz",
        "/readyz",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)

# Routes that authenticate themselves with a different scheme (e.g. AIQ
# internal callbacks present `X-Aleph-Service-Token`, not a user bearer).
# The middleware skips its bearer check for these prefixes; the route
# handler is responsible for its own verification.
_SELF_AUTH_PREFIXES: tuple[str, ...] = ("/internal/v1/aiq/", "/copilotkit")


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        if (
            path in _PUBLIC_PATHS
            or path.startswith("/static/")
            or any(path.startswith(p) for p in _SELF_AUTH_PREFIXES)
        ):
            return await call_next(request)

        settings = request.app.state.settings
        auth = request.headers.get("authorization") or ""

        # Agent tokens are accepted in both modes — they carry their own
        # HS256 signature and are how workers/aiq-server authenticate.
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            try:
                if _looks_like_agent_token(token):
                    principal = await _principal_from_agent_token(request, token)
                elif settings.aleph_auth_mode == "oidc":
                    principal = await _principal_from_user_jwt(request, token)
                else:
                    # Local mode: ignore the bearer content (could be the
                    # frontend's sentinel) and synthesize the dev principal.
                    principal = await _principal_local_dev(request)
            except PermissionDenied as exc:
                return _problem(401, "auth_failed", exc.message, request)
        elif settings.aleph_auth_mode == "local":
            principal = await _principal_local_dev(request)
        else:
            return _problem(
                401,
                "missing_bearer",
                "Authorization: Bearer <token> required",
                request,
            )

        request.state.principal = principal
        bind_request_context(
            request_id=getattr(request.state, "request_id", ""),
            user_id=principal.user_id,
        )

        return await call_next(request)


def _looks_like_agent_token(token: str) -> bool:
    parts = token.split(".")
    if len(parts) != 3:
        return False
    # Cheap heuristic: HS256-signed agent tokens come from us, so they're
    # short and JSON-parseable; we still defer the real choice to the
    # signature check.
    import base64
    import json

    try:
        pad = "=" * (-len(parts[0]) % 4)
        header = json.loads(base64.urlsafe_b64decode(parts[0] + pad))
    except (ValueError, OSError):
        return False
    return header.get("alg") == "HS256"


async def _principal_local_dev(request: Request) -> Principal:
    """Materialize the hardcoded local-dev principal.

    Goes through the same JIT-provisioning path as a real first-login,
    so the User row exists, is `user.create`-ledgered, and is stable
    across restarts (keyed on `local_dev_subject`).
    """
    s = request.app.state.settings
    user_id = await _resolve_or_provision_user(
        request,
        subject=s.local_dev_subject,
        email=s.local_dev_email,
        display_name=s.local_dev_display_name,
    )
    return Principal(
        user_id=user_id,
        subject=s.local_dev_subject,
        email=s.local_dev_email,
        actor_kind="user",
    )


async def _principal_from_user_jwt(request: Request, token: str) -> Principal:
    s = request.app.state.settings
    jwks_cache = request.app.state.jwks_cache
    if jwks_cache is None or not s.aleph_auth_issuer:
        msg = "OIDC auth requested but not configured"
        raise PermissionDenied(msg)
    claims = await verify_user_jwt(
        token,
        jwks_cache=jwks_cache,
        issuer=s.aleph_auth_issuer,
        audience=s.aleph_auth_audience,
    )

    subject = str(claims["sub"])
    email = str(claims.get("email", ""))
    display_name = str(claims.get("name") or claims.get("preferred_username") or email or subject)

    user_id = await _resolve_or_provision_user(
        request, subject=subject, email=email, display_name=display_name
    )
    return Principal(
        user_id=user_id,
        subject=subject,
        email=email,
        actor_kind="user",
    )


async def _principal_from_agent_token(request: Request, token: str) -> Principal:
    s = request.app.state.settings
    claims = verify_agent_token(token, secret=s.aleph_agent_token_secret)
    # Look up the underlying user to populate subject/email; users are
    # already JIT-provisioned at this point. If absent, refuse.
    maker = request.app.state.session_maker
    async with maker() as session:
        user = (
            await session.execute(select(User).where(User.id == claims.user_id))
        ).scalar_one_or_none()
        if user is None:
            msg = f"agent token references unknown user {claims.user_id}"
            raise PermissionDenied(msg)
    return Principal(
        user_id=claims.user_id,
        subject=user.subject,
        email=user.email,
        actor_kind=claims.actor_kind,
        agent_run_id=claims.agent_run_id,
        correlation_id=claims.correlation_id,
    )


async def _resolve_or_provision_user(
    request: Request,
    *,
    subject: str,
    email: str,
    display_name: str,
) -> UUID:
    maker = request.app.state.session_maker
    async with maker() as session:
        existing = (
            await session.execute(select(User).where(User.subject == subject))
        ).scalar_one_or_none()
        if existing is not None:
            # Keep email/display name fresh.
            mutated = False
            if existing.email != email and email:
                existing.email = email
                mutated = True
            if existing.display_name != display_name and display_name:
                existing.display_name = display_name
                mutated = True
            if mutated:
                await session.commit()
            return existing.id

        new_id = uuid7()
        user = User(
            id=new_id,
            subject=subject,
            email=email,
            display_name=display_name,
            created_by=new_id,
            access_scope="global",
        )
        session.add(user)
        await session.flush()

        ledger = LedgerWriter(session)
        await ledger.append(
            project_id=None,
            actor_id=new_id,
            actor_kind="system",
            action_kind="user.create",
            target_id=new_id,
            target_kind="user",
            payload={
                "subject": subject,
                "email": email,
                "display_name": display_name,
            },
            trace_id=current_trace_id(),
        )
        await session.commit()
        return new_id


def _problem(status: int, code: str, detail: str, request: Request) -> Response:
    body = {
        "type": f"about:blank#{code}",
        "title": code.replace("_", " ").capitalize(),
        "status": status,
        "detail": detail,
        "instance": str(request.url.path),
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json")
