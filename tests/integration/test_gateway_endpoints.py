"""A project's gateway is a row, its key is encrypted, and "configured" is not "reachable".

WS-MEP-4's data layer, against real Postgres and the real cipher. Every test
here drives `GatewayEndpointService` — nothing constructs a `GatewayEndpoint`
by hand and then asserts on it, because the write path is where the interesting
mistakes live (a `key_version` that does not move with the blob, a ledger
payload carrying the secret).

This file is the SERVICE. The routes that call it, and the two-endpoint
isolation the whole feature exists for, are
`tests/integration/test_gateway_endpoint_routes.py`.

What is still NOT here, and must not be inferred: the agent path
(`copilot_agent`), `app.state.litellm` and the workers' `ctx["litellm_client"]`
all still read `LITELLM_BASE_URL` from Settings. That is MEP-6.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_connectors.credentials import credential_cipher
from aleph_core.errors import NotFound, ValidationFailed
from aleph_db.repos.ledger import LedgerWriter
from aleph_models.endpoints import (
    SOURCE_ROW,
    SOURCE_SETTINGS,
    GatewayEndpointService,
)
from aleph_models.limiter import reset_limiters
from aleph_models.testing import FakeGateway, GatewayConfig

pytestmark = pytest.mark.integration

KEY_A = "a" * 64
KEY_B = "b" * 64
ACTOR = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clean_limiters() -> object:
    reset_limiters()
    yield
    reset_limiters()


def _service(session: AsyncSession, master_key: str = KEY_A) -> GatewayEndpointService:
    return GatewayEndpointService(session, cipher=credential_cipher(master_key=master_key))


async def _upsert(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    name: str,
    base_url: str,
    api_key: str | None,
    is_default: bool = False,
    master_key: str = KEY_A,
) -> uuid.UUID:
    row = await _service(session, master_key).upsert(
        ledger=LedgerWriter(session),
        actor_id=ACTOR,
        actor_kind="user",
        project_id=project_id,
        name=name,
        base_url=base_url,
        api_key=api_key,
        is_default=is_default,
    )
    return row.id


async def test_two_projects_resolve_to_two_different_gateways(
    session: AsyncSession,
) -> None:
    """The whole point of the table. Today both would get `LITELLM_BASE_URL`."""
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    await _upsert(
        session,
        project_id=project_a,
        name="primary",
        base_url="https://gateway-a.invalid",
        api_key="sk-a-not-a-real-key",
        is_default=True,
    )
    await _upsert(
        session,
        project_id=project_b,
        name="primary",
        base_url="https://gateway-b.invalid/",
        api_key="sk-b-not-a-real-key",
        is_default=True,
    )

    svc = _service(session)
    a = await svc.resolve(project_id=project_a)
    b = await svc.resolve(project_id=project_b)

    assert (a.base_url, a.api_key, a.source) == (
        "https://gateway-a.invalid",
        "sk-a-not-a-real-key",
        SOURCE_ROW,
    )
    # The trailing slash was normalised away on the write path: two rows that
    # differ only by it are the same endpoint, and `{base}//v1/...` is a 404.
    assert b.base_url == "https://gateway-b.invalid"
    assert b.api_key == "sk-b-not-a-real-key"
    assert a.endpoint_id != b.endpoint_id


async def test_the_key_is_not_in_the_row_and_not_in_the_ledger(
    session: AsyncSession,
) -> None:
    """Two append-only-ish places a secret must never land in plaintext.

    The settings-card defect this repository already fixed was exactly this
    shape: a field hidden on screen and written verbatim to `card_actions` and
    the ledger, where it is plaintext forever.
    """
    project_id = uuid.uuid4()
    plaintext = "sk-super-secret-gateway-key-0123456789"
    endpoint_id = await _upsert(
        session,
        project_id=project_id,
        name="primary",
        base_url="https://gateway.invalid",
        api_key=plaintext,
    )

    blob = (
        await session.execute(
            text(
                "SELECT api_key_cipher, cipher_scheme, key_version FROM gateway_endpoints "
                "WHERE id = :id"
            ),
            {"id": endpoint_id},
        )
    ).one()
    assert plaintext.encode() not in bytes(blob[0]), "the api key was stored in the clear"
    assert blob[1] == "libsodium-sealed"
    assert blob[2] == "v2", "the row must record the master-key generation that encrypted it"

    payloads = (
        (
            await session.execute(
                text("SELECT payload_jsonb::text FROM action_ledger_events WHERE target_id = :id"),
                {"id": endpoint_id},
            )
        )
        .scalars()
        .all()
    )
    assert payloads, "the write path recorded no ledger event"
    for payload in payloads:
        assert plaintext not in payload, "the api key reached the append-only ledger"


async def test_a_key_this_deployment_cannot_open_is_a_named_failure(
    session: AsyncSession,
) -> None:
    """MEP-4's rotation criterion: detectable, not silent.

    A resolver that fell back to the deployment default here would send this
    project's traffic to a different gateway on a different key, and the first
    symptom would be somebody else's invoice.
    """
    project_id = uuid.uuid4()
    await _upsert(
        session,
        project_id=project_id,
        name="primary",
        base_url="https://gateway.invalid",
        api_key="sk-encrypted-under-key-a",
        master_key=KEY_A,
    )

    stranded = _service(session, master_key=KEY_B)
    with pytest.raises(ValidationFailed) as caught:
        await stranded.resolve(
            project_id=project_id,
            fallback_base_url="https://deployment-default.invalid",
            fallback_api_key="sk-deployment-default",
        )
    message = str(caught.value)
    assert "primary" in message, "the failure must name the endpoint"
    assert "v2" in message, "the failure must name the key version it cannot open"


async def test_the_deployment_default_is_used_only_when_there_is_no_row(
    session: AsyncSession,
) -> None:
    project_id = uuid.uuid4()
    svc = _service(session)

    fell_back = await svc.resolve(
        project_id=project_id,
        fallback_base_url="https://deployment-default.invalid/",
        fallback_api_key="sk-deployment-default",
    )
    assert fell_back.source == SOURCE_SETTINGS
    assert fell_back.endpoint_id is None
    assert fell_back.base_url == "https://deployment-default.invalid"

    await _upsert(
        session,
        project_id=project_id,
        name="primary",
        base_url="https://project-gateway.invalid",
        api_key="sk-project",
    )
    now = await svc.resolve(
        project_id=project_id,
        fallback_base_url="https://deployment-default.invalid",
        fallback_api_key="sk-deployment-default",
    )
    assert now.source == SOURCE_ROW
    assert now.base_url == "https://project-gateway.invalid"


async def test_with_no_row_and_no_deployment_default_there_is_nowhere_to_send_a_call(
    session: AsyncSession,
) -> None:
    """A resolver that invented a URL here would produce a DNS error at call time."""
    with pytest.raises(NotFound):
        await _service(session).resolve(project_id=uuid.uuid4())


async def test_promoting_a_default_demotes_the_previous_one(session: AsyncSession) -> None:
    """Two rows claiming default is a coin toss about which gateway gets billed."""
    project_id = uuid.uuid4()
    await _upsert(
        session,
        project_id=project_id,
        name="old",
        base_url="https://old.invalid",
        api_key="sk-old",
        is_default=True,
    )
    await _upsert(
        session,
        project_id=project_id,
        name="new",
        base_url="https://new.invalid",
        api_key="sk-new",
        is_default=True,
    )
    svc = _service(session)
    assert [r.name for r in await svc.list_for_project(project_id) if r.is_default] == ["new"]
    assert (await svc.resolve(project_id=project_id)).base_url == "https://new.invalid"


async def test_an_edit_that_omits_the_key_keeps_it(session: AsyncSession) -> None:
    """The key cannot be read back, so an operator editing a URL cannot retype it."""
    project_id = uuid.uuid4()
    await _upsert(
        session,
        project_id=project_id,
        name="primary",
        base_url="https://old.invalid",
        api_key="sk-keep-me",
    )
    await _upsert(
        session,
        project_id=project_id,
        name="primary",
        base_url="https://new.invalid",
        api_key=None,
    )
    resolved = await _service(session).resolve(project_id=project_id)
    assert resolved.base_url == "https://new.invalid"
    assert resolved.api_key == "sk-keep-me"

    # An empty string is a different intention and has to stay expressible: a
    # gateway on a private network needs no key.
    await _upsert(
        session,
        project_id=project_id,
        name="primary",
        base_url="https://new.invalid",
        api_key="",
    )
    cleared = await _service(session).resolve(project_id=project_id)
    assert cleared.api_key == ""


async def test_a_probe_records_what_the_endpoint_actually_said(
    session: AsyncSession,
) -> None:
    """Configured and reachable are different claims, and only one is recorded here."""
    project_id = uuid.uuid4()
    fake = FakeGateway(GatewayConfig.well_behaved())
    endpoint_id = await _upsert(
        session,
        project_id=project_id,
        name="primary",
        base_url=fake.base_url,
        api_key=fake.api_key,
    )
    svc = _service(session)

    async with fake.client() as http:
        good = await svc.probe(project_id=project_id, endpoint_id=endpoint_id, client=http)
    assert good.ok
    assert good.model_count == len(GatewayConfig.well_behaved().models)
    assert good.error is None

    row = (
        await session.execute(
            text(
                "SELECT last_probe_ok, last_probe_model_count, last_probe_error, last_probe_at "
                "FROM gateway_endpoints WHERE id = :id"
            ),
            {"id": endpoint_id},
        )
    ).one()
    assert row[0] is True
    assert row[1] == good.model_count
    assert row[2] is None
    assert row[3] is not None, "a probe that records no timestamp cannot go stale"


async def test_a_probe_against_a_gateway_that_refuses_records_its_words(
    session: AsyncSession,
) -> None:
    """ "Something went wrong" sends an operator to the wrong place."""
    project_id = uuid.uuid4()
    endpoint_id = await _upsert(
        session,
        project_id=project_id,
        name="primary",
        base_url="http://unreachable.invalid",
        api_key="sk-whatever",
    )

    def _refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": "Authentication Error: invalid api key"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        bad = await _service(session).probe(
            project_id=project_id, endpoint_id=endpoint_id, client=http
        )
    assert not bad.ok
    assert bad.model_count == 0
    assert bad.error is not None
    assert "invalid api key" in bad.error, "the gateway's own words must survive"

    stored = (
        await session.execute(
            text("SELECT last_probe_ok, last_probe_error FROM gateway_endpoints WHERE id = :id"),
            {"id": endpoint_id},
        )
    ).one()
    assert stored[0] is False
    assert "invalid api key" in stored[1], (
        "a failed probe must be recorded; 'never probed' and 'probed and refused' "
        "are different states and only one needs an operator"
    )


async def test_probing_an_endpoint_in_another_project_is_a_miss(
    session: AsyncSession,
) -> None:
    """Project scoping on the read path, not only on the write path."""
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    endpoint_id = await _upsert(
        session,
        project_id=theirs,
        name="primary",
        base_url="https://theirs.invalid",
        api_key="sk-theirs",
    )
    with pytest.raises(NotFound):
        await _service(session).probe(project_id=mine, endpoint_id=endpoint_id)


async def test_a_probe_reports_whether_the_admin_route_was_allowed(
    session: AsyncSession,
) -> None:
    """ "Restricted key" and "gateway told us nothing" look identical without this.

    A LiteLLM virtual key is restricted to `llm_api_routes`, so `/model/info`
    403s and discovery falls back to `/v1/models` — ids and nothing else. That
    is the NORMAL case and it is not a failure, but it decides whether
    autoconfigure binds on gateway metadata or on an operator's hints file, and
    an operator staring at a model list with no modes and no prices has no way
    to tell which happened.

    Not derivable from the models themselves: `aleph_models.hints` sets
    `metadata_available=True` on a model it fills in, so a hinted id from
    `/v1/models` is indistinguishable from a described one.
    """
    project_id = uuid.uuid4()

    restricted = FakeGateway(GatewayConfig())  # the hostile default: /model/info 403s
    restricted_id = await _upsert(
        session,
        project_id=project_id,
        name="restricted",
        base_url=restricted.base_url,
        api_key=restricted.api_key,
    )
    async with restricted.client() as http:
        got = await _service(session).probe(
            project_id=project_id, endpoint_id=restricted_id, client=http
        )
    assert got.ok, got.error
    assert got.model_info_allowed is False
    assert got.models, "a 403 on the admin route must still yield the ids /v1/models serves"
    assert restricted.count("/v1/models") >= 1, "it did not fall back"

    admin = FakeGateway(GatewayConfig.well_behaved())
    admin_id = await _upsert(
        session,
        project_id=project_id,
        name="admin",
        base_url=admin.base_url,
        api_key=admin.api_key,
    )
    async with admin.client() as http:
        rich = await _service(session).probe(
            project_id=project_id, endpoint_id=admin_id, client=http
        )
    assert rich.model_info_allowed is True
    assert set(rich.models) == {m.id for m in GatewayConfig.well_behaved().models}


async def test_an_unreachable_endpoint_gives_no_verdict_on_the_admin_route(
    session: AsyncSession,
) -> None:
    """`False` here would tell an operator their key is restricted.

    It is not. Their URL is wrong, and the two remedies are nothing like each
    other. `None` is the only honest answer to a question the endpoint never
    got the chance to answer.
    """
    project_id = uuid.uuid4()
    endpoint_id = await _upsert(
        session,
        project_id=project_id,
        name="typo",
        base_url="http://gatewya.invalid",
        api_key="sk-whatever-1234567890",
    )

    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as http:
        got = await _service(session).probe(
            project_id=project_id, endpoint_id=endpoint_id, client=http
        )
    assert not got.ok
    assert got.model_info_allowed is None
    assert got.error is not None
    assert "ConnectError" in got.error, (
        "the exception TYPE is most of the diagnosis for a transport failure"
    )
    assert "nodename nor servname" in got.error


async def test_a_gateway_that_echoes_the_key_does_not_get_it_relayed(
    session: AsyncSession,
) -> None:
    """The one place returning the endpoint's words verbatim can leak the key.

    Some gateways put the offending bearer token in their 401 body. Relaying
    that verbatim puts the key in an HTTP response, in `last_probe_error`, and
    in every log line that quotes it — which is the settings-card defect again,
    arriving through the door built to be honest.
    """
    project_id = uuid.uuid4()
    plaintext = "sk-echoed-back-by-the-gateway-0123456789"
    endpoint_id = await _upsert(
        session,
        project_id=project_id,
        name="chatty",
        base_url="http://chatty.invalid",
        api_key=plaintext,
    )

    def _echo(request: httpx.Request) -> httpx.Response:
        sent = request.headers.get("authorization", "")
        return httpx.Response(401, json={"error": {"message": f"invalid api key: {sent}"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_echo)) as http:
        got = await _service(session).probe(
            project_id=project_id, endpoint_id=endpoint_id, client=http
        )
    assert not got.ok
    assert got.error is not None
    assert plaintext not in got.error, "the endpoint's own key was relayed back to the caller"
    assert "[redacted]" in got.error
    assert "invalid api key" in got.error, "redaction must not eat the diagnosis"

    stored = (
        await session.execute(
            text("SELECT last_probe_error FROM gateway_endpoints WHERE id = :id"),
            {"id": endpoint_id},
        )
    ).scalar_one()
    assert plaintext not in stored


async def test_delete_removes_the_row_and_records_who_and_what(
    session: AsyncSession,
) -> None:
    """`delete` had no caller and no test; it now has both."""
    project_id = uuid.uuid4()
    endpoint_id = await _upsert(
        session,
        project_id=project_id,
        name="retired",
        base_url="https://retired.invalid",
        api_key="sk-retired-key-0123456789",
    )
    removed = await _service(session).delete(
        ledger=LedgerWriter(session),
        actor_id=ACTOR,
        actor_kind="user",
        project_id=project_id,
        endpoint_id=endpoint_id,
    )
    assert removed == "retired"
    assert await _service(session).list_for_project(project_id) == []

    kinds = (
        (
            await session.execute(
                text("SELECT action_kind FROM action_ledger_events WHERE target_id = :id"),
                {"id": endpoint_id},
            )
        )
        .scalars()
        .all()
    )
    assert "gateway_endpoint.delete" in kinds


async def test_deleting_another_projects_endpoint_is_a_miss(session: AsyncSession) -> None:
    """A `False` nobody checks is how a cross-tenant delete reports success."""
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    endpoint_id = await _upsert(
        session,
        project_id=theirs,
        name="theirs",
        base_url="https://theirs.invalid",
        api_key="sk-theirs-0123456789",
    )
    with pytest.raises(NotFound):
        await _service(session).delete(
            ledger=LedgerWriter(session),
            actor_id=ACTOR,
            actor_kind="user",
            project_id=mine,
            endpoint_id=endpoint_id,
        )
    assert len(await _service(session).list_for_project(theirs)) == 1
