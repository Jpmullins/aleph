"""A model change made over HTTP reaches the gateway. WS-MEP-6 c1/c2, c5.

`apps/api/tests/unit/test_agent_profile_switch.py` proves the RESOLVER: given a
bindings loader and an endpoint loader, the seven models a graph is built from
change. What it cannot prove is that the two ends are connected — its rebind is
a dict mutation, its endpoint is a hand-built `ResolvedEndpoint`, and no request
ever leaves the process. The criteria are about a `PATCH /model-profile` and a
gateway that saw the new model, and until this file nothing measured either.

So: the row is written by the real route over real HTTP, the resolution reads it
back through production's `bindings_for_project` / `endpoint_for_project`
against real Postgres, and the model the graph was actually built with is
invoked against a `FakeGateway` that counts what it was asked. The chain has
one substitution — `deepagents.create_deep_agent` is captured so the built model
can be reached, the same technique the unit file uses — and it is downstream of
everything under test.

**Two gateways, and the deployment default is neither.** A test with one fake
cannot tell "resolved the project's row" from "fell through to Settings and
happened to be pointed at the same place", which is precisely the state MEP-6
was in before this: `resolve_agent` hardcoded `settings.litellm_base_url`, both
named gateway tests stayed green, and the endpoint half of the graph-cache
signature was a constant.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Annotated, Any

import deepagents
import httpx
import pytest
from fastapi import Depends, FastAPI, Path
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_api.deps import principal_dep
from aleph_api.middleware.project_scope import project_scope_dep
from aleph_core.schemas.model_profile import ModelBindingIn
from aleph_db.models.model_profile import ModelProfile
from aleph_models.limiter import reset_limiters
from aleph_models.testing import FakeGateway, FakeModel, GatewayConfig
from aleph_security.principal import Principal
from aleph_security.roles import ProjectRole

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000a6")
MASTER_KEY = "6" * 64

#: Deliberately not either fake. If the resolution is ignored the models point
#: here, no fake sees anything, and every assertion below fails loudly instead
#: of one of them passing by coincidence.
DEPLOYMENT_DEFAULT_URL = "http://deployment-default-a6.invalid"


def _fake(host: str, model_ids: tuple[str, ...]) -> FakeGateway:
    fake = FakeGateway(
        GatewayConfig(
            models=tuple(FakeModel(id=m, mode="chat") for m in model_ids),
            api_key=f"sk-{host}-key-0123456789",
            chat_reply=f"answered by {host}",
        )
    )
    fake.base_url = f"http://{host}.invalid"
    return fake


class _HostRouter(httpx.AsyncBaseTransport):
    """Dispatch by hostname. An unclaimed host raises rather than defaulting."""

    def __init__(self, fakes: tuple[FakeGateway, ...]) -> None:
        self._transports = {httpx.URL(f.base_url).host: f.transport() for f in fakes}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._transports.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(
                f"no fake gateway is listening on {request.url.host!r}", request=request
            )
        return await transport.handle_async_request(request)


@pytest.fixture(autouse=True)
def _clean_limiters() -> Any:
    reset_limiters()
    yield
    reset_limiters()


@pytest.fixture
def settings() -> SimpleNamespace:
    return SimpleNamespace(
        aleph_auth_mode="local",
        aleph_agent_token_secret="integration-secret-0123456789abcdef0123456789ab",
        aleph_credential_master_key=MASTER_KEY,
        credential_legacy_key="",
        litellm_base_url=DEPLOYMENT_DEFAULT_URL,
        insights_litellm_api_key="sk-deployment-default-0123456789",
        aleph_agent_request_timeout_s=30.0,
        redis_url="redis://localhost:6379/0",
    )


@pytest.fixture
async def fakes() -> AsyncIterator[tuple[FakeGateway, FakeGateway, httpx.AsyncClient]]:
    a = _fake("gw-agent-a", ("vllm-local-alpha", "vllm-local-alpha-two"))
    b = _fake("gw-agent-b", ("vllm-local-beta",))
    client = httpx.AsyncClient(transport=_HostRouter((a, b)))
    try:
        yield a, b, client
    finally:
        await client.aclose()


@pytest.fixture
def captured_graphs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What `create_deep_agent` was handed. Compiling seven real graphs would
    test LangGraph; what is under test is which models they were built from."""
    seen: list[dict[str, Any]] = []

    def _capture(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return SimpleNamespace(nodes={}, aleph_captured=kwargs)

    monkeypatch.setattr(deepagents, "create_deep_agent", _capture)
    return seen


@pytest.fixture
def routed_agent_transport(
    monkeypatch: pytest.MonkeyPatch, fakes: tuple[FakeGateway, FakeGateway, httpx.AsyncClient]
) -> None:
    """Put the agent's own HTTP client on the host router.

    `ChatOpenAI` builds its own transport, which is why `_gateway_chat_model`
    hands it `shared_gateway_client(base_url, ...)`. Substituting that one
    factory is the smallest possible intervention that lets a real `ainvoke`
    reach an in-process gateway — the `base_url` the model was configured with
    is still the thing that decides which fake answers.
    """
    from aleph_models import limiter as limiter_mod

    _a, _b, http = fakes

    def _client(base_url: str, **_kw: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=http._transport, base_url=base_url)

    monkeypatch.setattr(limiter_mod, "shared_gateway_client", _client)


@pytest.fixture
def bound_runtime(
    monkeypatch: pytest.MonkeyPatch,
    maker: Callable[[], AsyncSession],
    settings: SimpleNamespace,
) -> Any:
    """Bind the module-global agent runtime, and put it back afterwards.

    `copilot_agent._runtime` is process-wide; a test that leaves it changed
    decides what the next test resolves.
    """
    from aleph_api import copilot_agent

    previous = dict(copilot_agent._runtime)
    copilot_agent._runtime["session_maker"] = maker
    copilot_agent._runtime["settings"] = settings
    copilot_agent._runtime.pop("agent_bindings", None)
    try:
        yield copilot_agent
    finally:
        copilot_agent._runtime.clear()
        copilot_agent._runtime.update(previous)


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    maker: Callable[[], AsyncSession],
    settings: SimpleNamespace,
    gateway_http: httpx.AsyncClient,
) -> FastAPI:
    from aleph_api.main import create_app
    from aleph_api.middleware import auth as auth_mw

    app = create_app()
    app.state.settings = settings
    app.state.session_maker = maker
    app.state.gateway_http = gateway_http

    principal = Principal(user_id=ACTOR, subject="agent-profile-wire", email="", actor_kind="user")

    async def _fake_local_dev(_request: Any) -> Principal:
        return principal

    monkeypatch.setattr(auth_mw, "_principal_local_dev", _fake_local_dev)

    async def _scope(
        project_id: Annotated[uuid.UUID, Path(...)],
        p: Annotated[Principal, Depends(principal_dep)],
    ) -> uuid.UUID:
        p.cache_role(project_id, ProjectRole.OWNER.value)
        return project_id

    app.dependency_overrides[project_scope_dep] = _scope
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://agent-profile-wire",
        timeout=30.0,
    )


