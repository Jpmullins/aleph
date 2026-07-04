"""Integration test for the agent-facing source ingestion route.

Covers `POST /v1/projects/{project_id}/sources/ingest-url` — the route the
`ingest_source` agent tool self-calls. Reuses the shared `http_client` +
`auth_bypass` fixtures from conftest and creates a project inline (there is
no standalone project_id fixture in this suite).

This hits a real outbound URL (https://example.com/) so it requires network
egress in addition to the compose stack.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _create_project(http_client) -> str:
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Agent-tool ledger test", "description": "", "budget_usd": "5.00"},
    )
    assert proj.status_code == 201, proj.text
    return proj.json()["id"]


async def _ledger_kinds(asgi_app, project_id: str) -> list[str]:
    """Return the action_kind of every ActionLedgerEvent for a project.

    Mirrors the DB-access pattern in test_project_lifecycle.py: query the
    real rows through the app's shared session_maker — no mocks.
    """
    from aleph_db.models.ledger import ActionLedgerEvent

    maker = asgi_app.state.session_maker
    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(ActionLedgerEvent)
                    .where(ActionLedgerEvent.project_id == project_id)
                    .order_by(ActionLedgerEvent.timestamp)
                )
            )
            .scalars()
            .all()
        )
    return [r.action_kind for r in rows]


@pytest.fixture
async def auth_bypass(monkeypatch):
    """Replace JWT verification with a fixed claim set so we don't need an IdP."""
    from aleph_security import jwt as jwt_module

    fixed_subject = "test-user-agent-tools"
    fixed_email = "agent-tools@test.local"

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": fixed_subject, "email": fixed_email, "name": "Agent Tools Tester"}

    monkeypatch.setattr(jwt_module, "verify_user_jwt", fake_verify)
    from aleph_api.middleware import auth as auth_mod

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)
    yield {"subject": fixed_subject, "email": fixed_email}


async def test_ingest_url_creates_source(http_client, auth_bypass):
    """Ingesting a URL fetches it server-side and registers a Source."""
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Ingest URL test", "description": "", "budget_usd": "5.00"},
    )
    assert proj.status_code == 201, proj.text
    project_id = proj.json()["id"]

    resp = await http_client.post(
        f"/v1/projects/{project_id}/sources/ingest-url",
        json={"url": "https://example.com/", "title": "Example"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] in ("normalizing", "pending")
    assert body["source_id"]


async def test_ingest_url_writes_source_ledger(http_client, auth_bypass, asgi_app):
    """The ingest-url route (mirrored by the `ingest_source` agent tool) writes
    the source-creation ledger events (rule #4). `register_uploaded_source`
    emits both `source.create` and `source_version.create`."""
    project_id = await _create_project(http_client)

    resp = await http_client.post(
        f"/v1/projects/{project_id}/sources/ingest-url",
        json={"url": "https://example.com/", "title": "Example"},
    )
    assert resp.status_code == 201, resp.text

    kinds = await _ledger_kinds(asgi_app, project_id)
    assert "source.create" in kinds, kinds
    assert "source_version.create" in kinds, kinds


async def test_create_hypothesis_writes_ledger(http_client, auth_bypass, asgi_app):
    """Creating a hypothesis via the route the `create_hypothesis_tool` mirrors
    writes a `hypothesis.create` ledger event (rule #4)."""
    project_id = await _create_project(http_client)

    resp = await http_client.post(
        f"/v1/projects/{project_id}/hypotheses",
        json={
            "title": "Test hypothesis",
            "statement": "The ledger records hypothesis creation.",
        },
    )
    assert resp.status_code == 201, resp.text

    kinds = await _ledger_kinds(asgi_app, project_id)
    assert "hypothesis.create" in kinds, kinds


# ---------------------------------------------------------------------------
# Wave 6 B — approval-gated consequential agent actions
# ---------------------------------------------------------------------------


async def _request_agent_action(http_client, project_id: str, *, tool: str, args: dict) -> str:
    """Self-call the agent-actions/request route (what the gated tools do)."""
    resp = await http_client.post(
        f"/v1/projects/{project_id}/agent-actions/request",
        json={
            "tool": tool,
            "args": args,
            "title": f"Gated: {tool}",
            "summary": f"approval test for {tool}",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["request_id"]


async def test_agent_action_request_creates_pending_approval(http_client, auth_bypass, asgi_app):
    """`build_artifact`/`set_connector_enabled` route to a *pending* ApprovalRequest
    (target_kind=agent_action, target_id == its own id) + an
    `approval_request.create` ledger event — they do NOT execute on request."""
    from aleph_reviewer.models import ApprovalRequest

    project_id = await _create_project(http_client)
    request_id = await _request_agent_action(
        http_client,
        project_id,
        tool="build_artifact",
        args={
            "title": "Gated Report",
            "artifact_kind": "report_markdown_bundle",
            "template_name": "report_markdown_bundle",
            "csl_style": "apa-7",
            "wiki_page_ids": [],
            "dataset_version_ids": [],
        },
    )

    maker = asgi_app.state.session_maker
    async with maker() as session:
        req = (
            await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id))
        ).scalar_one()
        assert req.status == "pending"
        assert req.target_kind == "agent_action"
        assert str(req.target_id) == request_id  # target_id == request's own id
        assert req.proposed_patch_jsonb is not None
        assert req.proposed_patch_jsonb["tool"] == "build_artifact"

    kinds = await _ledger_kinds(asgi_app, project_id)
    assert "approval_request.create" in kinds, kinds
    # No build dispatched on request.
    arts = await http_client.get(f"/v1/projects/{project_id}/artifacts")
    assert arts.json() == []


async def test_agent_action_approve_executes_effect(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    """Approving an agent_action flips the request to approved, writes a decision +
    ledger, AND executes the real effect (an Artifact is created).

    The approve handler normally self-calls the underlying route over httpx at
    `aleph_self_url`; in-process tests have no real socket, so we redirect that
    one indirection through the in-process test client. The dispatch still goes
    through the handler's fixed allowlist (rule #8 preserved)."""
    from aleph_api import a2ui_handlers
    from aleph_reviewer.models import ApprovalRequest

    project_id = await _create_project(http_client)

    async def _exec_in_process(*, project_id, tool, args, **_):
        route = a2ui_handlers._AGENT_ACTION_ROUTES[tool].format(project_id=project_id)
        resp = await http_client.post(route, json=args)
        assert resp.status_code < 400, resp.text
        return resp.json()

    monkeypatch.setattr(a2ui_handlers, "_execute_agent_action", _exec_in_process)

    request_id = await _request_agent_action(
        http_client,
        project_id,
        tool="build_artifact",
        args={
            "title": "Approved Report",
            "artifact_kind": "report_markdown_bundle",
            "template_name": "report_markdown_bundle",
            "csl_style": "apa-7",
            "wiki_page_ids": [],
            "dataset_version_ids": [],
        },
    )

    approve = await http_client.post(
        f"/v1/projects/{project_id}/cards/actions",
        json={
            "surface_kind": "chat",
            "action_kind": "approve",
            "params": {"target_id": request_id, "target_kind": "agent_action"},
        },
    )
    assert approve.status_code == 200, approve.text
    result = approve.json()["result"]
    assert result["new_status"] == "approved"
    assert result["executed"]["artifact_id"]

    maker = asgi_app.state.session_maker
    async with maker() as session:
        req = (
            await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id))
        ).scalar_one()
        assert req.status == "approved"
        assert req.decision_id is not None

    # The real effect happened: an Artifact row now exists.
    arts = await http_client.get(f"/v1/projects/{project_id}/artifacts")
    assert len(arts.json()) == 1, arts.text

    kinds = await _ledger_kinds(asgi_app, project_id)
    assert "approval_request.create" in kinds, kinds
    assert "a2ui.action.approve" in kinds, kinds
    # The specific decision event (rule #4), mirroring approval_service.decide().
    assert "approval_request.approved" in kinds, kinds
    assert "artifact.create" in kinds, kinds  # the executed effect ledgered too


