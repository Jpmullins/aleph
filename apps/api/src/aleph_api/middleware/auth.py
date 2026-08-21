"""Auth middleware.

Behavior depends on `settings.aleph_auth_mode`:

  * `local` (default for local dev) — JWT path is skipped entirely. Every
    non-public request is associated with a fixed `dev@aleph.local`
    user, JIT-provisioned on first sight (ledgered as `user.create`).
    Agent tokens (HS256) are still accepted in this mode so
    aleph-workers can present scoped credentials.
  * `oidc` — accepts two token forms:
        JIT-provisions the `User` row on first sight.
      - Agent token (HS256, signed by aleph-api) →
        `Principal(actor_kind="aleph_agent")`.

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

from aleph_core.errors import NotFound, PermissionDenied
from aleph_core.ids import uuid7
from aleph_db.models.identity import User
from aleph_db.repos.ledger import LedgerWriter
from aleph_db.repos.project import get_member
from aleph_observability.logging import bind_request_context
from aleph_observability.tracing import current_trace_id
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal
from aleph_security.request_context import bind_principal, reset_principal

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

# Prefixes the middleware skips entirely, on the promise that the mounted
# handler verifies the caller itself.
#
# EMPTY, DELIBERATELY. `/copilotkit` lived here on exactly that promise and the
# handler never kept it: `setup_copilotkit` mounts the LangGraph AG-UI endpoint
# with no `dependencies=` and no principal — `copilotkit_endpoint.py` contains no
# auth code at all — so the Deep Agent's write tools were reachable
# unauthenticated in BOTH auth modes, while taking their project scope from a
# client-supplied `thread_id` / RunnableConfig with no membership check. Any
# caller could name any project in the database.
#
# The exemption bought nothing. In `local` mode — the only deployed mode —
# nothing changes: a request with no bearer still synthesizes the dev principal
# below. In `oidc` mode an unauthenticated agent request now gets the 401 that
# was always the correct answer. Note that the Node bridge does NOT yet forward
# the browser's credential (`copilot-runtime/src/server.ts` builds
# `new HttpAgent({ url })` with no headers), so under `oidc` the chat path
# correctly demands a credential it never receives; closing that needs
# per-request header propagation browser → runtime → API. Tracked in
# `docs/architecture.md` § Known gaps — it is not a reason to re-exempt.
#
# Adding a prefix here re-opens that class of hole. Do not, without demonstrating
# that the handler actually verifies — with a test.
# `test_no_blanket_auth_exemption_prefixes` and acceptance check F1 guard this.
_SELF_AUTH_PREFIXES: tuple[str, ...] = ()


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

        auth = request.headers.get("authorization") or ""

        # Agent tokens carry their own HS256 signature and are how workers
        # authenticate. Everything else is the local dev principal — Aleph runs
        # single-user, and the OIDC path was removed (docs/decisions.md D6)
        # because it was half-built, never deployed, and its two known holes
        # were shaping work that had no user.
        try:
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
                if _looks_like_agent_token(token):
                    principal = await _principal_from_agent_token(request, token)
                else:
                    # Ignore the bearer content — it may be the frontend's
                    # sentinel — and synthesize the dev principal.
                    principal = await _principal_local_dev(request)
            else:
                principal = await _principal_local_dev(request)
        except PermissionDenied as exc:
            return _problem(401, "auth_failed", exc.message, request)

        request.state.principal = principal
        # Also bind it task-locally: the AG-UI agent endpoint is owned by
        # `add_langgraph_fastapi_endpoint`, so its tools have no request to read
        # a principal from. Reset in `finally` so a pooled worker task cannot
        # inherit the previous request's identity.
        principal_token = bind_principal(principal)
        bind_request_context(
            request_id=getattr(request.state, "request_id", ""),
            user_id=principal.user_id,
        )

        try:
            # AG-UI agent requests carry their project scope in the body (a
            # `proj:<uuid>:<thread>` thread id). Refuse a foreign project HERE,
            # at the boundary, before the graph is started and before a single
            # token is spent.
            #
            # This does not replace `require_project_access` inside the tools
            # (via the ContextVar bound just above) — the two cover different
            # gaps and both are load-bearing:
            #
            #  * The tool check is authoritative and fails closed on an unbound
            #    principal, but it runs after the run is underway and it
            #    authorizes on *membership* only — it never inspects the
            #    credential's signed `project_id` binding.
            #  * This check reads the wire body, so it sees every project the
            #    request names at any depth, and it enforces that signed binding
            #    (an agent token minted for project A cannot drive a run against
            #    project B, even if its user is a member of B).
            #
            # See `middleware/agent_scope.py`.
            if path.startswith(_AGENT_SCOPED_PREFIXES):
                try:
                    await _assert_agent_request_scope(request, principal)
                except NotFound as exc:
                    return _problem(404, "not_found", exc.message, request)
                except PermissionDenied as exc:
                    return _problem(403, "permission_denied", exc.message, request)

            return await call_next(request)
        finally:
            reset_principal(principal_token)


#: Paths whose body names the project the agent will act on.
_AGENT_SCOPED_PREFIXES: tuple[str, ...] = ("/copilotkit",)


async def _assert_agent_request_scope(request: Request, principal: Principal) -> None:
    """Refuse an agent run naming a project the caller does not belong to.

    Reading the body here is safe: Starlette's `BaseHTTPMiddleware` wraps the
    request in a `_CachedRequest`, so the downstream handler still receives it.
    """
    from aleph_api.middleware.agent_scope import (
        assert_caller_may_use_projects,
        extract_project_ids,
    )

    project_ids = extract_project_ids(await request.body())
    if not project_ids:
        return

    maker = request.app.state.session_maker

    async def _is_member(user_id: UUID, project_id: UUID) -> bool:
        async with maker() as session:
            return (await get_member(session, project_id=project_id, user_id=user_id)) is not None

    await assert_caller_may_use_projects(principal, project_ids, _is_member)


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
        # The signed project binding. Discarding this is what made the mint-time
        # OWNER gate decorative — see `project_scope_dep`.
        project_id=claims.project_id,
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
