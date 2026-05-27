"""Auth middleware.

Two token forms are accepted:

  * User OIDC bearer (RS256, validated against JWKS). Resolves a
    `Principal(actor_kind="user")`. JIT-provisions the `User` row on
    first sight, ledgered as `user.create`.
  * Agent token (HS256, signed by aleph-api). Resolves a
    `Principal(actor_kind="aleph_agent"|"aiq_agent")`.

Unauthenticated routes (`/healthz`, `/readyz`, `/docs`, `/openapi.json`)
bypass.
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


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if (
            request.url.path in _PUBLIC_PATHS
            or request.url.path.startswith("/static/")
        ):
            return await call_next(request)

        auth = request.headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            return _problem(
                401,
                "missing_bearer",
                "Authorization: Bearer <token> required",
                request,
            )
        token = auth[7:].strip()

        try:
            if _looks_like_agent_token(token):
                principal = await _principal_from_agent_token(request, token)
            else:
                principal = await _principal_from_user_jwt(request, token)
        except PermissionDenied as exc:
            return _problem(401, "auth_failed", exc.message, request)

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


async def _principal_from_user_jwt(request: Request, token: str) -> Principal:
    s = request.app.state.settings
    jwks_cache = request.app.state.jwks_cache
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


def _problem(
    status: int, code: str, detail: str, request: Request
) -> Response:
    body = {
        "type": f"about:blank#{code}",
        "title": code.replace("_", " ").capitalize(),
        "status": status,
        "detail": detail,
        "instance": str(request.url.path),
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json")
