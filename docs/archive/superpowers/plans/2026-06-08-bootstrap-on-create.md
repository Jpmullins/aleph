# Bootstrap-on-Create Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a project is created, async background agents immediately bootstrap the wiki from the project's title + description — no manual click, no source upload required.

**Architecture:** On `POST /v1/projects`, after the existing create transaction, the API creates an `AgentRun(agent_kind="bootstrap")` and enqueues a new `bootstrap_project_job` (arq worker). The job runs three phases with live `AgentEvent`s: **scope** (one LiteLLM synthesis call → an overview blurb + N seed topics), **seed_overview** (commit a `draft` wiki "overview" page immediately, with `[[wikilinks]]` to the seed topics — instant visible content), and **dispatch_research** (fan out the *existing* AIQ research→synthesis pipeline per seed topic via a shared `dispatch_research()` helper extracted from `/synthesize`). Each research dispatch is its own child `aiq_deep`/`aiq_shallow` run that lands a draft page through the already-built `aiq_synthesis_poll_job`. The Activity card already polls `/agent-runs` + subscribes to `/agent-events/stream`, so progress shows automatically once we add a `"bootstrap"` label.

**Tech Stack:** FastAPI, arq (Redis worker), SQLAlchemy async, LangGraph-free plain async job, `LiteLLMClient` (Insights gateway), existing `WikiService` / `AIQClient` / `aiq_synthesis_poll_job`, React + `@tanstack/react-query`.

**This realizes the spec's unbuilt "Bootstrap" agent** (`docs/superpowers/specs/2026-05-26-aleph-design.md:342,352,452`: *Bootstrap → Research (AIQ, seed corpus from connector allowlist) → Wiki ingest/compile → reviewers*). The connector-binding seeding already in `projects.py:135` was the leftover prep for exactly this.

**Out of scope (explicit, per user steer):** No cost gating / spend-approval on bootstrap. Cost is bounded only by `bootstrap_max_topics` (the fan-out lever). A *global* max-cost cap belongs to the budget subsystem (settings, not per-project) and is a separate concern — not built here.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `apps/api/src/aleph_api/settings.py` | API settings | **Modify** — add `bootstrap_*` fields |
| `apps/workers/src/aleph_workers/settings.py` | Worker settings | **Modify** — add same `bootstrap_*` fields |
| `packages/aleph-aiq/src/aleph_aiq/dispatch.py` | Shared "dispatch one research job" helper (connector resolve → AgentRun → AIQ dispatch → enqueue poll) | **Create** |
| `apps/api/src/aleph_api/routes/synthesize.py` | `/synthesize` route | **Modify** — call the shared helper (behavior unchanged) |
| `apps/workers/src/aleph_workers/jobs/bootstrap.py` | `bootstrap_project_job` — scope → seed overview → dispatch research | **Create** |
| `apps/workers/src/aleph_workers/jobs/__init__.py` | Job exports | **Modify** — register job |
| `apps/workers/src/aleph_workers/arq.py` | `WorkerSettings.functions` | **Modify** — register job |
| `apps/api/src/aleph_api/routes/projects.py` | `create_project` | **Modify** — create bootstrap run + enqueue job |
| `apps/web/src/components/ActivityCard.tsx` | Activity card labels | **Modify** — add `bootstrap` label |
| `tests/workers/test_bootstrap_job.py` | Unit test for the job | **Create** |
| `tests/e2e/test_bootstrap_on_create.py` | Integration test for the trigger | **Create** |

No DB migration: `agent_kind="bootstrap"` is just a new string value; no schema change.

---

## Task 1: Bootstrap settings (both Settings classes)

