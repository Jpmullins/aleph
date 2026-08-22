"""One catalog per endpoint, bounded, and no secret in the cache key.

`ProjectGatewayCatalogs` replaces a single `GatewayCatalog` built at boot from
`LITELLM_BASE_URL` and read by every project. That singleton is why
`GET /v1/gateway/models` answered with the same list no matter whose project
asked, and swapping it for a per-project dict would have traded one defect for
three: a cache that never invalidates on a rotated key, a cache keyed on
operator-supplied strings with no bound, and a cache holding secrets.

No mock catalogs here — the fake gateway is the HTTP boundary and everything
above it is the shipped code, so "the second lookup was a cache hit" is
measured as *the gateway was not asked twice* rather than as an attribute on a
stub.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from aleph_models.endpoints import ProjectGatewayCatalogs, ResolvedEndpoint, settings_endpoint
from aleph_models.limiter import reset_limiters
from aleph_models.testing import FakeGateway, FakeModel, GatewayConfig


@pytest.fixture(autouse=True)
def _clean_limiters() -> object:
    reset_limiters()
    yield
    reset_limiters()


def _row(base_url: str, api_key: str, name: str = "primary") -> ResolvedEndpoint:
    return ResolvedEndpoint(
        base_url=base_url,
        api_key=api_key,
        name=name,
        endpoint_id=uuid.uuid4(),
        source="row",
    )


def test_two_endpoints_get_two_catalogs() -> None:
    """The whole point. One catalog for every project is the defect."""
    registry = ProjectGatewayCatalogs()
    a = registry.for_endpoint(_row("http://a.invalid", "sk-a-0123456789"))
    b = registry.for_endpoint(_row("http://b.invalid", "sk-b-0123456789"))
    assert a is not b
    assert len(registry) == 2


def test_two_projects_on_the_same_gateway_share_one_catalog() -> None:
    """Different rows, different ids, same gateway — one cache, one TTL.

    Keying on the endpoint id instead would double every project's discovery
    traffic to prove that two rows naming the same server with the same key
    serve the same models.
    """
    registry = ProjectGatewayCatalogs()
    mine = registry.for_endpoint(_row("http://shared.invalid", "sk-shared-0123456789"))
    theirs = registry.for_endpoint(_row("http://shared.invalid", "sk-shared-0123456789", "other"))
    assert mine is theirs
    assert len(registry) == 1


def test_a_rotated_key_at_the_same_url_is_a_different_catalog() -> None:
    """Otherwise a rotated key does nothing until the TTL expires.

    Which presents to an operator as "I changed the key and it still works with
    the old one" — the worst possible shape for a credential change, because it
    is indistinguishable from the rotation having silently failed.
    """
    registry = ProjectGatewayCatalogs()
    before = registry.for_endpoint(_row("http://gw.invalid", "sk-old-0123456789"))
    after = registry.for_endpoint(_row("http://gw.invalid", "sk-new-0123456789"))
    assert before is not after


def test_the_api_key_is_not_in_the_cache_key() -> None:
    """A dict key is in every heap dump, every `repr`, and every debugger.

    The digest answers the only question the cache has — "is this the same
    credential as last time" — and answers nothing else.
    """
    secret = "sk-super-secret-gateway-key-0123456789"
    key = ProjectGatewayCatalogs.key_for(_row("http://gw.invalid", secret))
    assert secret not in key
    assert "http://gw.invalid" in key, "the url is not a secret and identifies the entry"
    # Same input, same key — a digest that moved between calls would make every
    # lookup a miss and every request a fresh discovery.
    assert key == ProjectGatewayCatalogs.key_for(_row("http://gw.invalid", secret, "other"))


def test_the_registry_is_bounded_and_drops_the_least_recently_used() -> None:
    """It is process-wide and keyed on operator-supplied strings.

    Unbounded, that is a memory leak with an HTTP route in front of it: an
    endpoint edited a thousand times leaves a thousand cached model lists. LRU
    rather than insert-order so the endpoint actually in use is not the one
    evicted.
    """
    registry = ProjectGatewayCatalogs(max_entries=3)
    kept = registry.for_endpoint(_row("http://keep.invalid", "sk-keep-0123456789"))
    for n in range(2):
        registry.for_endpoint(_row(f"http://filler-{n}.invalid", "sk-filler-0123456789"))
    assert len(registry) == 3

    # Touch the first one so it is the most recent, then overflow by one.
    assert registry.for_endpoint(_row("http://keep.invalid", "sk-keep-0123456789")) is kept
    registry.for_endpoint(_row("http://overflow.invalid", "sk-overflow-0123456789"))

    assert len(registry) == 3, "the bound was not enforced"
    assert registry.for_endpoint(_row("http://keep.invalid", "sk-keep-0123456789")) is kept, (
        "the entry that was just used is the one that got evicted"
    )


def test_a_cache_hit_does_not_ask_the_gateway_again() -> None:
    """Measured at the HTTP boundary, not asserted on a stub.

    A registry that returned a *new* `GatewayCatalog` each time would pass every
    identity check above if they were written with `==`, and would still
    rediscover on every keystroke in the Settings picker. The gateway's own
    request counter is the only thing that cannot be satisfied by a
    convincing-looking object.
    """
    fake = FakeGateway(GatewayConfig(models=(FakeModel(id="vllm-local-only"),)))
    endpoint = _row(fake.base_url, fake.api_key)

    async def _run() -> tuple[list[str], list[str]]:
        async with fake.client() as http:
            registry = ProjectGatewayCatalogs(client=http)
            first = await registry.for_endpoint(endpoint).models()
            second = await registry.for_endpoint(endpoint).models()
        return [m.id for m in first], [m.id for m in second]

    first, second = asyncio.run(_run())
    assert first == second == ["vllm-local-only"]
    assert fake.count("/v1/models") == 1, (
        f"the gateway was asked {fake.count('/v1/models')} times for a list it had already given"
    )


def test_the_deployment_default_is_one_entry_shared_by_everyone_without_a_row() -> None:
    """`settings_endpoint` exists so the un-scoped route is not a second door.

    `GET /v1/gateway/models` and every project that has no endpoint of its own
    describe the same gateway. They have to come out of the same cache entry,
    or the boot default is quietly a separate code path again.
    """
    registry = ProjectGatewayCatalogs()
    unscoped = registry.for_endpoint(
        settings_endpoint(base_url="http://default.invalid/", api_key="sk-default-0123456789")
    )
    fell_back = registry.for_endpoint(
        ResolvedEndpoint(
            base_url="http://default.invalid",
            api_key="sk-default-0123456789",
            name="deployment default",
            endpoint_id=None,
            source="settings",
        )
    )
    assert unscoped is fell_back
    assert len(registry) == 1
