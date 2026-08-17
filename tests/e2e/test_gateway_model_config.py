"""Model configuration must come from the gateway, and say what it refused.

Aleph ships no model list. The Settings picker and the default capability
bindings are both derived from the gateway's `/model/info`, so these routes are
the seam where "what the deployment can actually do" enters the system.

Two failures matter here:

* **Offering a model that cannot do the job.** The cheapest chat model on the
  real gateway has an 8k context; bound to `page_selection` it fails only once
  the wiki index outgrows the window — long after configuration, on real
  content.
* **Binding a model that does not answer.** The real gateway advertises two
  Sonnets that 4xx on invocation. Advertised is not reachable, and the only way
  to know the difference is to call them.

The HTTP boundary to the gateway is faked (a captured `/model/info` payload and
canned probe responses); everything downstream — capability policy, selection,
the profile write, the ledger append — is the production path.
"""

from __future__ import annotations

import json
import pathlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "packages/aleph-models/tests/fixtures/bedrock_gateway_model_info.json"
)

#: Verified against the live gateway: advertised, priced, and both fail on call.
UNREACHABLE = {
    "bedrock-claude-sonnet-4": "HTTP 404: Access denied",
    "bedrock-claude-sonnet-4-6": "HTTP 400: on-demand throughput isn't supported",
}


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "gw-config", "email": "gw@test.local", "name": "GW"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


def _models():
    from aleph_models.discovery import parse_model_info

    return parse_model_info(json.loads(FIXTURE.read_text()))


@pytest.fixture
def gateway(asgi_app, monkeypatch):
    """Serve the captured payload, and fail the two models that really fail."""

    class _Catalog:
        async def models(self, *, force: bool = False):
            return _models()

    monkeypatch.setattr(asgi_app.state, "gateway_catalog", _Catalog(), raising=False)

    from aleph_api.routes import model_profile as routes

    async def fake_probe(*, base_url, api_key, model, client=None, timeout_s=45.0):
        return UNREACHABLE.get(model.id)

    monkeypatch.setattr(routes, "probe_model", fake_probe)