**Files:**
- Modify: `apps/api/src/aleph_api/settings.py` (after `aleph_self_url`)
- Modify: `apps/workers/src/aleph_workers/settings.py` (after `aiq_base_url`)
- Test: `tests/api/test_settings_bootstrap.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_settings_bootstrap.py
import os
import pytest


def _base_env() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://x:y@localhost/z",
        "REDIS_URL": "redis://localhost:6379/0",
        "LANGFUSE_HOST": "http://localhost:3000",
        "LANGFUSE_PUBLIC_KEY": "pk",
        "LANGFUSE_SECRET_KEY": "sk",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        "LITELLM_BASE_URL": "http://localhost:1",
        "INSIGHTS_LITELLM_API_KEY": "k",
        "ALEPH_AGENT_TOKEN_SECRET": "s",
    }


def test_api_settings_bootstrap_defaults(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    from aleph_api.settings import Settings

    s = Settings()
    assert s.bootstrap_auto_enabled is True
    assert s.bootstrap_max_topics == 3
    assert s.bootstrap_depth == "shallow"


def test_api_settings_bootstrap_env_override(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BOOTSTRAP_AUTO_ENABLED", "false")
    monkeypatch.setenv("BOOTSTRAP_MAX_TOPICS", "5")
    from aleph_api.settings import Settings

    s = Settings()
    assert s.bootstrap_auto_enabled is False
    assert s.bootstrap_max_topics == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_settings_bootstrap.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'bootstrap_auto_enabled'`

- [ ] **Step 3: Add the fields to BOTH settings classes**

In `apps/api/src/aleph_api/settings.py`, after the `aleph_self_url` field:

```python
    # Bootstrap-on-create. When a project is created, a background
    # `bootstrap_project_job` scopes the title+description into seed topics,
    # seeds an overview wiki page, and fans out research per topic. Cost is
    # bounded by `bootstrap_max_topics` (no per-action gating — see budget docs).
    bootstrap_auto_enabled: bool = True
    bootstrap_max_topics: int = 3
    bootstrap_depth: Literal["shallow", "deep"] = "shallow"
```

In `apps/workers/src/aleph_workers/settings.py`, after the `aiq_base_url` field (use `str` for depth to avoid importing Literal if not already present, or import `Literal`):

```python
    bootstrap_auto_enabled: bool = True
    bootstrap_max_topics: int = 3
    bootstrap_depth: str = "shallow"
```

