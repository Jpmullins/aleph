"""Pytest fixtures for integration tests.

Integration tests require:
  * Postgres reachable at DATABASE_URL (sync or asyncpg URL)
  * Redis reachable at REDIS_URL
  * Alembic migrations applied (`alembic upgrade head`)

They do NOT spin up the FastAPI app via uvicorn; they use the
ASGI test client (httpx.ASGITransport). They also do NOT call a
real LLM gateway — the LiteLLMClient is monkey-patched per test.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration


def _required_env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        pytest.skip(f"integration test requires env var {name}")
    return v


def _set_defaults() -> None:
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://aleph:changeme-ci@localhost:5432/aleph",
    )
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    # MinIO / asset store. The ingest-url + upload source routes 422 with
    # "asset store is not configured" unless the lifespan can build an
    # AssetStore, which requires all four of these. Endpoint defaults to the
    # host-published port; credentials/bucket must be supplied via env (same
    # pattern as DATABASE_URL) so no secret is baked into the repo.
    os.environ.setdefault("MINIO_ENDPOINT", "http://localhost:9000")
    os.environ.setdefault("ALEPH_S3_BUCKET", "aleph-local")
    os.environ.setdefault("LANGFUSE_HOST", "http://localhost:3000")
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "pk-ci")
    os.environ.setdefault("LANGFUSE_SECRET_KEY", "sk-ci")
    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    os.environ.setdefault("LITELLM_BASE_URL", "http://localhost:18999")
    os.environ.setdefault("INSIGHTS_LITELLM_API_KEY", "ci-fake")
    os.environ.setdefault("ALEPH_AUTH_ISSUER", "http://localhost:8080/realms/aleph")
    os.environ.setdefault("ALEPH_AUTH_AUDIENCE", "aleph")
    os.environ.setdefault("ALEPH_AUTH_JWKS_URL", "http://localhost:8080/jwks")
    os.environ.setdefault(
        "ALEPH_AGENT_TOKEN_SECRET",
        "ci-agent-token-secret-do-not-use-elsewhere",
    )
    os.environ.setdefault("ALEPH_DEFAULT_MODEL_PROFILE", "aleph-dev")
    # `Settings.aleph_env` is a Literal["local","dev","staging","prod"]; "ci" is
    # not valid and would raise a ValidationError when Settings() is constructed.
    # Use "local" to match the running compose stack.
    os.environ.setdefault("ALEPH_ENV", "local")


_set_defaults()
_required_env("DATABASE_URL")
_required_env("REDIS_URL")


@pytest.fixture(scope="session")
def fake_user_subject() -> str:
    return f"test-user-{uuid.uuid4()}"


@pytest.fixture
async def asgi_app() -> AsyncIterator[Any]:
    """Construct the FastAPI app per test. The lifespan startup connects to
    Postgres + Redis + Langfuse + OTEL collector — those are CI dependencies.

    Function-scoped (not session): pytest-asyncio gives each test its own
    event loop (asyncio_mode=auto, default function loop scope). A
    session-scoped async engine binds its asyncpg pool to the first test's
    loop, so a later test on a fresh loop hits "Future attached to a different
    loop". Rebuilding the app per test keeps the engine on the live loop.
    """
    from aleph_api.main import create_app

    app = create_app()
    # httpx.ASGITransport does NOT emit ASGI lifespan events, so we must drive
    # the lifespan ourselves; otherwise `lifespan()` never runs and
    # app.state.settings / .session_maker / .asset_store / .litellm are unset,
    # causing routes to 500.
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def http_client(asgi_app) -> AsyncIterator[httpx.AsyncClient]:
    """Authenticated test client.

    Auth is mocked by monkey-patching the auth middleware to inject a
    Principal directly. See `_TestAuthMiddleware` setup.

    Every project a test creates through this client is tracked (response
    hook on `POST /v1/projects`) and soft-deleted at teardown, so test runs
    don't pollute the dev project list.
    """

    from aleph_api.middleware.auth import AuthMiddleware  # noqa: F401

    # The test client uses an extra header that the auth middleware sees;
    # we monkey-patch `verify_user_jwt` to accept a fixed token. The
    # actual swap-out happens in `_BypassAuth` patched below.

    created_project_ids: list[str] = []

    async def _track_created_project(response: httpx.Response) -> None:
        req = response.request
        if req.method == "POST" and req.url.path == "/v1/projects" and response.status_code == 201:
            await response.aread()
            pid = response.json().get("id")
            if isinstance(pid, str):
                created_project_ids.append(pid)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="http://testserver",
        headers={"Authorization": "Bearer test"},
        event_hooks={"response": [_track_created_project]},
    ) as client:
        yield client
        for pid in created_project_ids:
            # Teardown is best-effort — a failing delete must not mask the test.
            with contextlib.suppress(Exception):
                await client.patch(f"/v1/projects/{pid}", json={"status": "deleted"})