async def _project(http_client) -> str:
    resp = await http_client.post(
        "/v1/projects", json={"title": f"gw-config {uuid4().hex[:6]}", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestGatewayModelListing:
    async def test_lists_what_the_gateway_serves(self, http_client, auth_bypass, gateway):
        resp = await http_client.get("/v1/gateway/models")
        assert resp.status_code == 200, resp.text
        ids = [m["id"] for m in resp.json()]
        assert ids == sorted(ids), "listing order is unstable; the picker would jump around"
        assert "bedrock-claude-opus-4.7" in ids
        assert "bedrock-titan-embed-text" in ids

    async def test_each_model_advertises_only_capabilities_it_can_serve(
        self, http_client, auth_bypass, gateway
    ):
        """The picker filters on this; a wrong answer is a runtime failure."""
        by_id = {m["id"]: m for m in (await http_client.get("/v1/gateway/models")).json()}

        small = by_id["bedrock-llama3-70b"]
        assert "page_selection" not in small["capabilities"], (
            "an 8k-context model was offered for page selection, which loads the "
            "whole wiki index into one prompt"
        )
        assert "extraction" not in small["capabilities"], (
            "a model without function calling was offered for extraction"
        )

        embed = by_id["bedrock-titan-embed-text"]
        assert embed["capabilities"] == ["embedding"]

        opus = by_id["bedrock-claude-opus-4.7"]
        assert {"synthesis", "judge", "vision", "code"} <= set(opus["capabilities"])

    async def test_prices_are_carried_through_for_display(self, http_client, auth_bypass, gateway):
        by_id = {m["id"]: m for m in (await http_client.get("/v1/gateway/models")).json()}
        opus = by_id["bedrock-claude-opus-4.7"]
        assert opus["is_priced"] is True
        assert float(opus["input_per_token"]) == pytest.approx(5.5e-06)
        assert opus["max_input_tokens"] == 1_000_000


class TestAutoconfigure:
    async def test_binds_every_capability_the_gateway_can_serve(
        self, http_client, auth_bypass, gateway
    ):
        project_id = await _project(http_client)
        resp = await http_client.post(f"/v1/projects/{project_id}/model-profile/autoconfigure")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert set(body["bound"]) == {
            "synthesis",
            "judge",
            "page_selection",
            "extraction",
            "classification",
            "vision",
            "code",
            "embedding",
        }
        assert body["unbound"] == ["rerank"], (
            "a capability with no qualifying model must be reported, not filled "
            f"in with a guess; got {body['unbound']}"
        )

    async def test_never_binds_a_model_that_fails_on_invocation(
        self, http_client, auth_bypass, gateway
    ):
        """The reason autoconfigure probes instead of trusting the list."""
        project_id = await _project(http_client)
        body = (
            await http_client.post(f"/v1/projects/{project_id}/model-profile/autoconfigure")
        ).json()

        assert set(body["unreachable"]) == set(UNREACHABLE)
        assert not set(body["bound"].values()) & set(UNREACHABLE), (
            f"bound a model that does not answer: {set(body['bound'].values()) & set(UNREACHABLE)}"
        )

    async def test_persists_the_bindings_with_gateway_rates(
        self, asgi_app, http_client, auth_bypass, gateway
    ):
        """A binding without rates falls back to schema defaults of zero cost."""
        from aleph_db.repos import model_profile as profile_repo

        project_id = await _project(http_client)
        await http_client.post(f"/v1/projects/{project_id}/model-profile/autoconfigure")

        async with asgi_app.state.session_maker() as session:
            profile = await profile_repo.get_project_profile(session, UUID(project_id))
        assert profile is not None
        synthesis = profile.bindings_jsonb["synthesis"]
        assert synthesis["model"] == "bedrock-claude-opus-4.7"
        assert synthesis["max_input_tokens"] == 1_000_000
        assert float(synthesis["cost_per_input_token_usd"]) == pytest.approx(5.5e-06)

    async def test_reads_back_over_the_api(self, http_client, auth_bypass, gateway):
        project_id = await _project(http_client)
        await http_client.post(f"/v1/projects/{project_id}/model-profile/autoconfigure")
        resp = await http_client.get(f"/v1/projects/{project_id}/model-profile")
        assert resp.status_code == 200, resp.text
        assert resp.json()["bindings"]["embedding"]["model"] == "bedrock-titan-embed-text"

    async def test_writes_exactly_one_ledger_event(
        self, asgi_app, http_client, auth_bypass, gateway
    ):
        """Rule #4 — configuration is a mutation and must be auditable."""
        from aleph_db.models.ledger import ActionLedgerEvent

        project_id = await _project(http_client)

        async with asgi_app.state.session_maker() as session:
            before = (
                await session.execute(
                    select(func.count())
                    .select_from(ActionLedgerEvent)
                    .where(ActionLedgerEvent.project_id == UUID(project_id))
                    .where(ActionLedgerEvent.action_kind == "model_profile.autoconfigure")
                )
            ).scalar_one()

        await http_client.post(f"/v1/projects/{project_id}/model-profile/autoconfigure")

        async with asgi_app.state.session_maker() as session:
            rows = list(
                (
                    await session.execute(
                        select(ActionLedgerEvent)
                        .where(ActionLedgerEvent.project_id == UUID(project_id))
                        .where(ActionLedgerEvent.action_kind == "model_profile.autoconfigure")
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == before + 1, f"expected one ledger event, found {len(rows)}"
        payload = rows[-1].payload_jsonb
        assert payload["unbound"] == ["rerank"]
        assert sorted(payload["unreachable"]) == sorted(UNREACHABLE)
        assert payload["bound"]["synthesis"] == "bedrock-claude-opus-4.7"

    async def test_skipping_the_probe_is_possible_but_then_trusts_the_list(
        self, http_client, auth_bypass, gateway
    ):
        """`probe=false` is faster and less safe; the difference must be visible."""
        project_id = await _project(http_client)
        body = (
            await http_client.post(
                f"/v1/projects/{project_id}/model-profile/autoconfigure?probe=false"
            )
        ).json()
        assert body["unreachable"] == {}
        # Without probing, an advertised-but-broken model is reachable as far as
        # we know — which is exactly why the default is to probe.
        assert body["bound"]["page_selection"] in {
            "bedrock-claude-opus-4.7",
            "bedrock-claude-sonnet-4",
            "bedrock-claude-sonnet-4-6",
        }