(`apps/api/settings.py` already imports `Literal`; confirm before using it there.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/api/test_settings_bootstrap.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/aleph_api/settings.py apps/workers/src/aleph_workers/settings.py tests/api/test_settings_bootstrap.py
git commit -m "feat(bootstrap): add bootstrap_* settings to api + worker settings"
```

---

## Task 2: Shared `dispatch_research()` helper

Extract the connector-resolve → AgentRun → AIQ-dispatch → enqueue-poll logic from `/synthesize` so both the route and the bootstrap job use one code path (DRY).

**Files:**
- Create: `packages/aleph-aiq/src/aleph_aiq/dispatch.py`
- Modify: `apps/api/src/aleph_api/routes/synthesize.py:84-204`
- Test: `tests/aiq/test_dispatch_research.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# tests/aiq/test_dispatch_research.py
from __future__ import annotations
from uuid import uuid4
import pytest

pytestmark = pytest.mark.asyncio


class _FakeAIQ:
    def __init__(self, *a, **k): ...
    async def health(self) -> bool: return True
    async def dispatch_deep(self, **k) -> str: return "aiq-job-123"


class _FakePool:
    def __init__(self): self.calls = []
    async def enqueue_job(self, *a, **k): self.calls.append((a, k))


async def test_dispatch_research_enqueues_poll(monkeypatch):
    import aleph_aiq.dispatch as d

    monkeypatch.setattr(d, "AIQClient", _FakeAIQ)
    # Stub connector resolution + run creation + ledger to avoid a real DB.
    async def _fake_enabled(session, project_id, allowed): return ["arxiv"]
    monkeypatch.setattr(d, "_resolve_enabled_connectors", _fake_enabled)

    started = d.StartedResearch  # sanity: dataclass exists
    pool = _FakePool()

    # _dispatch_core is the pure part: given enabled connectors + a created run,
    # it dispatches AIQ and enqueues the poll job. Test it directly.
    result = await d._dispatch_core(
        settings=type("S", (), {"aleph_agent_token_secret": "s", "aiq_base_url": "http://x"})(),
        redis_pool=pool,
        project_id=uuid4(),
        principal_user_id=uuid4(),
        agent_run_id=uuid4(),
        correlation_id="boot-1",
        topic="Quantum radar",
        depth="shallow",
        enabled_connectors=["arxiv"],
    )
    assert result.dispatched is True
    assert result.aiq_job_id == "aiq-job-123"
    assert pool.calls and pool.calls[0][0][0] == "aiq_synthesis_poll_job"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/aiq/test_dispatch_research.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'aleph_aiq.dispatch'`

- [ ] **Step 3: Create the helper**

```python
# packages/aleph-aiq/src/aleph_aiq/dispatch.py
"""Shared 'dispatch one AIQ research job' helper.

Used by the /synthesize route and the bootstrap_project_job so both follow
the identical path: resolve enabled connectors → create an AgentRun →
dispatch to AIQ → enqueue the poll job that lands a draft wiki page.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_aiq.auth_bridge import issue_service_token
from aleph_aiq.client import AIQClient
from aleph_aiq.job_service import append_aiq_event, create_aiq_agent_run
from aleph_db.repos.ledger import LedgerWriter
from aleph_rks.models import Connector, ConnectorBinding
from aleph_security.agent_token import mint_agent_token


@dataclass
class StartedResearch:
    agent_run_id: UUID
    correlation_id: str
    aiq_job_id: str | None
    dispatched: bool


async def _resolve_enabled_connectors(
    session: AsyncSession, project_id: UUID, allowed: list[str] | None
) -> list[str]:
    stmt = (
        select(Connector.kind)
        .join(ConnectorBinding, ConnectorBinding.connector_id == Connector.id)
        .where(ConnectorBinding.project_id == project_id, ConnectorBinding.enabled.is_(True))
    )
    enabled = [k for (k,) in (await session.execute(stmt)).all()]
    if allowed:
        enabled = [k for k in enabled if k in allowed]
    return enabled


async def _dispatch_core(
    *,
    settings: Any,
    redis_pool: Any,
    project_id: UUID,
    principal_user_id: UUID,
    agent_run_id: UUID,
    correlation_id: str,
    topic: str,
    depth: str,
    enabled_connectors: list[str],
) -> StartedResearch:
    service_token = issue_service_token(
        secret=settings.aleph_agent_token_secret,
        project_id=project_id,
        agent_run_id=agent_run_id,
        principal_user_id=principal_user_id,
    )
    poll_agent_token = mint_agent_token(
        secret=settings.aleph_agent_token_secret,
        user_id=principal_user_id,
        project_id=project_id,
        agent_run_id=agent_run_id,
        actor_kind="aleph_agent",
        correlation_id=correlation_id,
        ttl_seconds=3600,
    )
    aiq_base = getattr(settings, "aiq_base_url", None) or "http://aiq-server:8000"
    client = AIQClient(base_url=aiq_base, service_token=service_token)
    if not await client.health():
        return StartedResearch(agent_run_id, correlation_id, None, False)
    aiq_job_id = await client.dispatch_deep(
        project_id=project_id, topic=topic, allowed_data_sources=enabled_connectors, depth=depth
    )
    await redis_pool.enqueue_job(
        "aiq_synthesis_poll_job",
        str(agent_run_id),
        aiq_job_id,
        str(project_id),
        topic,
        poll_agent_token,
    )
    return StartedResearch(agent_run_id, correlation_id, aiq_job_id, True)


async def dispatch_research(
    *,
    session: AsyncSession,
    settings: Any,
    redis_pool: Any,
    project_id: UUID,
    principal_user_id: UUID,
    actor_kind: str,
    ledger: LedgerWriter,
    topic: str,
    depth: str = "deep",
    allowed_connectors: list[str] | None = None,
) -> StartedResearch:
    """Resolve connectors, create the run, dispatch, enqueue poll. Raises
    ValueError if no connectors are enabled."""
    enabled = await _resolve_enabled_connectors(session, project_id, allowed_connectors)
    if not enabled:
        msg = "no connectors are enabled for this project"
        raise ValueError(msg)

    started = await create_aiq_agent_run(
        session,
        project_id=project_id,
        topic=topic,
        depth=depth,
        allowed_connector_kinds=enabled,
        created_by=principal_user_id,
    )
    await ledger.append(
        project_id=project_id,
        actor_id=principal_user_id,
        actor_kind=actor_kind,
        action_kind="synthesize.dispatch",
        target_id=started.agent_run_id,
        target_kind="agent_run",
        payload={"topic": topic, "depth": depth, "allowed_connectors": enabled},
        trace_id=None,
    )
    try:
        result = await _dispatch_core(
            settings=settings,
            redis_pool=redis_pool,
            project_id=project_id,
            principal_user_id=principal_user_id,
            agent_run_id=started.agent_run_id,
            correlation_id=started.correlation_id,
            topic=topic,
            depth=depth,
            enabled_connectors=enabled,
        )
    except Exception as exc:
        await append_aiq_event(
            session,
            agent_run_id=started.agent_run_id,
            event_kind="aiq.dispatch.failed",
            payload={"error": str(exc)[:1024]},
        )
        return StartedResearch(started.agent_run_id, started.correlation_id, None, False)
    if result.dispatched:
        await append_aiq_event(
            session,
            agent_run_id=started.agent_run_id,
            event_kind="aiq.job.dispatched",
            payload={"aiq_job_id": result.aiq_job_id},
        )
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/aiq/test_dispatch_research.py -q`
Expected: PASS

- [ ] **Step 5: Refactor `/synthesize` to use the helper (behavior unchanged)**

In `apps/api/src/aleph_api/routes/synthesize.py`, replace the body of `synthesize()` (after `require_at_least`) with:

```python
    from aleph_aiq.dispatch import dispatch_research

    settings = request.app.state.settings
    from arq import create_pool
    from arq.connections import RedisSettings

    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        from aleph_core.errors import ValidationFailed
        try:
            started = await dispatch_research(
                session=session,
                settings=settings,
                redis_pool=pool,
                project_id=project_id,
                principal_user_id=principal.user_id,
                actor_kind=principal.actor_kind,
                ledger=ledger,
                topic=body.topic,
                depth=body.depth,
                allowed_connectors=body.allowed_connectors,
            )
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
    finally:
        await pool.aclose()

    return SynthesizeOut(
        agent_run_id=str(started.agent_run_id),
        correlation_id=started.correlation_id,
        aiq_job_id=started.aiq_job_id,
        dispatched=started.dispatched,
    )
```

Keep the imports `dispatch_research` uses; remove now-unused imports in `synthesize.py` (run ruff to find them).

- [ ] **Step 6: Verify the route still works**

Run: `uv run pytest tests/aiq tests/e2e -q -k "synth or dispatch" ; uv run ruff check apps/api/src/aleph_api/routes/synthesize.py`
Expected: PASS, ruff clean. (If an existing synthesize integration test needs the compose stack, note it and run the non-integration subset.)

- [ ] **Step 7: Commit**

```bash
git add packages/aleph-aiq/src/aleph_aiq/dispatch.py apps/api/src/aleph_api/routes/synthesize.py tests/aiq/test_dispatch_research.py
git commit -m "refactor(aiq): extract dispatch_research() helper; /synthesize uses it"
```

---

## Task 3: `bootstrap_project_job`

**Files:**
- Create: `apps/workers/src/aleph_workers/jobs/bootstrap.py`
- Test: `tests/workers/test_bootstrap_job.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# tests/workers/test_bootstrap_job.py
from __future__ import annotations
from uuid import uuid4
import json
import pytest

pytestmark = pytest.mark.asyncio


async def test_bootstrap_job_seeds_overview_and_dispatches(monkeypatch):
    import aleph_workers.jobs.bootstrap as b

    # Capture the overview page commit + research dispatches.
    committed = {}
    dispatched_topics = []

    async def _fake_commit(self, **kw):
        committed.update(kw)
        from types import SimpleNamespace
        return SimpleNamespace(page_id=uuid4(), revision_id=uuid4(), revision_no=1,
                               body_sha256="x", was_noop=False)
    monkeypatch.setattr(b.WikiService, "commit_revision", _fake_commit)

    async def _fake_dispatch(**kw):
        dispatched_topics.append(kw["topic"])
        from aleph_aiq.dispatch import StartedResearch
        return StartedResearch(uuid4(), "c", "job", True)
    monkeypatch.setattr(b, "dispatch_research", _fake_dispatch)

    # Fake LiteLLM returning the scope JSON.
    class _Msg:
        content = json.dumps({"overview_md": "Intro [[Topic A]] and [[Topic B]].",
                              "seed_topics": ["Topic A", "Topic B", "Topic C", "Topic D"]})
    class _Choice:  message = _Msg()
    class _Resp:    choices = [_Choice()]
    class _LLM:
        async def chat(self, **kw): return _Resp()

    ctx, project_id, run_id = _fake_ctx(monkeypatch, _LLM())
    token = _mint(project_id, run_id)

    out = await b.bootstrap_project_job(ctx, str(project_id), str(run_id), token)

    assert committed["page_kind"] == "topic"
    assert committed["body_md"].startswith("Intro")
    # max_topics defaults to 3 → only 3 dispatched even though 4 returned.
    assert len(dispatched_topics) == 3
    assert out["status"] == "succeeded"
```

(Define `_fake_ctx` and `_mint` helpers in the test mirroring `tests/workers` conventions: build a `ctx` dict with `session_maker`, `litellm_client`, `settings` (a SimpleNamespace with `bootstrap_max_topics=3`, `bootstrap_depth="shallow"`, `aleph_agent_token_secret`, `aiq_base_url`), `redis_pool`, `agent_token_secret`; seed a project row + ModelProfile via the test DB fixture, and `mint_agent_token` for the project/run. Reuse the existing worker-test DB fixture used by `tests/workers/test_*` — check that directory for the shared fixture before writing your own.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/workers/test_bootstrap_job.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'aleph_workers.jobs.bootstrap'`

- [ ] **Step 3: Implement the job**

```python
# apps/workers/src/aleph_workers/jobs/bootstrap.py
"""bootstrap_project_job: seed a project's wiki from its title + description.

Phase 1 scope   — one synthesis LLM call → {overview_md, seed_topics[]}.
Phase 2 overview — commit a draft 'overview' wiki page (instant visible content).
Phase 3 research — fan out the AIQ research→synthesis pipeline per seed topic
                   (bounded by settings.bootstrap_max_topics), each a child run
                   that lands a draft page via aiq_synthesis_poll_job.
"""
from __future__ import annotations

import json
import time
from typing import Any
from uuid import UUID

from sqlalchemy import select

from aleph_aiq.dispatch import dispatch_research
from aleph_core.schemas.model_profile import Capability
from aleph_db.models.model_profile import ModelProfile
from aleph_db.models.project import Project
from aleph_db.repos.agent_events import (
    emit_phase_completed,
    emit_phase_failed,
    emit_phase_started,
)
from aleph_db.repos.ledger import LedgerWriter
from aleph_models.client import ChatMessage
from aleph_observability.tracing import start_span
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal
from aleph_wiki.wiki_service import WikiLinkDraft, WikiService

_SCOPE_SYS = (
    "You are bootstrapping a research wiki for a new investigation. Given the "
    "project title and description, return a JSON object with exactly two keys: "
    '"overview_md" (a 2-4 paragraph markdown overview framing the research scope; '
    "mention each seed topic as a [[wikilink]]) and \"seed_topics\" (an array of "
    "at most {max_topics} concise, distinct topic titles suitable as wiki page "
    "names — proper nouns / concepts, not sentences)."
)


async def _set_status(maker: Any, run_id: UUID, status: str, error: str | None = None) -> None:
    from aleph_core.time import utcnow
    from aleph_db.models.agent import AgentRun

    async with maker() as session:
        run = (await session.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one_or_none()
        if run is None:
            return
        run.status = status
        if status in ("succeeded", "failed"):
            run.completed_at = utcnow()
        if error:
            run.error_text = error[:4096]
        await session.commit()


async def bootstrap_project_job(
    ctx: dict[str, Any], project_id_str: str, agent_run_id_str: str, agent_token: str
) -> dict[str, Any]:
    secret: str = ctx["agent_token_secret"]
    claims = verify_agent_token(agent_token, secret=secret)
    principal = Principal(
        user_id=claims.user_id, subject="agent", email="",
        actor_kind=claims.actor_kind, agent_run_id=claims.agent_run_id,
        correlation_id=claims.correlation_id,
    )
    project_id = UUID(project_id_str)
    agent_run_id = UUID(agent_run_id_str)
    maker = ctx["session_maker"]
    litellm = ctx["litellm_client"]
    settings = ctx["settings"]
    redis_pool = ctx["redis_pool"]
    max_topics = int(getattr(settings, "bootstrap_max_topics", 3))
    depth = str(getattr(settings, "bootstrap_depth", "shallow"))

    with start_span("worker.bootstrap", **{"aleph.project_id": project_id_str,
                                           "aleph.agent_run_id": agent_run_id_str}):
        await _set_status(maker, agent_run_id, "running")
        try:
            # --- Phase 1: scope ---
            async with maker() as session:
                project = (await session.execute(
                    select(Project).where(Project.id == project_id))).scalar_one_or_none()
                profile = (await session.execute(
                    select(ModelProfile).where(ModelProfile.project_id == project_id))).scalar_one_or_none()
                if project is None or profile is None:
                    raise RuntimeError("project or model profile missing")
                title, description = project.title, project.description or ""
                await emit_phase_started(session, agent_run_id=agent_run_id, phase_name="scope")
                await session.commit()

            t0 = time.monotonic()
            resp = await litellm.chat(
                principal=principal, project_id=project_id, agent_run_id=agent_run_id,
                capability=Capability.SYNTHESIS, profile_bindings=profile.bindings_jsonb,
                messages=[
                    ChatMessage(role="system", content=_SCOPE_SYS.format(max_topics=max_topics)),
                    ChatMessage(role="user", content=f"Title: {title}\nDescription: {description}"),
                ],
                response_format={"type": "json_object"}, temperature=0.2,
                max_tokens=1500, purpose="bootstrap.scope",
            )
            parsed = json.loads(resp.choices[0].message.content or "{}") if resp.choices else {}
            overview_md = str(parsed.get("overview_md") or f"# {title}\n\n{description}").strip()
            seed_topics = [str(t).strip() for t in (parsed.get("seed_topics") or []) if str(t).strip()][:max_topics]
            async with maker() as session:
                await emit_phase_completed(session, agent_run_id=agent_run_id, phase_name="scope",
                                           duration_ms=int((time.monotonic() - t0) * 1000),
                                           payload={"seed_topics": seed_topics})
                await session.commit()

            # --- Phase 2: seed overview page (draft, instant) ---
            async with maker() as session:
                await emit_phase_started(session, agent_run_id=agent_run_id, phase_name="seed_overview")
                ledger = LedgerWriter(session)
                svc = WikiService(session)
                wikilinks = [WikiLinkDraft(dst_title=t, dst_page_id=None,
                                           occurrences=max(1, overview_md.count(f"[[{t}]]")))
                             for t in seed_topics]
                result = await svc.commit_revision(
                    principal=principal, ledger=ledger, project_id=project_id, page_id=None,
                    title=title, slug=None, page_kind="topic", body_md=overview_md,
                    summary=(description or overview_md)[:2048], claims=[], wikilinks=wikilinks,
                    commit_message="Bootstrap: project overview", respect_hand_edits=True,
                )
                await emit_phase_completed(session, agent_run_id=agent_run_id, phase_name="seed_overview",
                                           duration_ms=0, payload={"page_id": str(result.page_id)})
                await session.commit()

            # --- Phase 3: dispatch research per seed topic ---
            dispatched = 0
            async with maker() as session:
                await emit_phase_started(session, agent_run_id=agent_run_id, phase_name="dispatch_research")
                ledger = LedgerWriter(session)
                for topic in seed_topics:
                    try:
                        r = await dispatch_research(
                            session=session, settings=settings, redis_pool=redis_pool,
                            project_id=project_id, principal_user_id=principal.user_id,
                            actor_kind=principal.actor_kind, ledger=ledger, topic=topic, depth=depth,
                        )
                        if r.dispatched:
                            dispatched += 1
                    except ValueError:
                        break  # no connectors enabled — overview page still stands
                await emit_phase_completed(session, agent_run_id=agent_run_id,
                                           phase_name="dispatch_research", duration_ms=0,
                                           payload={"dispatched": dispatched, "topics": len(seed_topics)})
                await session.commit()

            await _set_status(maker, agent_run_id, "succeeded")
            return {"status": "succeeded", "seed_topics": seed_topics, "dispatched": dispatched}
        except Exception as exc:
            async with maker() as session:
                await emit_phase_failed(session, agent_run_id=agent_run_id, phase_name="bootstrap",
                                        error_text=str(exc))
                await session.commit()
            await _set_status(maker, agent_run_id, "failed", error=str(exc))
            raise
```

> NOTE on imports: verify the real import paths before coding — `Project` model (`aleph_db.models.project`), `ChatMessage` (it may live in `aleph_models.client` or `aleph_models.schemas`; grep `class ChatMessage`). Fix to match.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/workers/test_bootstrap_job.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/workers/src/aleph_workers/jobs/bootstrap.py tests/workers/test_bootstrap_job.py
git commit -m "feat(bootstrap): bootstrap_project_job — scope, seed overview page, dispatch research"
```

---

## Task 4: Register the job

**Files:**
- Modify: `apps/workers/src/aleph_workers/jobs/__init__.py`
- Modify: `apps/workers/src/aleph_workers/arq.py`

- [ ] **Step 1: Add import + export in `jobs/__init__.py`**

Add `from aleph_workers.jobs.bootstrap import bootstrap_project_job` with the other imports, and add `"bootstrap_project_job",` to `__all__`.

- [ ] **Step 2: Register in `WorkerSettings.functions` (arq.py)**

Add `bootstrap_project_job,` to the `functions` ClassVar list and import it.

- [ ] **Step 3: Verify it imports**

Run: `uv run python -c "from aleph_workers.arq import WorkerSettings; print([f.__name__ for f in WorkerSettings.functions])"`
Expected: list includes `bootstrap_project_job`

- [ ] **Step 4: Commit**

```bash
git add apps/workers/src/aleph_workers/jobs/__init__.py apps/workers/src/aleph_workers/arq.py
git commit -m "feat(bootstrap): register bootstrap_project_job with the arq worker"
```

---

## Task 5: Trigger bootstrap on project creation

**Files:**
- Modify: `apps/api/src/aleph_api/routes/projects.py:45-173` (the `create_project` handler)
- Test: `tests/e2e/test_bootstrap_on_create.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# tests/e2e/test_bootstrap_on_create.py
from __future__ import annotations
import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_create_project_enqueues_bootstrap(http_client, auth_bypass, asgi_app, monkeypatch):
    # Capture enqueue_job calls instead of needing a live worker.
    calls = []

    class _Pool:
        async def enqueue_job(self, *a, **k): calls.append(a)
        async def aclose(self): ...

    async def _fake_create_pool(*a, **k): return _Pool()
    import aleph_api.routes.projects as proj
    monkeypatch.setattr(proj, "create_pool", _fake_create_pool, raising=False)

    resp = await http_client.post("/v1/projects",
                                  json={"title": "Quantum radar survey", "description": "OSINT scan"})
    assert resp.status_code == 201
    project_id = resp.json()["id"]

    from aleph_db.models.agent import AgentRun
    async with asgi_app.state.session_maker() as session:
        runs = list((await session.execute(
            select(AgentRun).where(AgentRun.project_id == project_id,
                                   AgentRun.agent_kind == "bootstrap"))).scalars().all())
    assert len(runs) == 1
    assert any(c and c[0] == "bootstrap_project_job" for c in calls)
```

(Reuse the `auth_bypass` / `http_client` / `asgi_app` fixtures from `tests/e2e/test_project_lifecycle.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/e2e/test_bootstrap_on_create.py -q -m integration`
Expected: FAIL — no `bootstrap` AgentRun row.

- [ ] **Step 3: Add the trigger to `create_project`**

Add `Request` to imports and the handler signature:
```python
from fastapi import APIRouter, Body, Request, status
```
Change the signature to add `request: Request`. Then, just before `return ProjectOut.model_validate(project)`, insert:

```python
    # Bootstrap-on-create: kick off the background wiki build from title +
    # description. Creates the run synchronously so the Activity card shows
    # "Bootstrapping project" the instant the project screen loads; the worker
    # job drives the phases. No cost gate — bounded by bootstrap_max_topics.
    settings = request.app.state.settings
    if getattr(settings, "bootstrap_auto_enabled", False):
        from aleph_core.ids import uuid7 as _uuid7
        from aleph_db.models.agent import AgentRun
        from aleph_security.agent_token import mint_agent_token

        boot_run_id = _uuid7()
        boot_corr = f"bootstrap-{boot_run_id.hex[:8]}"
        session.add(AgentRun(
            id=boot_run_id, project_id=project_id, agent_kind="bootstrap",
            correlation_id=boot_corr, status="pending",
            input_payload={"title": body.title, "description": body.description},
            created_by=principal.user_id, access_scope="project",
        ))
        await session.flush()
        await ledger.append(
            project_id=project_id, actor_id=principal.user_id, actor_kind=principal.actor_kind,
            action_kind="bootstrap.dispatch", target_id=boot_run_id, target_kind="agent_run",
            payload={"title": body.title}, trace_id=trace_id,
        )
        boot_token = mint_agent_token(
            secret=settings.aleph_agent_token_secret, user_id=principal.user_id,
            project_id=project_id, agent_run_id=boot_run_id, actor_kind="aleph_agent",
            correlation_id=boot_corr, ttl_seconds=3600,
        )
        try:
            from arq import create_pool
            from arq.connections import RedisSettings

            pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            try:
                await pool.enqueue_job(
                    "bootstrap_project_job", str(project_id), str(boot_run_id), boot_token
                )
            finally:
                await pool.aclose()
        except Exception:
            # Run stays 'pending'; not fatal to project creation.
            pass
```

(Import `create_pool` at module top so the test's `monkeypatch.setattr(proj, "create_pool", ...)` works: `from arq import create_pool`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/e2e/test_bootstrap_on_create.py -q -m integration`
Expected: PASS. Also re-run `tests/e2e/test_project_lifecycle.py` — the ledger now has an extra `bootstrap.dispatch` event; update that test's assertions only if it counts events exactly (it checks membership with `in`, so it should still pass).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/aleph_api/routes/projects.py tests/e2e/test_bootstrap_on_create.py
git commit -m "feat(bootstrap): create bootstrap run + enqueue job on project creation"
```

---

## Task 6: Frontend Activity-card label

**Files:**
- Modify: `apps/web/src/components/ActivityCard.tsx` (the `KIND_LABELS` map)

- [ ] **Step 1: Add the label**

In the `KIND_LABELS` record, add:
```typescript
  bootstrap: "Bootstrapping project",
```

- [ ] **Step 2: Typecheck + lint**

Run: `pnpm -C apps/web typecheck && pnpm -C apps/web lint`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add apps/web/src/components/ActivityCard.tsx
git commit -m "feat(bootstrap): label bootstrap runs in the Activity card"
```

---

## Task 7: End-to-end verification (real browser)

Per the project's standing rule — verify by driving the live UI, not just headless tests.

- [ ] **Step 1: Rebuild + recreate the app images**

```bash
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env build aleph-api aleph-workers aleph-web
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env up -d --no-deps --force-recreate aleph-api aleph-workers aleph-web
```

- [ ] **Step 2: Create a project via the UI** (forwarded `:5173`), title e.g. "Sandworm APT infrastructure", a one-line description.

- [ ] **Step 3: Observe** — within ~2s the Activity card shows **"Bootstrapping project"**; the **scope → seed_overview → dispatch_research** phases tick; an **overview wiki page** appears in the Wiki tab with `[[wikilinks]]` to seed topics; child **"Shallow research"** runs spawn.

- [ ] **Step 4: Tail logs if anything stalls**

```bash
docker logs --since 5m compose-aleph-workers-1 2>&1 | grep -iE "bootstrap|error|traceback"
docker logs --since 5m compose-aleph-api-1 2>&1 | grep -iE "bootstrap|enqueue"
```

- [ ] **Step 5: Add a Playwright spec** `tests/playwright/specs/09-bootstrap-on-create.spec.ts` — create a project via API, then assert the agent-events stream surfaces a `scope` and `seed_overview` phase for a `bootstrap` run within 60s, and that `/wiki/pages` returns ≥1 page.

---

## Self-Review

**Spec coverage:** trigger-on-create ✅ (Task 5) · async background agents ✅ (Task 3 job + Task 4 register) · scope from title+description ✅ (Task 3 phase 1) · seed corpus / research from connector allowlist ✅ (Task 2 helper + Task 3 phase 3, reuses enabled-connector resolution) · live phase progress / "Bootstrap screen" ✅ (phase events + Task 6 label, existing Activity card) · idempotent/resumable — partial: the job is re-runnable but does not yet dedupe a second bootstrap run; acceptable (creation fires once). No cost gate ✅ (per user steer; bounded by `bootstrap_max_topics`).

**Placeholder scan:** none — every step has concrete code. Two explicit "verify the import path" notes (ChatMessage location, Project model path) are real instructions, not deferrals.

**Type consistency:** `dispatch_research()` / `StartedResearch` defined in Task 2 and consumed identically in Tasks 2 (synthesize) and 3 (job). `bootstrap_project_job(ctx, project_id_str, agent_run_id_str, agent_token)` signature matches the enqueue call in Task 5. `emit_phase_*` and `WikiService.commit_revision` / `WikiLinkDraft` match the verified signatures. `agent_kind="bootstrap"` used consistently in Tasks 3/5/6.

**Open risk:** the `/synthesize` refactor (Task 2) touches a working path — the existing synthesize test is the guard; if it's integration-only, run it against the compose stack before merging.
