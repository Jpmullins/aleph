# Wave 6 — Complete the conversational pivot: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Live Deep Agent the only chat surface — give it tools to read the wiki deeply, manage hypotheses, ingest sources, manage connectors/settings, and build product artifacts (consequential ones approval-gated); attribute agent LLM cost; give it cross-session memory; consolidate cost UI into the Profile drawer; and delete Classic.

**Architecture:** Extend the existing `deepagents.create_deep_agent` graph in `apps/api/src/aleph_api/copilot_agent.py` with new `@tool`s that call typed `aleph-api` service methods (rule #3) — never the DB directly. Consequential tools use `create_deep_agent`'s native `interrupt_on=`. Cost attribution is a LangChain `BaseCallbackHandler` mirroring `CostWriter.record_call`. Cross-session memory is a langgraph `AsyncPostgresStore` + `CompositeBackend` routing `/memories/`. Frontend changes are localized to `ProjectWorkspace.tsx`, `Drawers.tsx`, `LeftPanel.tsx`, and the A2UI catalogs.

**Tech Stack:** Python 3.13, FastAPI, deepagents 0.6.6, langgraph >=1.2,<2, langchain_openai ChatOpenAI (gateway-only), React 19 + CopilotKit v2, `@a2ui/*`, Node copilot-runtime.

**Build order:** Phase A (tools) → Phase F (retire Classic) → Phase B (approval) → Phase C (cost callback) → Phase D (memory) → Phase E (cost UI + bell). Each phase is one commit, verified in a real browser (Playwright MCP) per the standing per-wave requirement.

---

## File Structure

**Backend — agent tools & wiring (`apps/api/src/aleph_api/`)**
- `copilot_agent.py` — MODIFY. Add tools (`read_wiki`, hypotheses, `ingest_source`, connectors/profile, `build_artifact`); extend `bind_runtime` to carry `litellm`; add `interrupt_on`; wire `store` + `backend`; register the cost callback on the model.
- `copilot_cost_callback.py` — CREATE. `AgentCostCallbackHandler(BaseCallbackHandler)` → writes `ModelCall` + `CostLedgerEvent` via `CostWriter`.
- `routes/sources.py` — MODIFY. Add `POST /sources/ingest-url` (fetch URL bytes → `register_uploaded_source`).
- `lifespan.py` — MODIFY. Pass `litellm` (+ Postgres store) into `bind_runtime` / `build_assistant_deep_agent`.

**Frontend (`apps/web/src/`)**
- `components/ProjectWorkspace.tsx` — MODIFY. Default `chatMode="live"`, remove toggle, drop `CostBanner`.
- `components/ChatSurface.tsx` — DELETE (Classic).
- `components/CostBanner.tsx` — DELETE (cost consolidated into Profile).
- `components/Drawers.tsx` — MODIFY. Add a **Usage** section to `ProfileBody`.
- `components/LeftPanel.tsx` — MODIFY. Bell glyph → monochrome.
- `a2ui/copilot-catalog.tsx` — MODIFY. Add `ArtifactCard` (Zod + renderer).

**Runtime (`apps/copilot-runtime/src/`)**
- `server.ts` — MODIFY. Add `ArtifactCard` to `ALEPH_A2UI_CATALOG`.

**Tests**
- `apps/api/tests/e2e/test_agent_tools.py` — CREATE (integration; ledger assertions per mutating tool).
- `apps/api/tests/unit/test_agent_cost_callback.py` — CREATE (pure cost math + record).
- `apps/web/playwright/specs/09-live-agent-tools.spec.ts` — CREATE (browser verification).

---

## PHASE A — Agent tool suite

### Task A1: `read_wiki` deep-retrieval tool

**Files:**
- Modify: `apps/api/src/aleph_api/copilot_agent.py`
- Modify: `apps/api/src/aleph_api/lifespan.py:107` (bind `litellm`)

- [ ] **Step 1: Extend `bind_runtime` to carry the LiteLLM client**

In `copilot_agent.py`, update the runtime dict + binder:

```python
_runtime: dict[str, Any] = {"session_maker": None, "settings": None, "litellm": None}


def bind_runtime(
    *,
    session_maker: "async_sessionmaker[AsyncSession]",
    settings: "Settings | None" = None,
    litellm: "Any | None" = None,
) -> None:
    _runtime["session_maker"] = session_maker
    if settings is not None:
        _runtime["settings"] = settings
    if litellm is not None:
        _runtime["litellm"] = litellm
```

- [ ] **Step 2: Pass `litellm` from lifespan**

In `lifespan.py`, the `LiteLLMClient` is built at line 78 and stored at `app.state.litellm` (line 110). Update the `bind_runtime` call (currently line ~107):

```python
bind_runtime(session_maker=session_maker, settings=settings, litellm=litellm)
```

- [ ] **Step 3: Add the `read_wiki` tool**

Add to `copilot_agent.py` (after `search_wiki`). It mirrors how `apps/workers/src/aleph_workers/jobs/assistant_turn.py` assembles the router deps: a dev `Principal`, the project `ModelProfile`, and the bound `LiteLLMClient`.

```python
@tool
async def read_wiki(query: str, config: RunnableConfig) -> str:
    """Read the wiki in depth to answer a question with citations.

    Use this (not search_wiki) when the analyst asks a substantive question
    that needs a composed, cited answer. It runs the full wiki-first retrieval
    pipeline: page selection, 1-hop wikilink expansion, answer composition, and
    intra-source descent. Returns a cited markdown answer plus a coverage note.
    """
    from uuid import uuid4

    from sqlalchemy import select

    from aleph_db.models.model_profile import ModelProfile
    from aleph_security.principal import Principal
    from aleph_assistant.retrieval.router import WikiFirstRetrievalRouter

    session_maker = _runtime.get("session_maker")
    litellm = _runtime.get("litellm")
    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if session_maker is None or litellm is None or project_id is None:
        return "Deep wiki reading is unavailable (no project scope on this run)."

    principal = Principal(
        subject=getattr(settings, "local_dev_subject", "dev@aleph.local"),
        actor_kind="user",
        email=getattr(settings, "local_dev_email", "dev@aleph.local"),
        display_name=getattr(settings, "local_dev_display_name", "Dev"),
    )
    async with session_maker() as session:
        profile = (
            await session.execute(
                select(ModelProfile).where(ModelProfile.project_id == project_id)
            )
        ).scalar_one_or_none()
    if profile is None:
        return "No model profile bound to this project; cannot read the wiki."

    router = WikiFirstRetrievalRouter(session_maker=session_maker, litellm=litellm)
    result = await router.retrieve(
        principal=principal,
        project_id=project_id,
        thread_id=uuid4(),
        query=query,
        prior_messages=[],
        profile=profile,
        agent_run_id=None,
    )
    coverage = getattr(result, "coverage_judgment", "ok")
    body = getattr(result, "composed_body_md", "") or "(the composer returned no body)"
    return f"{body}\n\n_(coverage: {coverage})_"
```

> NOTE: confirm `Principal(...)` kwargs against `apps/workers/src/aleph_workers/jobs/assistant_turn.py:30` and adjust field names if they differ. The router's `RetrievalResult` attributes (`composed_body_md`, `coverage_judgment`) are confirmed in `packages/aleph-assistant/src/aleph_assistant/retrieval/router.py`.

- [ ] **Step 4: Register the tool**

In `build_assistant_deep_agent`, add `read_wiki` to `tools=[...]`:

```python
tools=[search_wiki, read_wiki, start_research],
```

And update `SYSTEM_PROMPT`: replace "ALWAYS call `search_wiki` first" guidance with "Use `search_wiki` for a quick scan of what pages exist; use `read_wiki` to actually answer a question with a cited, composed answer."

- [ ] **Step 5: Restart api + verify in browser**

```bash
docker compose -f deploy/compose/docker-compose.yml restart aleph-api
```
Then drive the Live chat (Playwright MCP) on a project whose wiki has content: ask a substantive question, confirm the agent calls `read_wiki` and returns a cited answer with a coverage note. Expected: composed answer with `[[Page]]` citations, not just a list of page titles.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/aleph_api/copilot_agent.py apps/api/src/aleph_api/lifespan.py
git commit -m "Wave 6 A1: read_wiki deep-retrieval tool (wraps WikiFirstRetrievalRouter)"
```

---

### Task A2: Hypotheses tools (list / create / add evidence)

**Files:**
- Modify: `apps/api/src/aleph_api/copilot_agent.py`

Service signatures (confirmed): `aleph_hypotheses.hypothesis_service.create_hypothesis(session, *, ledger, principal, project_id, title, statement)`, `list_hypotheses(session, *, project_id)`, `add_evidence(session, *, ledger, principal, hypothesis_id, stance, evidence_kind, target_id, weight=1.0, note="")`. These mutating calls require a `LedgerWriter` (rule #4). Build it the way routes do.

- [ ] **Step 1: Add a ledger helper + the three tools**

```python
@tool
async def list_hypotheses_tool(config: RunnableConfig) -> str:
    """List the project's analytic hypotheses with their confidence."""
    from aleph_hypotheses.hypothesis_service import list_hypotheses

    session_maker = _runtime.get("session_maker")
    project_id = _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "Hypotheses are unavailable (no project scope)."
    async with session_maker() as session:
        rows = await list_hypotheses(session, project_id=project_id)
    if not rows:
        return "No hypotheses yet. Offer to create one."
    return "Hypotheses:\n" + "\n".join(
        f"- [{h.short_id}] {h.title} — confidence {getattr(h, 'confidence', 'n/a')}" for h in rows
    )


@tool
async def create_hypothesis_tool(title: str, statement: str, config: RunnableConfig) -> str:
    """Create a new analytic hypothesis. Render a HypothesisCard for the result."""
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_hypotheses.hypothesis_service import create_hypothesis

    session_maker = _runtime.get("session_maker")
    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if session_maker is None or project_id is None:
        return "Cannot create hypothesis (no project scope)."
    principal = _dev_principal(settings)
    async with session_maker() as session:
        ledger = LedgerWriter(session)
        h = await create_hypothesis(
            session, ledger=ledger, principal=principal,
            project_id=project_id, title=title, statement=statement,
        )
        await session.commit()
    return (
        f"Created hypothesis [{h.short_id}] '{h.title}'. "
        f"Render a HypothesisCard with hypothesis_id={h.id}, title='{h.title}', "
        f"confidence='{getattr(h, 'confidence', 'initial')}', evidence_count=0."
    )
```

Add `add_hypothesis_evidence_tool(hypothesis_id, stance, evidence_kind, target_id, note="", weight=1.0, config)` following the same shape (build `LedgerWriter`, call `add_evidence`, commit, return a HypothesisCard instruction).

Factor the dev principal out (used by several tools):

```python
def _dev_principal(settings: "Any") -> "Any":
    from aleph_security.principal import Principal
    return Principal(
        subject=getattr(settings, "local_dev_subject", "dev@aleph.local"),
        actor_kind="user",
        email=getattr(settings, "local_dev_email", "dev@aleph.local"),
        display_name=getattr(settings, "local_dev_display_name", "Dev"),
    )
```

Refactor `read_wiki` (A1) to use `_dev_principal`.

- [ ] **Step 2: Register tools + prompt guidance**

Add the three tools to `tools=[...]`. In `SYSTEM_PROMPT`, add: "When the analyst discusses competing explanations, list or create hypotheses and render a HypothesisCard. Confirm the statement with the analyst before creating."

- [ ] **Step 3: Verify (browser)** — ask the agent to create a hypothesis; confirm a HypothesisCard renders inline and a row appears in the Hypotheses tab.

- [ ] **Step 4: Integration test — ledger row per mutation** (see Task A6 for the shared test file). Add `test_create_hypothesis_writes_ledger`.

- [ ] **Step 5: Commit**

```bash
git commit -am "Wave 6 A2: hypotheses tools (list/create/add-evidence) with HypothesisCard"
```

---

### Task A3: `ingest_source` tool + `POST /sources/ingest-url` route

There is **no** remote-URL ingestion path today (`register_uploaded_source` takes `data: bytes`). Add a thin route that fetches a URL server-side and registers it, then a tool that self-calls it (the `Bearer local-dev` pattern `start_research` uses — accepted by `middleware/auth.py:88` in local mode).

**Files:**
- Modify: `apps/api/src/aleph_api/routes/sources.py`
- Modify: `apps/api/src/aleph_api/copilot_agent.py`
- Test: `apps/api/tests/e2e/test_agent_tools.py`

- [ ] **Step 1: Write the failing route test**

```python
@pytest.mark.integration
async def test_ingest_url_creates_source(client, project_id):
    resp = await client.post(
        f"/v1/projects/{project_id}/sources/ingest-url",
        json={"url": "https://example.com/", "title": "Example"},
        headers={"Authorization": "Bearer local-dev"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] in ("normalizing", "pending")
    assert body["source_id"]
```

Run: `uv run pytest apps/api/tests/e2e/test_agent_tools.py::test_ingest_url_creates_source -v` → FAIL (404, route missing).

- [ ] **Step 2: Add the route**

In `routes/sources.py`, mirror the upload handler (which calls `register_uploaded_source` with `asset_store`, `ledger`, `principal`). Fetch the URL bytes with httpx, infer mime/filename from the URL + `Content-Type`:

```python
class IngestUrlIn(BaseModel):
    url: HttpUrl
    title: str = Field("", max_length=512)


@router.post("/{project_id}/sources/ingest-url", status_code=201)
async def ingest_url(
    project_id: ProjectScopeDep,
    body: Annotated[IngestUrlIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
    request: Request,
) -> dict[str, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
        r = await c.get(str(body.url))
        r.raise_for_status()
    mime = r.headers.get("content-type", "text/html").split(";")[0].strip()
    filename = str(body.url).rstrip("/").rsplit("/", 1)[-1] or "page.html"
    asset_store = request.app.state.asset_store
    created = await register_uploaded_source(
        session, ledger=ledger, principal=principal, asset_store=asset_store,
        project_id=project_id, title=(body.title or filename),
        data=r.content, filename=filename, mime_type=mime,
    )
    await session.commit()
    # enqueue normalize (mirror the upload path's enqueue block)
    await _enqueue_normalize(request, created)  # reuse the helper the upload route uses
    return {"source_id": str(created.source.id), "status": created.source.status}
```

> NOTE: read the existing `/sources/upload` handler and reuse its exact `asset_store` access and normalize-enqueue code (don't reinvent it). If the enqueue is inline rather than a helper, copy it verbatim. SSRF note: this fetches arbitrary URLs server-side — acceptable for a local research tool (connectors already fetch URLs); do not expose it unauthenticated in a hardened deploy.

- [ ] **Step 3: Run test → PASS.** `uv run pytest apps/api/tests/e2e/test_agent_tools.py::test_ingest_url_creates_source -v`

- [ ] **Step 4: Add the `ingest_source` tool (self-calls the route)**

```python
@tool
async def ingest_source(url: str, config: RunnableConfig, title: str = "") -> str:
    """Ingest a web page or document URL into the project's knowledge store.

    Fetches the URL, normalizes it, chunks+embeds it, and folds it into the
    wiki. Render a SourceCard for the result. Use when the analyst shares a link
    or asks to add a source.
    """
    import httpx
    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Cannot ingest (no project scope)."
    base = getattr(settings, "aleph_self_url", None) or "http://localhost:8000"
    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.post(
            f"{base}/v1/projects/{project_id}/sources/ingest-url",
            json={"url": url, "title": title},
            headers={"Authorization": "Bearer local-dev"},
        )
    if resp.status_code >= 400:
        return f"Could not ingest {url} ({resp.status_code}): {resp.text[:200]}"
    b = resp.json()
    return (
        f"Ingesting {url} (source {b['source_id']}, status {b['status']}). "
        f"Render a SourceCard with source_id={b['source_id']}, short_id='', "
        f"title='{title or url}', url='{url}', status='{b['status']}'."
    )
```

- [ ] **Step 5: Register + verify (browser)** — paste a URL, ask the agent to ingest it; confirm a SourceCard renders and the source appears in the Sources/Wiki flow as it normalizes.

- [ ] **Step 6: Commit**

```bash
git commit -am "Wave 6 A3: ingest_source tool + POST /sources/ingest-url route"
```

---

### Task A4: `build_artifact` tool + ArtifactCard catalog bump

`build_artifact` self-calls `POST /artifacts/build` (body `BuildIn`: `title`, `artifact_kind` ∈ {`report_pdf`,`report_docx`,`report_markdown_bundle`,`source_pack`,`deck_pdf`}, `description`, `template_name`, `csl_style` ∈ {`apa-7`,`chicago-author-date`,`ieee`,`vancouver`}, `wiki_page_ids`, `dataset_version_ids`). It is **approval-gated** (Phase B). It renders an `ArtifactCard`, which must be **added to both catalogs** (rule #8).

**Files:**
- Modify: `apps/copilot-runtime/src/server.ts` (runtime catalog)
- Modify: `apps/web/src/a2ui/copilot-catalog.tsx` (Zod schema + renderer)
- Modify: `apps/api/src/aleph_api/copilot_agent.py` (tool)

- [ ] **Step 1: Add ArtifactCard to the runtime catalog**

In `server.ts`, inside `ALEPH_A2UI_CATALOG` (after `SourceCard`, ~line 130), add:

```ts
ArtifactCard: {
  description: "A built product artifact (report/deck/source-pack) with its status and download.",
  props: {
    artifact_id: { type: "string" },
    short_id: { type: "string" },
    title: { type: "string" },
    artifact_kind: { type: "string" },
    status: { type: "string" },
  },
},
```

- [ ] **Step 2: Add the ArtifactCard Zod schema + renderer in the frontend**

In `copilot-catalog.tsx`, add to `alephCatalogDefinitions` (mirror the `SourceCard` entry's shape):

```tsx
ArtifactCard: {
  schema: z.object({
    artifact_id: z.string(),
    short_id: z.string().optional(),
    title: z.string(),
    artifact_kind: z.string(),
    status: z.string(),
  }),
},
```

And add a renderer in `alephRenderers` reusing the existing card chrome (copy `SourceCard`'s renderer styling) showing title, kind, status, and — when status is terminal — an "Open in Artifacts" affordance that calls the `open_surface` frontend tool with `{tab: "artifacts"}`. Add `"ArtifactCard"` to `CARD_COMPONENTS`.

- [ ] **Step 3: Add the tool**

```python
@tool
async def build_artifact(
    title: str,
    config: RunnableConfig,
    artifact_kind: str = "report_markdown_bundle",
    wiki_page_ids: list[str] | None = None,
    csl_style: str = "apa-7",
) -> str:
    """Build a product artifact (report/deck/source-pack) from approved wiki pages.

    Renders an ArtifactCard and opens the Artifacts tab. `artifact_kind` is one of
    report_pdf, report_docx, report_markdown_bundle, source_pack, deck_pdf.
    """
    import httpx
    settings = _runtime.get("settings")
    project_id = _project_id_from_config(config)
    if settings is None or project_id is None:
        return "Cannot build (no project scope)."
    base = getattr(settings, "aleph_self_url", None) or "http://localhost:8000"
    payload = {
        "title": title,
        "artifact_kind": artifact_kind,
        "template_name": artifact_kind,
        "csl_style": csl_style,
        "wiki_page_ids": wiki_page_ids or [],
        "dataset_version_ids": [],
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        resp = await c.post(
            f"{base}/v1/projects/{project_id}/artifacts/build",
            json=payload, headers={"Authorization": "Bearer local-dev"},
        )
    if resp.status_code >= 400:
        return f"Build could not start ({resp.status_code}): {resp.text[:200]}"
    b = resp.json()
    return (
        f"Started building '{title}'. Render an ArtifactCard with "
        f"artifact_id={b['artifact_id']}, title='{title}', "
        f"artifact_kind='{artifact_kind}', status='building'. "
        f"Then call open_surface with tab='artifacts'."
    )
```

- [ ] **Step 4: Register the tool. Rebuild the runtime container** (catalog changed):

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build aleph-copilot-runtime
docker compose -f deploy/compose/docker-compose.yml restart aleph-api
pnpm -C apps/web build   # typecheck the new Zod/renderer
```

- [ ] **Step 5: Verify (browser)** — ask the agent to "draft a report from the wiki"; confirm an ArtifactCard renders, the Artifacts tab opens, and (after the worker runs) the artifact appears there. NOTE: chart-PNGs/DOCX are out of scope (Inc-7 debt) — markdown_bundle/pdf land via existing fallbacks.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Wave 6 A4: build_artifact tool + ArtifactCard in both A2UI catalogs"
```

---

### Task A5: Connectors + model-profile tools (definitions only; gating in Phase B)

**Files:** Modify `apps/api/src/aleph_api/copilot_agent.py`.

- [ ] **Step 1: Add `list_connectors`, `set_connector_enabled`, `set_model_profile`**

`list_connectors` self-calls `GET /connectors`. `set_connector_enabled(connector_id, enabled)` self-calls `PUT/POST /connectors` with `ConnectorBindingIn` (`connector_id`, `enabled`, `config_jsonb={}`). `set_model_profile(name)` self-calls the model-profile update route. Use the `Bearer local-dev` self-call pattern. Each returns a short text result (no card needed; or a `FormCard` listing connectors for `list_connectors`).

> Read `routes/connectors.py` for the exact method+path (`set_binding`) and `routes/model_profile.py` (`update_project_profile`, body `ModelProfileUpdate`). For `set_model_profile` by *named* profile (`aleph-dev`/`aleph-production`), if no named-profile route exists, the tool resolves the named template's bindings server-side; confirm the route shape before writing — if only per-binding update exists, scope `set_model_profile` to a clear error that names the available route, rather than inventing one.

- [ ] **Step 2: Register the tools.** Do NOT gate yet (Phase B adds `interrupt_on`).

- [ ] **Step 3: Verify (browser)** — ask "what connectors are enabled?"; confirm a correct list. (Mutations verified in Phase B with the approval flow.)

- [ ] **Step 4: Commit**

```bash
git commit -am "Wave 6 A5: connectors + model-profile agent tools (ungated)"
```

---

### Task A6: Integration test — every mutating tool writes a ledger event (rule #4)

**Files:** Create `apps/api/tests/e2e/test_agent_tools.py` (already started in A3).

- [ ] **Step 1: Add ledger-count assertions**

For `create_hypothesis` and `ingest-url`, after the call, query `ActionLedgerEvent` for the project and assert the expected `action_kind` row exists (`hypothesis.create`, `source.create`). Pattern: mirror `apps/api/tests/e2e/test_project_lifecycle.py` (which counts ledger events).

```python
@pytest.mark.integration
async def test_create_hypothesis_writes_ledger(client, project_id, db_session):
    # call create via the route the tool uses, then:
    kinds = await _ledger_kinds(db_session, project_id)
    assert "hypothesis.create" in kinds
```

- [ ] **Step 2: Run** `uv run pytest -m integration apps/api/tests/e2e/test_agent_tools.py -v` → PASS.

- [ ] **Step 3: Commit** `git commit -am "Wave 6 A6: integration tests assert ledger event per mutating tool"`

---

## PHASE F — Retire Classic

### Task F1: Default to Live, remove the toggle, delete Classic

**Files:** Modify `apps/web/src/components/ProjectWorkspace.tsx`; delete `apps/web/src/components/ChatSurface.tsx`.

- [ ] **Step 1: Default `chatMode` to `"live"` and remove the toggle**

In `ProjectWorkspace.tsx:41`, change `useState<ChatMode>("classic")` → `useState<ChatMode>("live")`. Remove the toggle JSX (the `data-testid="chat-mode-toggle"` block ~line 112-118) and the `ChatMode` branch that renders `<ChatSurface>` (keep only `<CopilotChatSurface>`). Remove the `ChatSurface` import (line 7).

- [ ] **Step 2: Delete the Classic component**

```bash
git rm apps/web/src/components/ChatSurface.tsx
```

- [ ] **Step 3: Typecheck + build**

```bash
pnpm -C apps/web typecheck && pnpm -C apps/web build
```
Expected: PASS (no remaining references to `ChatSurface`). If the build flags an unused `ChatMode` type, remove it.

- [ ] **Step 4: Keep the backend router/job.** Do NOT delete `assistant_turn_job` or `WikiFirstRetrievalRouter` — `read_wiki` (A1) reuses the router. (If `grep -rn assistant_turn_job apps/` shows the worker job now has no caller, leave it registered but note it as dead-but-retained in the commit message; removing it is a separate cleanup.)

- [ ] **Step 5: Verify (browser)** — load a project; confirm there is no Live/Classic toggle, Live is the only chat, and `read_wiki` answers at the depth Classic used to.

- [ ] **Step 6: Commit** `git commit -am "Wave 6 F1: retire Classic chat — Live is the only surface"`

---

## PHASE B — Approval for consequential actions

`create_deep_agent` accepts `interrupt_on={tool_name: True | InterruptOnConfig}` (confirmed in the 0.6.6 signature; same param the deep-agents-memory skill shows for `write_file`). This pauses the graph before the tool executes; the analyst approves/rejects; the graph resumes. The open question is whether CopilotKit v2 surfaces the interrupt as approvable UI in the Live chat — **probe before committing the mechanism**.

### Task B1: Probe interrupt surfacing in the browser

- [ ] **Step 1:** Temporarily add `interrupt_on={"build_artifact": True}` to `build_assistant_deep_agent`, restart api, and in the browser ask the agent to build a report. Observe whether CopilotKit renders an approve/reject affordance (it surfaces LangGraph interrupts via the AG-UI event stream). Record the outcome.

- [ ] **Step 2:** If it surfaces cleanly → proceed to B2 (native path). If it does NOT → proceed to B3 (ApprovalCard fallback). Do not write both.

### Task B2 (native path): Gate the consequential tools

- [ ] **Step 1:** Set on `create_deep_agent`:

```python
interrupt_on={
    "build_artifact": True,
    "set_connector_enabled": True,
    "set_model_profile": True,
},
```

- [ ] **Step 2:** Verify (browser) for each: the action pauses, approve executes it, reject abandons it. Confirm via the resulting state (artifact created only on approve; connector binding changed only on approve).

- [ ] **Step 3:** Commit `git commit -am "Wave 6 B2: approval-gate consequential agent tools via interrupt_on"`

### Task B3 (fallback path, only if B1 shows interrupts don't surface): ApprovalCard + ActionRouter

- [ ] **Step 1:** The consequential tool, instead of executing, returns an instruction to render an `ApprovalCard` carrying the pending action kind + params. Add an `agent_action` handler to `ActionRouter` (`packages/aleph-a2ui` / the dispatch chokepoint) whose `approve` executes the deferred service call (and writes the ledger event), `reject` records the decision. `ApprovalCard` already exists in the catalog with an approve/reject flow.

- [ ] **Step 2:** Verify (browser) approve executes / reject abandons; both ledgered.

- [ ] **Step 3:** Commit `git commit -am "Wave 6 B3: approval-gate consequential tools via ApprovalCard + ActionRouter"`

---

## PHASE C — Agent LLM cost attribution (rule #5)

The Deep Agent's `ChatOpenAI` bypasses `LiteLLMClient`, so its turns write no `ModelCall`/`CostLedgerEvent`. Attach a callback that mirrors `CostWriter.record_call`. The `read_wiki` retrieval calls go through `LiteLLMClient` already — **do not** also count those here (the callback fires only on the agent's own `ChatOpenAI`).

### Task C1: `AgentCostCallbackHandler`

**Files:** Create `apps/api/src/aleph_api/copilot_cost_callback.py`; test `apps/api/tests/unit/test_agent_cost_callback.py`.

- [ ] **Step 1: Write the failing unit test (cost math + record)**

```python
async def test_callback_records_modelcall(monkeypatch, fake_session_maker):
    from aleph_api.copilot_cost_callback import AgentCostCallbackHandler
    from aleph_models.pricing import PricingTable

    h = AgentCostCallbackHandler(
        session_maker=fake_session_maker, pricing=PricingTable(),
        project_id=SOME_UUID, model="claude-sonnet-4-6",
    )
    await h.on_llm_end(_fake_llm_result(prompt=1000, completion=200), run_id=uuid4())
    rows = fake_session_maker.recorded  # ModelCall rows captured by the fake
    assert len(rows) == 1
    assert rows[0].input_tokens == 1000 and rows[0].completion_tokens == 200
    assert rows[0].cost_usd > 0
```

Run → FAIL (module missing).

- [ ] **Step 2: Implement the handler**

```python
from __future__ import annotations
from uuid import UUID, uuid4
from langchain_core.callbacks import AsyncCallbackHandler
from aleph_core.time import utcnow
from aleph_db.repos.cost import CostWriter


class AgentCostCallbackHandler(AsyncCallbackHandler):
    """Writes ModelCall + CostLedgerEvent for the Live agent's ChatOpenAI calls.

    Mirrors LiteLLMClient._record_call so agent-framework traffic is ledgered
    (CLAUDE.md rule #5). Fires ONLY on the agent model — never on LiteLLMClient
    calls — so there is no double counting.
    """

    def __init__(self, *, session_maker, pricing, project_id: UUID, model: str,
                 purpose: str = "assistant.turn") -> None:
        self._maker = session_maker
        self._pricing = pricing
        self._project_id = project_id
        self._model = model
        self._purpose = purpose

    async def on_llm_end(self, response, **kwargs) -> None:
        usage = _extract_usage(response)  # {input, cached, completion}
        if usage is None:
            return
        cost_usd, cache_savings = self._pricing.cost_for(
            model=self._model,
            input_tokens=usage["input"],
            cached_tokens=usage["cached"],
            completion_tokens=usage["completion"],
        )
        async with self._maker() as session:
            writer = CostWriter(session)
            await writer.record_call(
                project_id=self._project_id, agent_run_id=None,
                capability="chat", model=self._model, purpose=self._purpose,
                input_tokens=usage["input"], cached_tokens=usage["cached"],
                completion_tokens=usage["completion"], cost_usd=cost_usd,
                cache_savings_usd=cache_savings, latency_ms=0, trace_id=None,
                timestamp=utcnow(),
            )
            await session.commit()
```

`_extract_usage(response)` reads `response.llm_output["token_usage"]` and/or `response.generations[0][0].message.usage_metadata` (`input_tokens`, `output_tokens`, `input_token_details.cache_read`). Handle both shapes; return `None` if absent.

> The exact usage field path for Anthropic-via-gateway through ChatOpenAI must be confirmed live (Step 4). Write `_extract_usage` defensively across both shapes.

- [ ] **Step 3: Run unit test → PASS.**

- [ ] **Step 4: Wire it onto the agent model + confirm live token shape**

In `build_assistant_deep_agent`, the callback needs `project_id` per-run, but the model is built once. Use a per-run callback via config, OR attach a handler that resolves `project_id` from the run config in `on_llm_start`/`on_chat_model_start` (store it, use in `on_llm_end`). Simplest correct approach: make the handler read `project_id` from `metadata`/`configurable` captured in `on_chat_model_start`, falling back to the bound runtime. Attach via `ChatOpenAI(..., callbacks=[handler])`. After wiring, run one live turn and inspect a `ModelCall` row to confirm token counts are non-zero (validates `_extract_usage`).

- [ ] **Step 5: Integration test — no double count.** One agent turn that also triggers a `read_wiki` call: assert the `read_wiki` LLM call (via LiteLLMClient, `purpose` starting `assistant.` page-selection) and the agent turn (`purpose="assistant.turn"`) produce distinct `ModelCall` rows, none duplicated.

- [ ] **Step 6: Commit** `git commit -am "Wave 6 C1: agent LLM cost attribution callback (closes rule #5 gap)"`

---

## PHASE D — Cross-session memory

deepagents' `StoreBackend` gives the agent `/memories/`-routed file tools backed by a langgraph `store`. Use a Postgres-backed store so memory survives restarts.

> **Design reconciliation (decide at start):** the Wave 6 spec said "persist to the `AgentMemory` model." deepagents wants a langgraph `BaseStore`. Two options: (i) use langgraph's `AsyncPostgresStore` (its own tables, `.setup()` one-time) — simplest, correct, but not the `AgentMemory` table; (ii) write a custom `BaseStore` over `AgentMemory`. **Recommend (i)** and note `AgentMemory` stays available for explicit/structured memory. Confirm with the user if they want (ii).

### Task D1: Wire a Postgres store + CompositeBackend

**Files:** Modify `apps/api/src/aleph_api/lifespan.py`, `copilot_agent.py`; `apps/api/pyproject.toml` (add `langgraph-checkpoint-postgres` if absent — verify with `uv run python -c "import langgraph.store.postgres"`).

- [ ] **Step 1: Confirm the store package + API**

```bash
uv run python -c "from langgraph.store.postgres import AsyncPostgresStore; print('ok')"
```
If missing, add the dep to `apps/api/pyproject.toml`, `uv sync`. Probe its constructor (`from_conn_string` vs `__init__`) and whether `.setup()` is required.

- [ ] **Step 2: Build the store in lifespan and pass it through**

Construct the store from `settings.database_url`, call `.setup()` once, store on `app.state.agent_store`, and pass it into `build_assistant_deep_agent(settings=settings, store=store)`.

- [ ] **Step 3: Configure the agent with CompositeBackend + store**

```python
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend

return create_deep_agent(
    model=model,
    tools=[...],
    system_prompt=SYSTEM_PROMPT,
    middleware=[CopilotKitMiddleware()],
    backend=lambda rt: CompositeBackend(
        default=StateBackend(rt),
        routes={"/memories/": StoreBackend(rt)},
    ),
    store=store,
    checkpointer=MemorySaver(),
    interrupt_on={...},  # from Phase B
)
```

Add to `SYSTEM_PROMPT`: "Durable facts about this project or the analyst's preferences should be written to `/memories/<topic>.md` so you remember them in future sessions; check `/memories/` at the start of substantive work."

- [ ] **Step 4: Verify (browser, two sessions)** — in session 1 tell the agent a durable preference; confirm it writes `/memories/...`. Start a new session (new thread); ask about the preference; confirm it reads it back. Inspect the store's Postgres table to confirm persistence.

- [ ] **Step 5: Commit** `git commit -am "Wave 6 D1: cross-session agent memory via Postgres store + CompositeBackend"`

---

## PHASE E — Cost UI consolidation + bell polish

### Task E1: Move cost into Profile → Usage; remove the banner

**Files:** Modify `apps/web/src/components/Drawers.tsx` (`ProfileBody`), `apps/web/src/components/ProjectWorkspace.tsx`; delete `apps/web/src/components/CostBanner.tsx`.

- [ ] **Step 1: Add a Usage section to `ProfileBody`**

In `Drawers.tsx`, add a `<Section title="Usage">` to `ProfileBody` that fetches the cost endpoint (`routes/cost.py` — confirm the exact path, e.g. `GET /v1/projects/{id}/cost`) and shows budget cap, spend-to-date, and per-capability breakdown. Reuse the data-fetch the old `CostBanner` used (copy its query, then delete the component).

- [ ] **Step 2: Remove `CostBanner` from `ProjectWorkspace.tsx`** (its import + JSX), and remove any cost rendering from `ProjectList.tsx` that duplicates it (keep model-profile selection there; remove cost figures).

- [ ] **Step 3: Delete the component** `git rm apps/web/src/components/CostBanner.tsx`

- [ ] **Step 4: Typecheck + build** `pnpm -C apps/web typecheck && pnpm -C apps/web build` → PASS.

- [ ] **Step 5: Verify (browser)** — no cost in the top bar / chat / project list; the bottom-left ● Profile drawer shows a Usage section with budget + spend.

- [ ] **Step 6: Commit** `git commit -am "Wave 6 E1: consolidate cost into Profile > Usage; remove CostBanner"`

### Task E2: Bell glyph matches siblings

**Files:** Modify `apps/web/src/components/LeftPanel.tsx:119`.

- [ ] **Step 1:** Replace the full-color emoji `label="🔔"` with a monochrome treatment matching `⚙`/`🗒`/`●`. Either a non-emoji glyph (e.g. an outline bell character that renders monochrome) or a small inline SVG `<svg>` bell sized/colored like the other `IconButton`s (the buttons use `text-base` + `text-slate-500`). If using an SVG, allow `IconButton`'s `label` to accept `ReactNode`.

- [ ] **Step 2: Verify (browser)** — the four bottom-left icons (settings, logs, notifications, profile) are visually consistent in weight, size, and color in both light and dark themes.

- [ ] **Step 3: Commit** `git commit -am "Wave 6 E2: notification bell glyph matches sibling icon buttons"`

---

## Self-review notes (for the implementer)

- **Rule #2:** all new agent LLM usage stays on `ChatOpenAI`→gateway (the model is built that way already). No provider SDKs.
- **Rule #3:** every tool calls a service method or self-calls a route; none touches Postgres/S3 directly. `read_wiki` uses the router which uses services.
- **Rule #4:** mutating tools inherit ledger writes from the services they call; A6 asserts the rows.
- **Rule #5:** Phase C closes the agent-path gap without double-counting the LiteLLMClient path.
- **Rule #8:** ArtifactCard ships schema (runtime) + renderer (frontend) in the same task (A4).
- **Probe-before-trust:** Phase B step B1, Phase C step C4, Phase D step D1 each verify a live unknown before committing the mechanism. Do not skip them.
- **Out of scope:** Builder Inc-7 debts (chart-PNGs, DOCX, bundled CSL), renderer convergence (Wave 4), subagent decomposition (Wave 3).