async def _seed_profile(
    maker: Callable[[], AsyncSession], project_id: uuid.UUID, model: str
) -> None:
    async with maker() as session:
        session.add(
            ModelProfile(
                id=uuid.uuid4(),
                name=f"fixture-{project_id.hex[:8]}",
                project_id=project_id,
                is_template=False,
                # Built through the schema, not hand-written: `ModelProfileOut`
                # requires four fields a two-key literal omits, so a hand-built
                # fixture makes the PATCH route 500 on serialising the row it
                # just wrote — a failure about the fixture, not the feature.
                bindings_jsonb={
                    cap: ModelBindingIn(model=model).model_dump(mode="json")
                    for cap in ("synthesis", "judge", "code")
                },
                created_by=ACTOR,
            )
        )
        await session.commit()


async def _put_endpoint(
    client: httpx.AsyncClient, project_id: uuid.UUID, fake: FakeGateway
) -> None:
    response = await client.put(
        f"/v1/projects/{project_id}/gateway-endpoints",
        json={
            "name": "primary",
            "base_url": fake.base_url,
            "api_key": fake.api_key,
            "is_default": True,
        },
    )
    assert response.status_code == 200, response.text


async def _patch_model(
    client: httpx.AsyncClient, project_id: uuid.UUID, model: str
) -> httpx.Response:
    return await client.patch(
        f"/v1/projects/{project_id}/model-profile",
        json={"bindings": {"synthesis": {"model": model, "provider": "litellm"}}},
    )


async def _turn(agent_module: Any, project_id: uuid.UUID, settings: SimpleNamespace) -> Any:
    """One resolution, built and INVOKED. The production path, end to end.

    `assistant_agent_resolver` with neither loader injected: `resolve_agent`
    calls `bindings_for_project` and `endpoint_for_project`, both of which open
    a session on the `session_maker` bound above and read committed rows.
    """
    resolve = agent_module.assistant_agent_resolver(
        settings=settings,
        store=None,
        cache=agent_module.BoundedGraphCache(agent_module.AGENT_GRAPH_CACHE_MAX),
    )
    agent = await resolve(project_id)
    return agent.graph.aleph_captured["model"]