async def test_agent_action_reject_does_not_execute(http_client, auth_bypass, asgi_app):
    """Rejecting an agent_action flips it to rejected + ledgers the decision, and
    does NOT execute the effect (no Artifact created)."""
    from aleph_reviewer.models import ApprovalRequest

    project_id = await _create_project(http_client)
    request_id = await _request_agent_action(
        http_client,
        project_id,
        tool="build_artifact",
        args={
            "title": "Rejected Report",
            "artifact_kind": "report_markdown_bundle",
            "template_name": "report_markdown_bundle",
            "csl_style": "apa-7",
            "wiki_page_ids": [],
            "dataset_version_ids": [],
        },
    )

    reject = await http_client.post(
        f"/v1/projects/{project_id}/cards/actions",
        json={
            "surface_kind": "chat",
            "action_kind": "reject",
            "params": {
                "target_id": request_id,
                "target_kind": "agent_action",
                "reason": "not now",
            },
        },
    )
    assert reject.status_code == 200, reject.text
    assert reject.json()["result"]["new_status"] == "rejected"

    maker = asgi_app.state.session_maker
    async with maker() as session:
        req = (
            await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == request_id))
        ).scalar_one()
        assert req.status == "rejected"
        assert req.decision_id is not None

    # No effect executed on reject.
    arts = await http_client.get(f"/v1/projects/{project_id}/artifacts")
    assert arts.json() == []

    kinds = await _ledger_kinds(asgi_app, project_id)
    assert "a2ui.action.reject" in kinds, kinds
    assert "artifact.create" not in kinds, kinds


async def test_agent_action_request_rejects_unknown_tool(http_client, auth_bypass):
    """The request route refuses to gate a tool outside the allowlist (422)."""
    project_id = await _create_project(http_client)
    resp = await http_client.post(
        f"/v1/projects/{project_id}/agent-actions/request",
        json={"tool": "rm_rf", "args": {}, "title": "evil", "summary": "evil"},
    )
    assert resp.status_code == 422, resp.text
