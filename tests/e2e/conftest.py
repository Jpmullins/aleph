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
    os.environ.setdefault("LANGFUSE_HOST", "http://localhost:3000")
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "pk-ci")
    os.environ.setdefault("LANGFUSE_SECRET_KEY", "sk-ci")
    os.environ.setdefault(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
    )
    os.environ.setdefault("LITELLM_BASE_URL", "http://localhost:18999")
    os.environ.setdefault("INSIGHTS_LITELLM_API_KEY", "ci-fake")
    os.environ.setdefault("ALEPH_AUTH_ISSUER", "http://localhost:8080/realms/aleph")
    os.environ.setdefault("ALEPH_AUTH_AUDIENCE", "aleph")
    os.environ.setdefault(
        "ALEPH_AUTH_JWKS_URL", "http://localhost:8080/jwks"
    )
    os.environ.setdefault(
        "ALEPH_AGENT_TOKEN_SECRET",
        "ci-agent-token-secret-do-not-use-elsewhere",
    )
    os.environ.setdefault("ALEPH_DEFAULT_MODEL_PROFILE", "aleph-dev")
    os.environ.setdefault("ALEPH_ENV", "ci")


_set_defaults()
_required_env("DATABASE_URL")
_required_env("REDIS_URL")


@pytest.fixture(scope="session")
def fake_user_subject() -> str:
    return f"test-user-{uuid.uuid4()}"


@pytest.fixture(scope="session")
async def asgi_app() -> AsyncIterator[Any]:
    """Construct the FastAPI app once. The lifespan startup connects to
    Postgres + Redis + Langfuse + OTEL collector — those are CI dependencies.
    """
    from aleph_api.main import create_app

    app = create_app()
    # Manually drive the lifespan because httpx.ASGITransport in tests
    # doesn't always trigger it.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as _:
        yield app


@pytest.fixture
async def http_client(asgi_app) -> AsyncIterator[httpx.AsyncClient]:
    """Authenticated test client.

    Auth is mocked by monkey-patching the auth middleware to inject a
    Principal directly. See `_TestAuthMiddleware` setup.
    """
    from starlette.middleware import Middleware

    from aleph_api.middleware.auth import AuthMiddleware  # noqa: F401

    # The test client uses an extra header that the auth middleware sees;
    # we monkey-patch `verify_user_jwt` to accept a fixed token. The
    # actual swap-out happens in `_BypassAuth` patched below.

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app),
        base_url="http://testserver",
        headers={"Authorization": "Bearer test"},
    ) as client:
        yield client
