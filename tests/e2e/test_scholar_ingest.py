"""Integration tests for the WP-2 scholarly ingest passthrough.

Covers `POST /v1/projects/{project_id}/sources/ingest-url` with the new
optional `connector_kind` + `source_metadata` fields: a real origin lands
on the Source row's `connector_kind` and its `source_metadata_jsonb`
(spec WP-2 §5), while unknown kinds are rejected (422). Requires the
compose stack + migrations (the `openalex` connector row is seeded by the
Inc 3 migration) and network egress to https://example.com/, mirroring
`test_agent_tools.py`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    """Replace JWT verification with a fixed claim set so we don't need an IdP."""
    from aleph_security import jwt as jwt_module

    fixed_subject = "test-user-scholar-ingest"
    fixed_email = "scholar-ingest@test.local"

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": fixed_subject, "email": fixed_email, "name": "Scholar Ingest Tester"}

    monkeypatch.setattr(jwt_module, "verify_user_jwt", fake_verify)
    from aleph_api.middleware import auth as auth_mod

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)
    yield {"subject": fixed_subject, "email": fixed_email}


async def _create_project(http_client) -> str:
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Scholar ingest test", "description": "", "budget_usd": "5.00"},
    )
    assert proj.status_code == 201, proj.text
    return proj.json()["id"]


async def test_ingest_url_with_scholar_provenance_lands_on_source_row(
    http_client, auth_bypass, asgi_app
):
    """connector_kind=openalex + source_metadata (doi / openalex_id /
    doi_verdict) must land on the Source row — connector_kind on the column,
    metadata merged into source_metadata_jsonb."""
    from aleph_rks.models import Source

    project_id = await _create_project(http_client)

    doi_verdict = {
        "ok": True,
        "retracted": False,
        "checked_via": "crossref+openalex",
        "checked_at": "2026-07-03T00:00:00+00:00",
    }
    resp = await http_client.post(
        f"/v1/projects/{project_id}/sources/ingest-url",
        json={
            "url": "https://example.com/",
            "title": "Scholarly example",
            "connector_kind": "openalex",
            "source_metadata": {
                "doi": "10.5555/example.doi",
                "openalex_id": "W2741809807",
                "doi_verdict": doi_verdict,
            },
        },
    )
    assert resp.status_code == 201, resp.text
    source_id = resp.json()["source_id"]

    maker = asgi_app.state.session_maker
    async with maker() as session:
        src = (await session.execute(select(Source).where(Source.id == source_id))).scalar_one()
        assert src.connector_kind == "openalex"
        meta = src.source_metadata_jsonb
        assert meta["doi"] == "10.5555/example.doi"
        assert meta["openalex_id"] == "W2741809807"
        assert meta["doi_verdict"] == doi_verdict
        # The upload bookkeeping keys survive the merge.
        assert "storage_uri" in meta


async def test_ingest_url_unknown_connector_kind_422(http_client, auth_bypass):
    """Unknown connector_kind → 422 ValidationFailed (provenance names must
    exist in the seeded connectors table)."""
    project_id = await _create_project(http_client)

    resp = await http_client.post(
        f"/v1/projects/{project_id}/sources/ingest-url",
        json={
            "url": "https://example.com/",
            "title": "Bad provenance",
            "connector_kind": "not_a_connector",
        },
    )
    assert resp.status_code == 422, resp.text
    assert "unknown connector_kind" in resp.json()["detail"]


async def test_ingest_url_without_provenance_keeps_upload_default(
    http_client, auth_bypass, asgi_app
):
    """Omitting the new fields preserves the historical behavior:
    connector_kind stays 'upload'."""
    from aleph_rks.models import Source

    project_id = await _create_project(http_client)
    resp = await http_client.post(
        f"/v1/projects/{project_id}/sources/ingest-url",
        json={"url": "https://example.com/", "title": "Plain ingest"},
    )
    assert resp.status_code == 201, resp.text
    source_id = resp.json()["source_id"]

    maker = asgi_app.state.session_maker
    async with maker() as session:
        src = (await session.execute(select(Source).where(Source.id == source_id))).scalar_one()
        assert src.connector_kind == "upload"
