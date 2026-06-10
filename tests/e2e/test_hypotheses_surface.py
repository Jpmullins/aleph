"""The Hypotheses tab mounts the rich HypothesesSurface view.

Regression test for the blank/do-nothing Hypotheses tab: the surface builder
emitted a bare Column of HypothesisCards (empty Column for new projects — a
literally blank tab with no header, no + New, no empty state) instead of the
interactive HypothesesSurface component the other four tabs' pattern uses.
"""

from __future__ import annotations

from uuid import UUID

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-hyp", "email": "hyp@test.local", "name": "Hyp"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


def _components(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        body = m.get("updateComponents")
        if body:
            out.extend(body.get("components", []))
    return out


async def test_hypotheses_tab_mounts_the_rich_surface(http_client, auth_bypass):
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Hyp surface", "description": "t", "budget_usd": "5.00"},
    )
    assert proj.status_code == 201, proj.text
    project_id = UUID(proj.json()["id"])

    resp = await http_client.get(f"/v1/projects/{project_id}/surfaces/hypotheses")
    assert resp.status_code == 200, resp.text
    comps = _components(resp.json()["messages"])
    roots = [c for c in comps if c.get("id") == "root"]
    assert len(roots) == 1
    assert roots[0]["component"] == "HypothesesSurface", (
        "the tab must mount the interactive HypothesesSurface "
        f"(got {roots[0]['component']!r})"
    )
