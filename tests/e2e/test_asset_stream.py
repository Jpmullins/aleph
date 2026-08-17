"""WP-1 integration: upload a source on the fs backend and stream it back
through the one authenticated asset route — no MinIO anywhere.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-asset", "email": "asset@test.local", "name": "Asset"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _make_project(http_client) -> str:
    resp = await http_client.post(
        "/v1/projects",
        json={"title": "Asset Stream", "description": "wp1"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_upload_then_stream_roundtrip(http_client, auth_bypass, asgi_app, monkeypatch):
    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    pid = await _make_project(http_client)

    payload = b"<html><body>wp-1 asset stream</body></html>"
    up = await http_client.post(
        f"/v1/projects/{pid}/sources/upload",
        files={"file": ("wp1.html", payload, "text/html")},
    )
    assert up.status_code == 201, up.text
    sid = up.json()["id"]

    resp = await http_client.get(f"/v1/projects/{pid}/assets/source/{sid}")
    assert resp.status_code == 200, resp.text
    assert resp.content == payload
    assert resp.headers["content-type"].startswith("text/html")
    sha = hashlib.sha256(payload).hexdigest()
    assert resp.headers["etag"] == f'"{sha}"'
    # Non-PDF asset bytes are neutralized server-side: no script execution
    # on the API origin (local mode carries ambient auth).
    assert resp.headers["content-security-policy"] == "sandbox"
    assert resp.headers["x-content-type-options"] == "nosniff"

    # Conditional revalidation short-circuits.
    cached = await http_client.get(
        f"/v1/projects/{pid}/assets/source/{sid}",
        headers={"If-None-Match": f'"{sha}"'},
    )
    assert cached.status_code == 304


async def test_stream_404s_for_unknown_and_foreign_ids(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    pid = await _make_project(http_client)
    resp = await http_client.get(f"/v1/projects/{pid}/assets/source/{uuid4()}")
    assert resp.status_code == 404

    resp = await http_client.get(f"/v1/projects/{pid}/assets/rendered/{uuid4()}")
    assert resp.status_code == 404

    resp = await http_client.get(f"/v1/projects/{pid}/assets/artifact-version/{uuid4()}")
    assert resp.status_code == 404


async def test_cross_project_source_404s(http_client, auth_bypass, asgi_app, monkeypatch):
    """A real source in project A must 404 under project B's path — the
    row lookup is project-scoped, not just the membership check."""
    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    pid_a = await _make_project(http_client)
    pid_b = await _make_project(http_client)

    up = await http_client.post(
        f"/v1/projects/{pid_a}/sources/upload",
        files={"file": ("cross.txt", b"project-a bytes", "text/plain")},
    )
    assert up.status_code == 201, up.text
    sid = up.json()["id"]

    ok = await http_client.get(f"/v1/projects/{pid_a}/assets/source/{sid}")
    assert ok.status_code == 200

    crossed = await http_client.get(f"/v1/projects/{pid_b}/assets/source/{sid}")
    assert crossed.status_code == 404


async def _insert_rendered_asset(asgi_app, pid: str, storage_uri: str, data: bytes):
    from aleph_artifacts.models import RenderedAsset
    from aleph_core.ids import uuid7

    row_id = uuid7()
    async with asgi_app.state.session_maker() as session:
        session.add(
            RenderedAsset(
                id=row_id,
                project_id=UUID(pid),
                created_by=uuid7(),
                source_kind="dataset",
                source_id=uuid7(),
                output_format="png",
                storage_uri=storage_uri,
                bytes_size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                render_spec_jsonb={},
            )
        )
        await session.commit()
    return row_id


async def test_rendered_and_artifact_version_stream_positive_paths(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    """The two non-source kinds stream real bytes (WP-4's artifact consumers
    ride this route; keep the happy paths exercised, not vacuous)."""
    from aleph_artifacts.models import ArtifactVersion
    from aleph_core.ids import uuid7

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    pid = await _make_project(http_client)
    store = asgi_app.state.asset_store

    png = b"\x89PNG-fake-render-bytes"
    stored = store.put_bytes(
        key=f"projects/{pid}/renders/test/{uuid4().hex[:12]}.png",
        data=png,
        mime_type="image/png",
    )
    render_id = await _insert_rendered_asset(asgi_app, pid, stored.storage_uri, png)

    resp = await http_client.get(f"/v1/projects/{pid}/assets/rendered/{render_id}")
    assert resp.status_code == 200, resp.text
    assert resp.content == png
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-security-policy"] == "sandbox"

    pdf = b"%PDF-fake-artifact-bytes"
    stored_pdf = store.put_bytes(
        key=f"projects/{pid}/artifacts/{uuid4()}/1.pdf",
        data=pdf,
        mime_type="application/pdf",
    )
    av_id = uuid7()
    async with asgi_app.state.session_maker() as session:
        session.add(
            ArtifactVersion(
                id=av_id,
                project_id=UUID(pid),
                artifact_id=uuid7(),
                version_no=1,
                storage_uri=stored_pdf.storage_uri,
                bytes_size=len(pdf),
                sha256=hashlib.sha256(pdf).hexdigest(),
                lineage_jsonb={},
                template_name="default",
                csl_style="apa",
                builder_agent_run_id=uuid7(),
                author_kind="agent",
                author_id=uuid7(),
                ledger_event_id=uuid7(),
            )
        )
        await session.commit()

    resp = await http_client.get(f"/v1/projects/{pid}/assets/artifact-version/{av_id}")
    assert resp.status_code == 200, resp.text
    assert resp.content == pdf
    assert resp.headers["content-type"] == "application/pdf"
    # PDFs are the one CSP-sandbox exemption (the viewer refuses sandboxed
    # documents); everything else carries it.
    assert "content-security-policy" not in resp.headers


async def test_missing_bytes_404_not_truncated_200(http_client, auth_bypass, asgi_app, monkeypatch):
    """A DB row whose file is gone must 404 cleanly — stream() raises before
    the response commits a 200 to the wire."""
    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    pid = await _make_project(http_client)
    render_id = await _insert_rendered_asset(
        asgi_app, pid, f"fs://projects/{pid}/renders/test/gone.png", b"never-written"
    )
    resp = await http_client.get(f"/v1/projects/{pid}/assets/rendered/{render_id}")
    assert resp.status_code == 404