# ---------------------------------------------------------------------------
# c1 / c2 over the wire
# ---------------------------------------------------------------------------


async def test_a_patch_over_http_changes_the_model_the_gateway_is_asked_for(
    monkeypatch: pytest.MonkeyPatch,
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    settings: SimpleNamespace,
    fakes: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
    captured_graphs: list[dict[str, Any]],
    routed_agent_transport: None,
    bound_runtime: Any,
) -> None:
    """c2, measured at the gateway rather than at the dict.

    The rebind is a real `PATCH /v1/projects/{id}/model-profile`, and the proof
    is a request body the fake recorded — not the value the resolver reports
    about itself.
    """
    fake_a, _fake_b, http = fakes
    await _seed_profile(maker, committed_project, "vllm-local-alpha")
    app = _build_app(monkeypatch, maker, settings, http)

    async with _client(app) as client:
        await _put_endpoint(client, committed_project, fake_a)

        first = await _turn(bound_runtime, committed_project, settings)
        await first.ainvoke("ping")
        assert fake_a.count("/v1/chat/completions") == 1
        assert _models_asked(fake_a) == ["vllm-local-alpha"]

        patched = await _patch_model(client, committed_project, "vllm-local-alpha-two")
        assert patched.status_code == 200, patched.text

        second = await _turn(bound_runtime, committed_project, settings)
        await second.ainvoke("ping")

    assert _models_asked(fake_a) == ["vllm-local-alpha", "vllm-local-alpha-two"], (
        "the PATCH changed a row and the next turn asked for the old model — "
        "the binding is not reaching the wire"
    )


async def test_a_gateway_endpoint_written_over_http_is_the_one_the_agent_calls(
    monkeypatch: pytest.MonkeyPatch,
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    second_project: uuid.UUID,
    settings: SimpleNamespace,
    fakes: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
    captured_graphs: list[dict[str, Any]],
    routed_agent_transport: None,
    bound_runtime: Any,
) -> None:
    """c1 and c5 together: two projects, two gateways, two model names.

    This is the assertion MEP-6 c5 could not make. Both of its named tests were
    green while `resolve_agent` read `settings.litellm_base_url`, because
    neither of them ever resolves an endpoint that differs from it.
    """
    fake_a, fake_b, http = fakes
    await _seed_profile(maker, committed_project, "vllm-local-alpha")
    await _seed_profile(maker, second_project, "vllm-local-beta")
    app = _build_app(monkeypatch, maker, settings, http)

    async with _client(app) as client:
        await _put_endpoint(client, committed_project, fake_a)
        await _put_endpoint(client, second_project, fake_b)

        b_model = await _turn(bound_runtime, second_project, settings)
        a_before = fake_a.count("/v1/chat/completions")
        await b_model.ainvoke("ping")
        assert fake_b.count("/v1/chat/completions") == 1
        assert fake_a.count("/v1/chat/completions") == a_before

        a_model = await _turn(bound_runtime, committed_project, settings)
        b_after = fake_b.count("/v1/chat/completions")
        await a_model.ainvoke("ping")

    assert fake_a.count("/v1/chat/completions") == 1
    assert fake_b.count("/v1/chat/completions") == b_after
    assert _models_asked(fake_a) == ["vllm-local-alpha"]
    assert _models_asked(fake_b) == ["vllm-local-beta"]


async def test_the_bearer_the_agent_sends_is_the_endpoints_own_key(
    monkeypatch: pytest.MonkeyPatch,
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    settings: SimpleNamespace,
    fakes: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
    captured_graphs: list[dict[str, Any]],
    routed_agent_transport: None,
    bound_runtime: Any,
) -> None:
    """`require_auth` is on by default, so a wrong key is a 401 the fake raises.

    Reaching the right URL with the deployment's credential is the half of a
    partial refactor that a base-url assertion cannot see.
    """
    fake_a, _fake_b, http = fakes
    await _seed_profile(maker, committed_project, "vllm-local-alpha")
    app = _build_app(monkeypatch, maker, settings, http)

    async with _client(app) as client:
        await _put_endpoint(client, committed_project, fake_a)
        model = await _turn(bound_runtime, committed_project, settings)
        reply = await model.ainvoke("ping")

    assert reply.content == "answered by gw-agent-a"
    sent = [r.authorization for r in fake_a.requests if r.path.endswith("completions")]
    assert sent == [f"Bearer {fake_a.api_key}"]
    assert settings.insights_litellm_api_key not in str(sent)


def _models_asked(fake: FakeGateway) -> list[str]:
    """The `model` field of every chat request this gateway answered."""
    return [
        str(r.body.get("model"))
        for r in fake.requests
        if r.path.endswith("/chat/completions") and r.body is not None
    ]
