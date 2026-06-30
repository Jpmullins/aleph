"""bootstrap_project_job: scope -> seed overview page -> dispatch research.

Integration: real DB + real WikiService.commit_revision. The LLM gateway is
patched to return the scope JSON, and dispatch_research is patched to record
the topics it would fan out to (no AIQ needed).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _noop_enqueue(*_args: object, **_kwargs: object) -> None:
    """Stand-in for ArqRedis.enqueue_job (bootstrap enqueues curate_page_job)."""
    return None


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-boot", "email": "boot@test.local", "name": "Boot"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


@pytest.fixture
def patched_scope_gateway(monkeypatch):
    """LiteLLM gateway returns the bootstrap scope JSON (4 topics -> cap to 3)."""
    from aleph_models import client as client_mod

    scope = json.dumps(
        {
            "overview_md": "Overview of [[Topic A]], [[Topic B]], and [[Topic C]].",
            "seed_topics": ["Topic A", "Topic B", "Topic C", "Topic D"],
        }
    )
    canned = {
        "id": "chatcmpl-boot",
        "model": "claude-haiku-4-5",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": scope},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 40,
            "total_tokens": 60,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }

    async def fake_post(self, path, payload):
        assert path == "/v1/chat/completions"
        return canned

    monkeypatch.setattr(client_mod.LiteLLMClient, "_post_with_retry", fake_post)


async def test_bootstrap_job_seeds_overview_and_caps_dispatch(
    http_client, auth_bypass, patched_scope_gateway, asgi_app, monkeypatch
):
    # Disable the auto-trigger so the API doesn't also create a bootstrap run;
    # this test drives the job directly on a run it creates.
    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)

    # A real project (seeds profile + connector bindings).
    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Sandworm APT", "description": "OSINT on the group", "budget_usd": "5.00"},
    )
    assert proj.status_code == 201, proj.text
    pid = UUID(proj.json()["id"])

    maker = asgi_app.state.session_maker
    settings = asgi_app.state.settings

    # Manually create the bootstrap AgentRun (Task 5 does this in the route).
    from aleph_core.ids import uuid7
    from aleph_db.models.agent import AgentRun
    from aleph_db.models.project import Project
    from aleph_security.agent_token import mint_agent_token

    async with maker() as session:
        project = (await session.execute(select(Project).where(Project.id == pid))).scalar_one()
        owner_id = project.created_by
        run_id = uuid7()
        corr = f"bootstrap-{run_id.hex}"
        session.add(
            AgentRun(
                id=run_id,
                project_id=pid,
                agent_kind="bootstrap",
                correlation_id=corr,
                status="pending",
                input_payload={},
                created_by=owner_id,
                access_scope="project",
            )
        )
        await session.commit()

    token = mint_agent_token(
        secret=settings.aleph_agent_token_secret,
        user_id=owner_id,
        project_id=pid,
        agent_run_id=run_id,
        actor_kind="aleph_agent",
        correlation_id=corr,
        ttl_seconds=3600,
    )

    # Patch dispatch_research to record topics (no AIQ).
    import aleph_workers.jobs.bootstrap as b
    from aleph_aiq.dispatch import StartedResearch

    dispatched: list[str] = []

    async def fake_dispatch(**kw):
        dispatched.append(kw["topic"])
        return StartedResearch(uuid7(), "c", "job", True)

    monkeypatch.setattr(b, "dispatch_research", fake_dispatch)

    ctx = {
        "session_maker": maker,
        "litellm_client": asgi_app.state.litellm,
        "settings": settings,
        # dispatch_research is mocked, but bootstrap also enqueues curate_page_job
        # for the seeded overview, so the fake pool needs an async enqueue_job.
        "redis_pool": SimpleNamespace(enqueue_job=_noop_enqueue),
        "agent_token_secret": settings.aleph_agent_token_secret,
    }

    out = await b.bootstrap_project_job(ctx, str(pid), str(run_id), token)

    assert out["status"] == "succeeded"
    # 4 topics returned, capped to bootstrap_max_topics (default 3).
    assert len(dispatched) == 3

    # An overview wiki page was committed as a draft.
    from aleph_wiki.models import WikiPage

    async with maker() as session:
        pages = list(
            (await session.execute(select(WikiPage).where(WikiPage.project_id == pid)))
            .scalars()
            .all()
        )
        run = (await session.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
    assert any(p.title == "Sandworm APT" and p.status == "draft" for p in pages)
    assert run.status == "succeeded"
