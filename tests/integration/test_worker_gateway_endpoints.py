"""A background job reaches the PROJECT's gateway. WS-MEP-4 c6, worker half.

The API request path was fixed first and the worker process was not, so the
defect survived in the place that spends the most: `arq.py` read one
`LiteLLMClient` out of the kernel at boot and eleven job modules read it back
off `ctx["litellm_client"]`. A project could write a `gateway_endpoints` row,
see it read back correctly on the settings screen, and have every ingest,
embed, curation, research and reviewer call keep going to `LITELLM_BASE_URL`.
Nothing said so, because a row that is read correctly and ignored produces no
error anywhere — only somebody else's bill.

**Two real fakes, not one mock.** The claim is isolation, and a spy on the
resolved `base_url` cannot see the difference between "resolved A and called A"
and "resolved A and called the boot client". Each fake counts its own traffic
behind a host-routing transport, so A's counter not moving is a statement about
what left the process.

**The job is real.** `smoke_llm_job` is driven end to end — token verification,
the profile read, `LiteLLMClient.chat`, the `ModelCall` write — against a
`WorkerGateways` built exactly as `aleph_workers.arq._startup` builds it. The
only substitutions are the HTTP transport and a settings object; the resolution
path, the cipher, the registries and the client are production code.

The third property is the one a two-endpoint test usually forgets: a project
with **no** row must still reach the deployment default, or MEP-4 is a flag day
rather than an adoption. That is a third fake at a third address, so "fell
through to Settings" cannot be confused with "found a row".
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_connectors.credentials import credential_cipher
from aleph_db.models.cost import ModelCall
from aleph_db.models.model_profile import ModelProfile
from aleph_db.repos.ledger import LedgerWriter
from aleph_models.endpoints import SOURCE_ROW, SOURCE_SETTINGS, GatewayEndpointService
from aleph_models.limiter import reset_limiters
from aleph_models.testing import FakeGateway, FakeModel, GatewayConfig
from aleph_security.agent_token import mint_agent_token
from aleph_workers.gateway import WorkerGateways
from aleph_workers.jobs.smoketest import smoke_llm_job

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-00000000000d")
SECRET = "worker-gateway-secret-that-is-at-least-32-bytes"
MASTER_KEY = "d" * 64

#: The deployment default, and it is a THIRD gateway rather than either fake.
#: A fallback pointed at fake A would make "resolved A's row" and "fell through
#: to Settings" produce identical traffic, and the test could not tell them
#: apart — which is the whole distinction `ResolvedEndpoint.source` exists for.
DEFAULT_HOST = "gw-deployment-default"


def _fake(host: str, model_id: str) -> FakeGateway:
    """A fake at its own address, serving one model, with its own key.

    `FakeGateway.base_url` is a CLASS attribute, so two instances share one
    address unless it is shadowed per instance. The model ids are invented
    (`vllm-local-…`) because `aleph_models.hints` recognises real names and
    would supply metadata the gateway never reported.
    """
    fake = FakeGateway(
        GatewayConfig(
            models=(FakeModel(id=model_id, mode="chat", supports_function_calling=True),),
            api_key=f"sk-{host}-key-0123456789",
            chat_reply=f"answered by {host}",
        )
    )
    fake.base_url = f"http://{host}.invalid"
    return fake


class _HostRouter(httpx.AsyncBaseTransport):
    """DNS plus the network, for three in-process gateways.

    A request for a host nobody claimed raises rather than falling through to a
    default: a silent default is exactly the defect under test, and reproducing
    it inside the harness would make every assertion below meaningless.
    """

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
async def fakes() -> AsyncIterator[tuple[FakeGateway, FakeGateway, FakeGateway, httpx.AsyncClient]]:
    a = _fake("gw-worker-a", "vllm-local-alpha")
    b = _fake("gw-worker-b", "vllm-local-beta")
    fallback = _fake(DEFAULT_HOST, "vllm-local-default")
    client = httpx.AsyncClient(transport=_HostRouter((a, b, fallback)))
    try:
        yield a, b, fallback, client
    finally:
        await client.aclose()


def _settings(fallback: FakeGateway) -> SimpleNamespace:
    """Exactly the fields `WorkerGateways` reads off worker settings."""
    return SimpleNamespace(
        aleph_credential_master_key=MASTER_KEY,
        credential_legacy_key="",
        litellm_base_url=fallback.base_url,
        insights_litellm_api_key=fallback.api_key,
    )


def _resolver(
    maker: Callable[[], AsyncSession], fallback: FakeGateway, http: httpx.AsyncClient
) -> WorkerGateways:
    """Built the way `aleph_workers.arq._startup` builds it, minus the kernel."""
    from aleph_models.pricing import PricingTable

    return WorkerGateways(
        settings=_settings(fallback),
        session_maker=maker,
        pricing=PricingTable({}),
        http_client=http,
    )


async def _bind(
    maker: Callable[[], AsyncSession],
    project_id: uuid.UUID,
    *,
    fake: FakeGateway | None,
    model_id: str,
) -> None:
    """Give the project a model profile, and optionally its own endpoint row."""
    async with maker() as session:
        session.add(
            ModelProfile(
                id=uuid.uuid4(),
                name=f"fixture-{project_id.hex[:8]}",
                project_id=project_id,
                is_template=False,
                bindings_jsonb={"classification": {"model": model_id, "provider": "litellm"}},
                created_by=ACTOR,
            )
        )
        if fake is not None:
            await GatewayEndpointService(
                session, cipher=credential_cipher(master_key=MASTER_KEY)
            ).upsert(
                ledger=LedgerWriter(session),
                actor_id=ACTOR,
                actor_kind="user",
                project_id=project_id,
                name="primary",
                base_url=fake.base_url,
                api_key=fake.api_key,
                is_default=True,
            )
        await session.commit()


def _token(project_id: uuid.UUID) -> str:
    return mint_agent_token(
        secret=SECRET,
        user_id=ACTOR,
        project_id=project_id,
        agent_run_id=uuid.uuid4(),
        actor_kind="aleph_agent",
        correlation_id=f"wgw-{uuid.uuid4().hex}",
    )


def _ctx(maker: Callable[[], AsyncSession], resolver: WorkerGateways) -> dict[str, Any]:
    """The arq context, with exactly the keys `smoke_llm_job` reads."""
    return {"agent_token_secret": SECRET, "session_maker": maker, "gateways": resolver}


# ---------------------------------------------------------------------------
# c6 — a project's row decides where its BACKGROUND traffic goes
# ---------------------------------------------------------------------------


async def test_a_job_run_for_one_project_never_touches_the_other_gateway(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    second_project: uuid.UUID,
    fakes: tuple[FakeGateway, FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """The defect, stated as traffic. Both jobs used to reach the same server.

    B is run FIRST, so "A's gateway" cannot be whichever one happened to be
    called last, and A's counter is captured before B runs so its stillness is
    measured rather than assumed.
    """
    fake_a, fake_b, fallback, http = fakes
    await _bind(maker, committed_project, fake=fake_a, model_id="vllm-local-alpha")
    await _bind(maker, second_project, fake=fake_b, model_id="vllm-local-beta")
    resolver = _resolver(maker, fallback, http)

    a_before = fake_a.request_count
    b_result = await smoke_llm_job(
        _ctx(maker, resolver), str(second_project), _token(second_project), prompt="hello B"
    )
    assert b_result["ok"] is True
    assert b_result["model"] == "vllm-local-beta"
    assert fake_b.count("/v1/chat/completions") == 1
    assert fake_a.request_count == a_before, (
        "project B's job reached project A's gateway — the endpoint row is not "
        "deciding where worker traffic goes"
    )

    b_after_b = fake_b.request_count
    a_result = await smoke_llm_job(
        _ctx(maker, resolver), str(committed_project), _token(committed_project), prompt="hello A"
    )
    assert a_result["ok"] is True
    assert a_result["model"] == "vllm-local-alpha"
    assert fake_a.count("/v1/chat/completions") == 1
    assert fake_b.request_count == b_after_b, "project A's job reached project B's gateway"

    # And nothing fell through to the deployment default on either run: a
    # resolver that ignored the rows entirely would have sent both there, and
    # both counters above would still read zero.
    assert fallback.request_count == 0


async def test_the_answer_each_job_returns_came_from_its_own_gateway(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    second_project: uuid.UUID,
    fakes: tuple[FakeGateway, FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """The counters say who was ASKED; this says whose answer came back.

    Both halves are needed. A client that reached B and then rendered a cached
    response from A would satisfy the request-counter test on its own — the
    same pairing `test_two_projects_see_two_disjoint_model_lists` makes on the
    API side.
    """
    fake_a, fake_b, fallback, http = fakes
    await _bind(maker, committed_project, fake=fake_a, model_id="vllm-local-alpha")
    await _bind(maker, second_project, fake=fake_b, model_id="vllm-local-beta")
    resolver = _resolver(maker, fallback, http)
    ctx = _ctx(maker, resolver)

    a = await smoke_llm_job(ctx, str(committed_project), _token(committed_project))
    b = await smoke_llm_job(ctx, str(second_project), _token(second_project))

    assert a["content"] == "answered by gw-worker-a"
    assert b["content"] == "answered by gw-worker-b"


async def test_a_project_with_no_row_still_reaches_the_deployment_default(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    fakes: tuple[FakeGateway, FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """Adoption without a flag day. Nobody has to configure anything to keep working."""
    fake_a, fake_b, fallback, http = fakes
    await _bind(maker, committed_project, fake=None, model_id="vllm-local-default")
    resolver = _resolver(maker, fallback, http)

    resolved = await resolver.resolve(committed_project)
    assert resolved.source == SOURCE_SETTINGS
    assert resolved.endpoint_id is None

    result = await smoke_llm_job(
        _ctx(maker, resolver), str(committed_project), _token(committed_project)
    )
    assert result["ok"] is True
    assert fallback.count("/v1/chat/completions") == 1
    assert fake_a.request_count == 0
    assert fake_b.request_count == 0


async def test_repointing_a_project_takes_effect_on_the_next_job(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    fakes: tuple[FakeGateway, FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """The reason resolution is per job and not per boot.

    An operator repointing a project at a new gateway must not have to restart
    the worker fleet for it to be true — a `WorkerGateways` that resolved once
    and cached by project would pass every test above and fail this one.
    """
    fake_a, fake_b, fallback, http = fakes
    await _bind(maker, committed_project, fake=fake_a, model_id="vllm-local-alpha")
    resolver = _resolver(maker, fallback, http)
    ctx = _ctx(maker, resolver)

    first = await smoke_llm_job(ctx, str(committed_project), _token(committed_project))
    assert first["model"] == "vllm-local-alpha"

    # Repoint, in the same process, with nothing restarted. The profile moves
    # too, because B does not serve A's model.
    async with maker() as session:
        await GatewayEndpointService(
            session, cipher=credential_cipher(master_key=MASTER_KEY)
        ).upsert(
            ledger=LedgerWriter(session),
            actor_id=ACTOR,
            actor_kind="user",
            project_id=committed_project,
            name="primary",
            base_url=fake_b.base_url,
            api_key=fake_b.api_key,
            is_default=True,
        )
        profile = (
            await session.execute(
                select(ModelProfile).where(ModelProfile.project_id == committed_project)
            )
        ).scalar_one()
        profile.bindings_jsonb = {
            "classification": {"model": "vllm-local-beta", "provider": "litellm"}
        }
        await session.commit()

    b_before = fake_b.request_count
    second = await smoke_llm_job(ctx, str(committed_project), _token(committed_project))
    assert second["model"] == "vllm-local-beta"
    assert fake_b.request_count > b_before
    assert fallback.request_count == 0


async def test_the_call_is_still_recorded_against_the_project(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    fakes: tuple[FakeGateway, FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """Routing per project must not cost the ledger.

    The per-endpoint clients are built with the same `pricing` table and the
    same `session_maker` the boot client had; forgetting either would move a
    project's traffic and silently stop recording what it spent, which is a
    worse trade than the defect this replaces.
    """
    fake_a, _fake_b, fallback, http = fakes
    await _bind(maker, committed_project, fake=fake_a, model_id="vllm-local-alpha")
    resolver = _resolver(maker, fallback, http)

    result = await smoke_llm_job(
        _ctx(maker, resolver), str(committed_project), _token(committed_project)
    )
    assert result["model_call_id"] is not None

    async with maker() as session:
        calls = list(
            (
                await session.execute(
                    select(ModelCall).where(ModelCall.project_id == committed_project)
                )
            )
            .scalars()
            .all()
        )
    assert len(calls) == 1
    assert calls[0].model == "vllm-local-alpha"
    assert calls[0].purpose == "inc0.smoke.worker"


async def test_two_projects_on_one_gateway_share_a_client(
    maker: Callable[[], AsyncSession],
    committed_project: uuid.UUID,
    second_project: uuid.UUID,
    fakes: tuple[FakeGateway, FakeGateway, FakeGateway, httpx.AsyncClient],
) -> None:
    """Keyed on the endpoint, not the project — one pool and one ceiling.

    A registry keyed on `project_id` would look identical from the outside and
    would open a connection pool per project, which is the shape the limiter
    exists to prevent.
    """
    fake_a, _fake_b, fallback, http = fakes
    await _bind(maker, committed_project, fake=fake_a, model_id="vllm-local-alpha")
    await _bind(maker, second_project, fake=fake_a, model_id="vllm-local-alpha")
    resolver = _resolver(maker, fallback, http)

    one = await resolver.litellm(committed_project)
    two = await resolver.litellm(second_project)
    assert one is two

    resolved_a = await resolver.resolve(committed_project)
    assert resolved_a.source == SOURCE_ROW
    assert resolver.catalog_for(resolved_a) is resolver.catalog_for(resolved_a)
