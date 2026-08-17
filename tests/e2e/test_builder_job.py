"""builder_job + post_build lifecycle: the AgentRun the API creates is the one
the UI watches, so the worker must drive THAT run through
running -> succeeded/failed, and a failed dispatch must not leave it pending.

Integration: real DB; BuilderWorkflow is stubbed (no LLM / no render).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-build", "email": "build@test.local", "name": "Build"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _make_project(http_client) -> UUID:
    resp = await http_client.post(
        "/v1/projects",
        json={"title": "Builder lifecycle", "description": "t"},
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def _make_artifact_and_pending_run(asgi_app, project_id: UUID):
    """Mirror what post_build does: artifact + pending AgentRun + agent token."""
    from aleph_artifacts.artifact_service import create_artifact
    from aleph_core.ids import uuid7
    from aleph_db.models.agent import AgentRun
    from aleph_db.models.project import Project
    from aleph_security.agent_token import mint_agent_token
    from aleph_security.principal import Principal

    maker = asgi_app.state.session_maker
    settings = asgi_app.state.settings
    async with maker() as session:
        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one()
        owner_id = project.created_by
        principal = Principal(
            user_id=owner_id,
            subject="user-build",
            email="build@test.local",
            actor_kind="user",
        )
        artifact = await create_artifact(
            session,
            principal=principal,
            project_id=project_id,
            title="Report",
            artifact_kind="report_pdf",
        )
        run_id = uuid7()
        corr = f"builder-{run_id.hex}"
        session.add(
            AgentRun(
                id=run_id,
                project_id=project_id,
                agent_kind="builder",
                correlation_id=corr,
                status="pending",
                input_payload={"artifact_id": str(artifact.id)},
                created_by=owner_id,
                access_scope="project",
            )
        )
        await session.commit()
        artifact_id = artifact.id

    token = mint_agent_token(
        secret=settings.aleph_agent_token_secret,
        user_id=owner_id,
        project_id=project_id,
        agent_run_id=run_id,
        actor_kind="aleph_agent",
        correlation_id=corr,
        ttl_seconds=3600,
    )
    return artifact_id, run_id, token


async def _builder_runs(maker, project_id: UUID):
    from aleph_db.models.agent import AgentRun

    async with maker() as session:
        return list(
            (
                await session.execute(
                    select(AgentRun).where(
                        AgentRun.project_id == project_id,
                        AgentRun.agent_kind == "builder",
                    )
                )
            )
            .scalars()
            .all()
        )


def _ctx(asgi_app) -> dict:
    return {
        "session_maker": asgi_app.state.session_maker,
        "asset_store": SimpleNamespace(),
        "agent_token_secret": asgi_app.state.settings.aleph_agent_token_secret,
    }


async def test_builder_job_succeeds_on_the_dispatching_run(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    """builder_job must update the run the API created — not insert a twin."""
    import aleph_workers.jobs.builder as builder_mod
    from aleph_core.ids import uuid7

    project_id = await _make_project(http_client)
    artifact_id, run_id, token = await _make_artifact_and_pending_run(asgi_app, project_id)

    fake_version_id = uuid7()

    class StubWorkflow:
        def __init__(self, **kw):
            pass

        async def run(self, **kw):
            return SimpleNamespace(id=fake_version_id)

    monkeypatch.setattr(builder_mod, "BuilderWorkflow", StubWorkflow)

    out = await builder_mod.builder_job(
        _ctx(asgi_app),
        str(project_id),
        str(artifact_id),
        "report_pdf",
        "default",
        "apa",
        [],
        [],
        token,
    )

    assert out["ok"] is True
    runs = await _builder_runs(asgi_app.state.session_maker, project_id)
    assert len(runs) == 1, f"expected exactly the dispatching run, got {len(runs)}"
    (run,) = runs
    assert run.id == run_id
    assert run.status == "succeeded"
    assert run.started_at is not None
    assert run.completed_at is not None
    assert run.result_payload == {"version_id": str(fake_version_id)}


async def test_builder_job_marks_the_dispatching_run_failed(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    import aleph_workers.jobs.builder as builder_mod

    project_id = await _make_project(http_client)
    artifact_id, run_id, token = await _make_artifact_and_pending_run(asgi_app, project_id)

    class StubWorkflow:
        def __init__(self, **kw):
            pass

        async def run(self, **kw):
            raise RuntimeError("render exploded")

    monkeypatch.setattr(builder_mod, "BuilderWorkflow", StubWorkflow)

    out = await builder_mod.builder_job(
        _ctx(asgi_app),
        str(project_id),
        str(artifact_id),
        "report_pdf",
        "default",
        "apa",
        [],
        [],
        token,
    )

    assert out["ok"] is False
    runs = await _builder_runs(asgi_app.state.session_maker, project_id)
    assert len(runs) == 1
    (run,) = runs
    assert run.id == run_id
    assert run.status == "failed"
    assert "render exploded" in (run.error_text or "")
    assert run.completed_at is not None


async def test_post_build_marks_run_failed_when_enqueue_fails(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    """A swallowed enqueue failure must not leave a forever-pending run."""
    import arq

    project_id = await _make_project(http_client)

    async def boom(*a, **kw):
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(arq, "create_pool", boom)

    resp = await http_client.post(
        f"/v1/projects/{project_id}/artifacts/build",
        json={"title": "Doomed report", "artifact_kind": "report_pdf"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["dispatched"] is False

    runs = await _builder_runs(asgi_app.state.session_maker, project_id)
    assert len(runs) == 1
    (run,) = runs
    assert run.status == "failed", "enqueue failure must fail the run, not strand it pending"
    assert "redis unreachable" in (run.error_text or "")
