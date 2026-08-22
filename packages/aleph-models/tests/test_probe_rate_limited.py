"""Being busy is not being broken: a 429 must not disqualify a model.

`probe_model` exists because a gateway's model list states configuration, not
reachability — `bedrock-claude-sonnet-4-6` advertises happily and 400s on
invocation. Its answer feeds `unreachable`, which `select_default_bindings`
filters out entirely.

Folding a 429 into that set had two consequences, and the second is worse than
the first:

1. A model was disqualified for the deployment being over its per-minute budget
   at the instant somebody pressed "Configure from gateway".
2. The load that produced the 429 was the probe sweep itself — every advertised
   model invoked in one `asyncio.gather`. The bigger the gateway, the more
   likely it was to delete its own best candidates, and nothing in the result
   said so: the capability simply came out bound to something else, or unbound.

A 400 must still disqualify, or this change would have replaced one silent
failure with another.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from aleph_models.autoconfigure import autoconfigure_bindings
from aleph_models.discovery import GatewayCatalog, discover_models, probe_model
from aleph_models.limiter import reset_limiters
from aleph_models.testing import FakeGateway, FakeModel, GatewayConfig, rate_limited

CHAT = "/v1/chat/completions"

#: One priced, tool-capable chat model with a large window — it qualifies for
#: every chat capability, so if it survives the probe it must appear in the
#: bindings, and if it does not the result is unmistakable.
SOLO = FakeModel(
    id="solo-chat-4",
    mode="chat",
    max_input_tokens=200_000,
    max_output_tokens=8_192,
    input_per_token="1e-6",
    output_per_token="2e-6",
    supports_function_calling=True,
    supports_reasoning=True,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    reset_limiters()
    yield
    reset_limiters()


async def _probe_once(config: GatewayConfig) -> tuple[FakeGateway, str | None]:
    fake = FakeGateway(config)
    async with fake.client() as http:
        (model,) = await discover_models(base_url=fake.base_url, api_key=fake.api_key, client=http)
        error = await probe_model(
            base_url=fake.base_url, api_key=fake.api_key, model=model, client=http
        )
    return fake, error


class TestARateLimitedProbe:
    async def test_reports_no_error(self) -> None:
        fake, error = await _probe_once(
            GatewayConfig.well_behaved(models=(SOLO,), invoke_script=(rate_limited(),))
        )
        assert error is None, f"a busy minute was reported as a broken model: {error}"
        assert fake.count(CHAT) == 1, "the probe did not actually reach the gateway"

    async def test_still_gets_bound(self) -> None:
        """The end of the chain the criterion is about: through `autoconfigure`."""
        fake = FakeGateway(
            GatewayConfig.well_behaved(models=(SOLO,), invoke_script=(rate_limited(),))
        )
        profile = SimpleNamespace(bindings_jsonb={})
        async with fake.client() as http:
            result = await autoconfigure_bindings(
                cast("Any", profile),
                catalog=GatewayCatalog(base_url=fake.base_url, api_key=fake.api_key, client=http),
                base_url=fake.base_url,
                api_key=fake.api_key,
                http_client=http,
                probe=True,
            )
        assert result.unreachable == {}, (
            "a 429 was folded into `unreachable`, which deletes the model from "
            "every capability it qualified for"
        )
        assert set(result.bound.values()) == {SOLO.id}
        assert "synthesis" in result.bound

    async def test_a_429_without_a_retry_after_is_treated_the_same(self) -> None:
        """The header is optional; the meaning of the status is not."""
        _fake, error = await _probe_once(
            GatewayConfig.well_behaved(
                models=(SOLO,), invoke_script=(rate_limited(retry_after=None),)
            )
        )
        assert error is None


class TestAFailedProbeStillFails:
    async def test_a_400_is_still_an_error(self) -> None:
        """Otherwise the fix replaced one silent wrong binding with another."""
        broken = FakeModel(
            id="bedrock-needs-an-inference-profile",
            mode="chat",
            input_per_token="3e-6",
            output_per_token="1.5e-5",
            supports_function_calling=True,
            invoke_error="Invocation of model ID ... requires an inference profile",
        )
        _fake, error = await _probe_once(GatewayConfig.well_behaved(models=(broken,)))
        assert error is not None
        assert "inference profile" in error, "the gateway's own words did not survive"

    async def test_a_500_is_still_an_error(self) -> None:
        from aleph_models.testing import server_error

        _fake, error = await _probe_once(
            GatewayConfig.well_behaved(models=(SOLO,), invoke_script=(server_error(),))
        )
        assert error is not None
        assert "503" in error
