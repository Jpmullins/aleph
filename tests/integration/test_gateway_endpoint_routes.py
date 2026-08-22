"""Two projects, two gateways, over HTTP. WS-MEP-4 c2, c3, c5, c7.

The data layer landed and nothing called it: `grep -rn "GatewayEndpointService"
apps packages` outside its own module returned nothing at all, and the table's
own test file said so in its docstring. A row nobody can reach is not a
feature, and this is the file that decides whether the routes make it one.

**Why two real fakes and not one mock.** The claim under test is *isolation* —
"a call made under project B's scope reaches B's endpoint and never A's". A
single fake with a spy on the URL would let a resolver that returns the right
`base_url` and then sends the request somewhere else pass. Two
`FakeGateway`s behind a host-routing transport, each counting its own traffic,
make the claim measurable: A's counter must not move.

**The app is booted without its lifespan.** The kernel is not the subject; the
routes are. `session_maker`, `settings`, `gateway_http` and the principal seam
are supplied by hand, which is the pattern `test_vault_export_route.py`
established, and the session is real Postgres because the endpoint rows, their
ciphertext and their ledger events all have to survive a commit.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aleph_api.deps import principal_dep
from aleph_api.middleware.project_scope import project_scope_dep
from aleph_models.limiter import reset_limiters
from aleph_models.testing import FakeGateway, FakeModel, GatewayConfig
from aleph_security.principal import Principal
from aleph_security.roles import ProjectRole

pytestmark = pytest.mark.integration

#: 64 hex-ish characters; `credential_cipher` derives the sealed-box secret
#: from it. The same shape the service tests use.
MASTER_KEY = "c" * 64

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000ce")

#: The deployment default the fallback path resolves to when a project has no
#: row. Deliberately not either fake: a test that could not tell "resolved the
#: row" from "fell through to Settings" would pass on a resolver that never
#: read the table at all.
DEPLOYMENT_DEFAULT_URL = "http://deployment-default.invalid"
DEPLOYMENT_DEFAULT_KEY = "sk-deployment-default-0123456789"


@pytest.fixture(autouse=True)
def _clean_limiters() -> Any:
    reset_limiters()
    yield
    reset_limiters()


# ---------------------------------------------------------------------------
# Two gateways, told apart by host
# ---------------------------------------------------------------------------


def _fake(host: str, model_ids: tuple[str, ...]) -> FakeGateway:
    """A fake at its own address, serving its own models, with its own key.

    `FakeGateway.base_url` is a CLASS attribute, so two instances share one
    address unless it is shadowed per instance — and two endpoints at the same
    URL cannot demonstrate routing. The model ids are invented (`vllm-local-…`
    shape) rather than real names, because `aleph_models.hints` recognises real
    ones and would fill in metadata the gateway never reported.
    """
    fake = FakeGateway(
        GatewayConfig(
            models=tuple(FakeModel(id=mid) for mid in model_ids),
            api_key=f"sk-{host}-key-0123456789",
        )
    )
    fake.base_url = f"http://{host}.invalid"
    return fake


class _HostRouter(httpx.AsyncBaseTransport):
    """Dispatch by hostname to whichever fake owns it.

    This stands in for DNS plus the network. A request for a host nobody
    claimed raises rather than falling through to a default — a silent default
    is precisely the bug this suite exists to catch, and it must not be
    reproduced in the harness that checks for it.
    """

    def __init__(self, fakes: dict[str, FakeGateway]) -> None:
        self._transports = {host: f.transport() for host, f in fakes.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        transport = self._transports.get(host)
        if transport is None:
            raise httpx.ConnectError(f"no fake gateway is listening on {host!r}", request=request)
        return await transport.handle_async_request(request)


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    principal: Principal,
    *,
    gateway_http: httpx.AsyncClient,
    role: ProjectRole = ProjectRole.OWNER,
) -> FastAPI:
    from aleph_api.main import create_app
    from aleph_api.middleware import auth as auth_mw

    app = create_app()
    app.state.settings = SimpleNamespace(
        aleph_auth_mode="local",
        aleph_agent_token_secret="integration-secret-0123456789abcdef0123456789ab",
        aleph_credential_master_key=MASTER_KEY,
        credential_legacy_key="",
        litellm_base_url=DEPLOYMENT_DEFAULT_URL,
        insights_litellm_api_key=DEPLOYMENT_DEFAULT_KEY,
    )
    app.state.session_maker = maker
    app.state.gateway_http = gateway_http

    async def _fake_local_dev(_request: Any) -> Principal:
        return principal

    monkeypatch.setattr(auth_mw, "_principal_local_dev", _fake_local_dev)

    async def _scope(
        project_id: Annotated[uuid.UUID, Path(...)],
        p: Annotated[Principal, Depends(principal_dep)],
    ) -> uuid.UUID:
        p.cache_role(project_id, role.value)
        return project_id

    app.dependency_overrides[project_scope_dep] = _scope
    return app


def _principal() -> Principal:
    return Principal(user_id=ACTOR, subject="gateway-endpoint-routes", email="", actor_kind="user")


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://gateway-endpoint-routes",
        timeout=30.0,
    )


@pytest.fixture
async def gateways() -> AsyncIterator[tuple[FakeGateway, FakeGateway, httpx.AsyncClient]]:
    a = _fake("gw-a", ("vllm-local-alpha", "vllm-local-alpha-embed"))
    b = _fake("gw-b", ("vllm-local-beta", "vllm-local-beta-embed"))
    # Keyed on the FULL host each fake actually answers to, read back off its
    # own `base_url`. Writing the hostnames out again here is how the map and
    # the fakes drift apart, and the symptom is every request failing to
    # resolve — which reads exactly like the feature being broken.
    client = httpx.AsyncClient(
        transport=_HostRouter({httpx.URL(f.base_url).host: f for f in (a, b)})
    )
    try:
        yield a, b, client
    finally:
        await client.aclose()


async def _put(client: httpx.AsyncClient, project_id: uuid.UUID, **body: Any) -> httpx.Response:
    return await client.put(f"/v1/projects/{project_id}/gateway-endpoints", json=body)


# ---------------------------------------------------------------------------
# c7 — the key never leaves the server
# ---------------------------------------------------------------------------


def _paths_containing(node: Any, needle: str, path: str = "$") -> list[str]:
    """Every JSON path at which `needle` appears, however deeply nested.

    Written as a walk rather than as `assert body["api_key"] is None` on
    purpose. Checking three known keys is a test of the fields somebody
    remembered; the leak that matters is the one added later, inside a nested
    object, by an author who did not read this file. A walk cannot be outrun by
    a new field.

    Keys are checked as well as values: a response shaped
    `{"sk-real-key": "..."}` leaks just as thoroughly as one with the key on
    the right-hand side.
    """
    found: list[str] = []
    if isinstance(node, str):
        if needle in node:
            found.append(path)
    elif isinstance(node, dict):
        for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
            if isinstance(key, str) and needle in key:
                found.append(f"{path}.<key {key!r}>")
            found.extend(_paths_containing(value, needle, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):  # pyright: ignore[reportUnknownVariableType]
            found.extend(_paths_containing(value, needle, f"{path}[{index}]"))
    return found


def test_the_walker_finds_a_key_a_three_field_check_would_miss() -> None:
    """The leak detector, tested. A walker that finds nothing proves nothing.

    Every assertion below is `_paths_containing(...) == []`, which is exactly
    what a broken walker returns for a leaking response. So it has to be shown
    finding a key in the shapes a real regression would take: nested, inside a
    list, and used as a key rather than a value.
    """
    secret = "sk-leaked-0123456789"
    assert _paths_containing({"endpoint": {"detail": {"auth": secret}}}, secret) == [
        "$.endpoint.detail.auth"
    ]
    assert _paths_containing({"models": ["fine", f"bearer {secret}"]}, secret) == ["$.models[1]"]
    assert _paths_containing({secret: "value"}, secret) == [f"$.<key {secret!r}>"]
    assert _paths_containing({"error": "invalid api key"}, secret) == []


async def test_the_key_never_leaves_the_server_through_any_route(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    gateways: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """c7's response half. Every response a caller can obtain, walked.

    The write path's half — the row and the ledger — is
    `test_gateway_endpoints.py::test_the_key_is_not_in_the_row_and_not_in_the_ledger`.
    This is the other door: an operator can only ever have typed the key into
    this router, so this router is where it would come back out.
    """
    fake_a, _fake_b, http = gateways
    plaintext = "sk-typed-by-an-operator-0123456789"
    app = _build_app(monkeypatch, maker, _principal(), gateway_http=http)

    async with await _client(app) as client:
        created = await _put(
            client,
            committed_project,
            name="primary",
            base_url=fake_a.base_url,
            api_key=plaintext,
            is_default=True,
        )
        assert created.status_code == 200, created.text
        endpoint_id = created.json()["id"]

        listed = await client.get(f"/v1/projects/{committed_project}/gateway-endpoints")
        tested = await client.post(
            f"/v1/projects/{committed_project}/gateway-endpoints/{endpoint_id}/test"
        )
        models = await client.get(f"/v1/projects/{committed_project}/gateway/models")

    for label, response in (
        ("PUT", created),
        ("GET list", listed),
        ("POST test", tested),
        ("GET models", models),
    ):
        assert response.status_code == 200, f"{label}: {response.text}"
        leaks = _paths_containing(response.json(), plaintext)
        assert leaks == [], f"{label} returned the api key at {leaks}"
        # Belt and braces: a key smuggled out in a header, or in a body the
        # JSON walk cannot reach, is still a key that left the server.
        assert plaintext not in response.text, f"{label} body contains the key"
        assert plaintext not in str(dict(response.headers)), f"{label} headers contain the key"

    # And what IS returned has to be enough to administer the thing, or the
    # redaction is just a hole: "is a key set" and "under which generation".
    row = created.json()
    assert row["has_api_key"] is True
    assert row["cipher_scheme"] == "libsodium-sealed"
    assert row["key_version"] == "v2"


# ---------------------------------------------------------------------------
# c2 + c3 — two projects, two gateways
# ---------------------------------------------------------------------------


async def test_two_projects_see_two_disjoint_model_lists(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    second_project: uuid.UUID,
    gateways: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """c2 and c3 in one run, because they are one claim seen two ways.

    c2 is what the operator sees: the model picker for project A offers A's
    models. c3 is what the network saw: A's gateway was the one asked. Either
    alone can pass while the feature is broken — a resolver that returns the
    right list from a cache keyed on nothing would satisfy c2, and one that
    reaches the right host and then renders the boot catalog would satisfy c3.
    """
    fake_a, fake_b, http = gateways
    app = _build_app(monkeypatch, maker, _principal(), gateway_http=http)

    async with await _client(app) as client:
        for project_id, fake in ((committed_project, fake_a), (second_project, fake_b)):
            created = await _put(
                client,
                project_id,
                name="primary",
                base_url=fake.base_url,
                api_key=fake.api_key,
                is_default=True,
            )
            assert created.status_code == 200, created.text

        # B first, so that "A's list" cannot be whatever was fetched last.
        b_body = (await client.get(f"/v1/projects/{second_project}/gateway/models")).json()
        b_requests = fake_b.request_count
        a_before = fake_a.request_count

        a_body = (await client.get(f"/v1/projects/{committed_project}/gateway/models")).json()

    a_models = {m["id"] for m in a_body["models"]}
    b_models = {m["id"] for m in b_body["models"]}
    assert a_models == {"vllm-local-alpha", "vllm-local-alpha-embed"}, a_body
    assert b_models == {"vllm-local-beta", "vllm-local-beta-embed"}, b_body
    assert a_models.isdisjoint(b_models)

    # Which endpoint each answer came from, said out loud.
    assert a_body["endpoint"]["base_url"] == fake_a.base_url
    assert a_body["endpoint"]["source"] == "row"
    assert b_body["endpoint"]["base_url"] == fake_b.base_url

    # c3: the request counters. B's moved when B was asked; A's did not.
    assert a_before == 0, "project A's gateway was contacted while serving project B"
    assert b_requests > 0, "project B's gateway was never contacted at all"
    assert fake_a.request_count > 0, "project A's gateway was never contacted either"
    assert fake_b.request_count == b_requests, (
        "project B's gateway was contacted while serving project A"
    )


async def test_a_project_with_no_row_falls_through_to_the_deployment_default(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    gateways: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """The adoption path, and the guard on the test above.

    Without this, a resolver that ignored the table entirely and always
    returned Settings would still look isolated if Settings happened to point
    at the right place. Here nothing is listening on the deployment default, so
    the answer is a named error rather than a list — and `source` says which
    door was taken.
    """
    _fake_a, _fake_b, http = gateways
    app = _build_app(monkeypatch, maker, _principal(), gateway_http=http)

    async with await _client(app) as client:
        body = (await client.get(f"/v1/projects/{committed_project}/gateway/models")).json()

    assert body["endpoint"]["source"] == "settings"
    assert body["endpoint"]["base_url"] == DEPLOYMENT_DEFAULT_URL
    assert body["endpoint"]["endpoint_id"] is None
    assert body["models"] == []
    # An unreachable gateway must not present as a gateway serving nothing.
    assert body["error"] is not None
    assert "ConnectError" in body["error"]


async def test_one_project_cannot_reach_another_projects_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    second_project: uuid.UUID,
    gateways: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """An id is guessable; scoping the read is what makes that not matter."""
    fake_a, _fake_b, http = gateways
    app = _build_app(monkeypatch, maker, _principal(), gateway_http=http)

    async with await _client(app) as client:
        created = await _put(
            client,
            second_project,
            name="theirs",
            base_url=fake_a.base_url,
            api_key=fake_a.api_key,
        )
        theirs = created.json()["id"]

        probed = await client.post(
            f"/v1/projects/{committed_project}/gateway-endpoints/{theirs}/test"
        )
        removed = await client.delete(
            f"/v1/projects/{committed_project}/gateway-endpoints/{theirs}"
        )
        listed = await client.get(f"/v1/projects/{committed_project}/gateway-endpoints")

    assert probed.status_code == 404, probed.text
    assert removed.status_code == 404, removed.text
    assert listed.json() == []


# ---------------------------------------------------------------------------
# c5 — the test-connection route reports what the endpoint said
# ---------------------------------------------------------------------------


async def test_test_connection_reports_a_restricted_key_as_a_fact_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    gateways: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """c5's first half, exactly as written: 403 on `/model/info`, models anyway.

    This is the shape of every real LiteLLM virtual key, so a probe that
    reported it as a failure would tell every normal operator their working
    gateway is broken.
    """
    fake_a, _fake_b, http = gateways
    app = _build_app(monkeypatch, maker, _principal(), gateway_http=http)

    async with await _client(app) as client:
        created = await _put(
            client,
            committed_project,
            name="primary",
            base_url=fake_a.base_url,
            api_key=fake_a.api_key,
        )
        endpoint_id = created.json()["id"]
        result = await client.post(
            f"/v1/projects/{committed_project}/gateway-endpoints/{endpoint_id}/test"
        )

    assert result.status_code == 200, result.text
    body = result.json()
    assert body["ok"] is True
    assert body["model_info_allowed"] is False
    assert sorted(body["models"]) == ["vllm-local-alpha", "vllm-local-alpha-embed"]
    assert body["model_count"] == 2
    assert body["error"] is None
    # Recorded on the row, not only returned: "never probed" and "probed and
    # answered" are different states and only the operator can see the second
    # if it is stored.
    assert body["endpoint"]["last_probe_ok"] is True
    assert body["endpoint"]["last_probe_model_count"] == 2
    assert body["endpoint"]["last_probe_at"] is not None
    assert fake_a.count("/model/info") >= 1, "the admin route was never tried"


async def test_test_connection_returns_the_transport_error_for_a_url_that_answers_nothing(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    gateways: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """c5's second half. A typo in the host is the commonest way this fails.

    "Connection failed" would send an operator to look at their network. The
    upstream text is the diagnosis, and `model_info_allowed: null` — rather
    than `false` — keeps the route from asserting their key is restricted when
    nothing ever read it.
    """
    _fake_a, _fake_b, http = gateways
    app = _build_app(monkeypatch, maker, _principal(), gateway_http=http)

    async with await _client(app) as client:
        created = await _put(
            client,
            committed_project,
            name="typo",
            base_url="http://gw-typo.invalid",
            api_key="sk-gw-typo-key-0123456789",
        )
        endpoint_id = created.json()["id"]
        result = await client.post(
            f"/v1/projects/{committed_project}/gateway-endpoints/{endpoint_id}/test"
        )

    assert result.status_code == 200, "the gateway failed; Aleph did not"
    body = result.json()
    assert body["ok"] is False
    assert body["model_info_allowed"] is None
    assert body["models"] == []
    assert body["error"] is not None
    assert "ConnectError" in body["error"]
    assert "gw-typo" in body["error"], "the operator's own typo must appear in the message"
    assert body["endpoint"]["last_probe_ok"] is False
    assert "gw-typo" in body["endpoint"]["last_probe_error"]


# ---------------------------------------------------------------------------
# CRUD and its gates
# ---------------------------------------------------------------------------


async def test_an_edit_that_omits_the_key_keeps_it_and_a_blank_one_clears_it(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    gateways: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """The consequence of never returning the key: an edit cannot retype it.

    Three intentions, three request shapes, and the round trip through HTTP is
    the part that matters — `api_key` absent from a JSON body has to arrive as
    `None` and not as `""`, or every URL edit silently wipes the credential.
    """
    fake_a, _fake_b, http = gateways
    app = _build_app(monkeypatch, maker, _principal(), gateway_http=http)

    async with await _client(app) as client:
        await _put(
            client,
            committed_project,
            name="primary",
            base_url="http://old.invalid",
            api_key=fake_a.api_key,
        )
        # No `api_key` key at all in the JSON — the shape a UI that cannot
        # populate a password field must send.
        edited = await _put(client, committed_project, name="primary", base_url=fake_a.base_url)
        assert edited.json()["has_api_key"] is True
        assert edited.json()["base_url"] == fake_a.base_url

        endpoint_id = edited.json()["id"]
        # The key survived, and the proof is that the fake accepts it.
        probed = await client.post(
            f"/v1/projects/{committed_project}/gateway-endpoints/{endpoint_id}/test"
        )
        assert probed.json()["ok"] is True, probed.text

        cleared = await _put(
            client,
            committed_project,
            name="primary",
            base_url=fake_a.base_url,
            api_key="",
        )

    assert cleared.json()["has_api_key"] is False
    assert cleared.json()["key_version"] is None


async def test_promoting_a_default_over_http_demotes_the_previous_one(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    gateways: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """Two rows claiming default is a coin toss about which gateway gets billed."""
    fake_a, fake_b, http = gateways
    app = _build_app(monkeypatch, maker, _principal(), gateway_http=http)

    async with await _client(app) as client:
        await _put(
            client,
            committed_project,
            name="old",
            base_url=fake_a.base_url,
            api_key=fake_a.api_key,
            is_default=True,
        )
        await _put(
            client,
            committed_project,
            name="new",
            base_url=fake_b.base_url,
            api_key=fake_b.api_key,
            is_default=True,
        )
        listed = (await client.get(f"/v1/projects/{committed_project}/gateway-endpoints")).json()
        models = (await client.get(f"/v1/projects/{committed_project}/gateway/models")).json()

    assert [r["name"] for r in listed if r["is_default"]] == ["new"]
    assert models["endpoint"]["base_url"] == fake_b.base_url


async def test_delete_over_http_removes_the_row(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    gateways: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    fake_a, _fake_b, http = gateways
    app = _build_app(monkeypatch, maker, _principal(), gateway_http=http)

    async with await _client(app) as client:
        created = await _put(
            client,
            committed_project,
            name="primary",
            base_url=fake_a.base_url,
            api_key=fake_a.api_key,
        )
        endpoint_id = created.json()["id"]
        removed = await client.delete(
            f"/v1/projects/{committed_project}/gateway-endpoints/{endpoint_id}"
        )
        listed = await client.get(f"/v1/projects/{committed_project}/gateway-endpoints")

    assert removed.status_code == 204, removed.text
    assert listed.json() == []

    async with maker() as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM gateway_endpoints WHERE id = :id"),
                {"id": uuid.UUID(endpoint_id)},
            )
        ).scalar_one()
    assert remaining == 0


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("GET", "/gateway-endpoints", None),
        ("PUT", "/gateway-endpoints", {"name": "x", "base_url": "http://x.invalid"}),
    ],
)
async def test_an_editor_cannot_read_or_write_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    committed_project: uuid.UUID,
    gateways: tuple[FakeGateway, FakeGateway, httpx.AsyncClient],
    method: str,
    suffix: str,
    body: dict[str, Any] | None,
) -> None:
    """OWNER, and the gate is asserted rather than assumed.

    An EDITOR is the interesting principal, not an anonymous caller: they pass
    `project_scope_dep` and reach the handler, so only `require_at_least`
    stands between them and a base URL plus an operator's probe errors.
    """
    _fake_a, _fake_b, http = gateways
    app = _build_app(monkeypatch, maker, _principal(), gateway_http=http, role=ProjectRole.EDITOR)

    async with await _client(app) as client:
        response = await client.request(
            method, f"/v1/projects/{committed_project}{suffix}", json=body
        )

    assert response.status_code == 403, response.text
