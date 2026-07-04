"""render_code_artifact_job — the privileged persistence side of WP-4c.

Integration (real DB + asset store). The isolated code_runner is stubbed via a
fake ArqRedis pool so we exercise persistence, not execution: the returned bytes
must become a versioned ArtifactVersion carrying producing_code + its sha + the
output sha + builder_agent_run_id, ledgered, with a card pinned to Briefs.
"""

from __future__ import annotations

import base64
import hashlib
from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_PNG = b"\x89PNG\r\n\x1a\n-fake-but-decodable-bytes"


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-viz", "email": "viz@test.local", "name": "Viz"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _make_project(http_client) -> UUID:
    resp = await http_client.post(
        "/v1/projects",
        json={"title": "Viz code", "description": "t", "budget_usd": "5.00"},
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def _make_pending_run(asgi_app, project_id: UUID):
    from aleph_core.ids import uuid7
    from aleph_db.models.agent import AgentRun
    from aleph_db.models.project import Project
    from aleph_security.agent_token import mint_agent_token

    maker = asgi_app.state.session_maker
    settings = asgi_app.state.settings
    async with maker() as session:
        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one()
        owner_id = project.created_by
        run_id = uuid7()
        corr = f"vizcode-{run_id.hex}"
        session.add(
            AgentRun(
                id=run_id,
                project_id=project_id,
                agent_kind="viz_code",
                correlation_id=corr,
                status="pending",
                input_payload={"output_kind": "png"},
                created_by=owner_id,
                access_scope="project",
            )
        )
        await session.commit()
    token = mint_agent_token(
        secret=settings.aleph_agent_token_secret,
        user_id=owner_id,
        project_id=project_id,
        agent_run_id=run_id,
        actor_kind="aleph_agent",
        correlation_id=corr,
        ttl_seconds=3600,
    )
    return run_id, token


class _FakeJob:
    def __init__(self, result):
        self._result = result

    async def result(self, timeout=None):  # noqa: ASYNC109 — mirrors arq.Job.result signature
        return self._result


class _FakePool:
    """Stands in for the ArqRedis pool: records the enqueue + returns a canned
    code_runner result (the runner itself is never invoked here)."""

    def __init__(self, result):
        self._result = result
        self.calls: list[tuple] = []

    async def enqueue_job(self, fn, *args, _queue_name=None, **kwargs):
        self.calls.append((fn, args, _queue_name))
        return _FakeJob(self._result)


def _ctx(asgi_app, pool) -> dict:
    return {
        "session_maker": asgi_app.state.session_maker,
        "asset_store": asgi_app.state.asset_store,
        "agent_token_secret": asgi_app.state.settings.aleph_agent_token_secret,
        # render_code_job dispatches on the dedicated code-runner pool (the
        # sandbox's isolated bus); the fake pool stands in for it.
        "code_runner_pool": pool,
    }


async def test_render_code_job_persists_versioned_artifact(http_client, auth_bypass, asgi_app):
    from aleph_a2ui.models import InteractiveCard
    from aleph_artifacts.models import ArtifactVersion
    from aleph_db.models.agent import AgentRun
    from aleph_db.models.ledger import ActionLedgerEvent
    from aleph_workers.jobs.render_code import render_code_artifact_job

    project_id = await _make_project(http_client)
    run_id, token = await _make_pending_run(asgi_app, project_id)

    code = "import matplotlib\nplt=None  # producing code marker\n"
    runner_result = {
        "ok": True,
        "output_kind": "png",
        "mime": "image/png",
        "bytes_b64": base64.b64encode(_PNG).decode("ascii"),
    }
    pool = _FakePool(runner_result)

    out = await render_code_artifact_job(
        _ctx(asgi_app, pool),
        str(project_id),
        code,
        "png",
        "My sandbox chart",
        True,  # pin
        token,
    )

    assert out["ok"] is True, out
    # dispatched to the dedicated code_runner queue
    assert pool.calls and pool.calls[0][0] == "run_code_job"
    assert pool.calls[0][2] == "arq:queue:code_runner"

    maker = asgi_app.state.session_maker
    async with maker() as session:
        version = await session.get(ArtifactVersion, UUID(out["artifact_version_id"]))
        assert version is not None
        assert version.lineage_jsonb["producing_code"] == code
        assert (
            version.lineage_jsonb["producing_code_sha256"]
            == hashlib.sha256(code.encode()).hexdigest()
        )
        assert version.sha256 == hashlib.sha256(_PNG).hexdigest()
        assert version.builder_agent_run_id == run_id
        assert version.bytes_size == len(_PNG)

        # A card was pinned to Briefs referencing the artifact by URI.
        card = await session.get(InteractiveCard, UUID(out["card_id"]))
        assert card is not None and card.pinned_to == "briefs"
        assert card.card_kind == "ImageCard"

        # Both mutations are ledgered.
        kinds = set(
            (
                await session.execute(
                    select(ActionLedgerEvent.action_kind).where(
                        ActionLedgerEvent.project_id == project_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "artifact.version.create" in kinds
        assert "card.pin" in kinds

        run = await session.get(AgentRun, run_id)
        assert run is not None and run.status == "succeeded"


async def test_render_code_job_fails_closed_on_runner_error(http_client, auth_bypass, asgi_app):
    from aleph_db.models.agent import AgentRun
    from aleph_workers.jobs.render_code import render_code_artifact_job

    project_id = await _make_project(http_client)
    run_id, token = await _make_pending_run(asgi_app, project_id)

    pool = _FakePool({"ok": False, "error": "network access is disabled"})
    out = await render_code_artifact_job(
        _ctx(asgi_app, pool), str(project_id), "code", "png", "t", True, token
    )
    assert out["ok"] is False
    async with asgi_app.state.session_maker() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None and run.status == "failed"
