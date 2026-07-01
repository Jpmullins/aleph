# Aleph — Intent Reconstruction

*Audit date: 2026-07-01 · Branch: `audit-remediation` · Method: code read + 3 parallel
subsystem traces + live probes against the running compose stack (api/web/copilot-runtime/
aiq/postgres/minio/redis all up).*

This document reconstructs (1) what Aleph is **supposed** to do, (2) what it **actually** does
as observed from code and a live stack, and (3) where the **gaps** are. Companion machine-checkable
claims are in `audit/claims.yaml`; the executable harness is in `audit/checks/` + `audit/run.sh`.

---

## 1. What this app is supposed to do (intended product)

Aleph is a **multi-agent research environment**. An analyst opens a *project* (a research
question), and a fleet of LLM agents builds and maintains a **compiled wiki** as the primary
knowledge surface, drawing on ingested **sources** and external **web research** (via an NVIDIA
AIQ subsystem). The analyst works almost entirely **conversationally** through a single Live
chat agent, which plans and delegates to purpose-built subagents; the agent can drive a 3-panel
workspace UI. Everything is **provenance-first**: every mutation is hash-chain-ledgered, every
LLM call is cost-tracked, every row is project-scoped.

Intended user-facing capabilities:

- **Projects**: list / create / open / soft-delete a project. Creating one **auto-bootstraps**
  a wiki (an overview page + seed-topic research) from the title + description.
- **3-panel workspace**: left rail (sessions, drawers), center Live chat + Activity card,
  right panel of 5 A2UI surface tabs (Wiki / Library / Notes / Hypotheses / Briefs).
- **Live conversational agent**: streamed responses over AG-UI; a Deep-Agents orchestrator that
  plans via `write_todos` and delegates to 6 subagents (retriever, researcher, wiki_builder,
  viz_builder, analyst, reviewer). The agent can render inline A2UI cards and steer the right panel.
- **Human-in-the-loop**: agent-proposed actions (build artifact, toggle connector) surface as
  **ApprovalCards** the analyst approves/rejects, routed through an ActionRouter with ledger audit.
- **Wiki**: browse/read pages, wikilink navigation (resolved vs broken-link states), draft
  **approve/reject** workflow, **repair-broken-links** action, wiki-first retrieval.
- **Library**: view ingested **sources** (raw asset + normalized text) alongside built **artifacts**.
- **Source ingestion pipeline**: upload a file (or ingest a URL) → normalize → chunk+embed →
  wiki-ingest, all as background workers, surfaced live in the Activity card.
- **Notes**: create/edit a notebook, **promote** a note to a draft wiki page + Briefs proposal.
- **Hypotheses**: create hypotheses, attach evidence, view an **ACH matrix** (analysis of
  competing hypotheses).
- **Briefs**: pending synthesis proposals (from AIQ research or note-promotion), page-merge
  proposals, and open review findings render as approval/finding cards for the analyst to action.
- **AIQ research → wiki**: dispatch deep/shallow web research to `aiq-server`; poll for the
  report; run a synthesis workflow that commits a **draft wiki page + pending proposal**; a
  curator repairs wikilinks.
- **Artifacts**: build report_pdf / docx / markdown bundle / source pack / deck from wiki pages
  + datasets; download the rendered asset.
- **Visualizations**: chart (Vega-Lite), table, graph (SVG), and map (MapLibre) cards.
- **Cross-cutting**: cost rollup (Profile drawer), action ledger + hash-chain verify (Logs
  drawer), agent-run notifications, realtime SSE push (Live Wiki), model-profile switching.

Authoritative spec: `docs/superpowers/specs/2026-05-26-aleph-design.md` + per-increment/wave specs.
The load-bearing invariants (wiki-first retrieval, single LiteLLM transport, agent→service-only
writes, ledger-per-mutation, cost-per-call, project-scoped rows, declarative A2UI) are stated in
`CLAUDE.md`.

---

## 2. What it actually does (observed from code + live stack)

The headline finding: **this branch is far more wired than a docs-only read would suggest.**
`audit-remediation` has closed most of the gaps recorded in the older `docs/system-assessment.md`
and the project memory (e.g. the "wikilink repair not on the synthesis path" gap). Live probes
confirm real data end-to-end.

### Backbone (verified live)
- **API** mounts ~33 routers (`apps/api/src/aleph_api/main.py:77-116`). `/healthz`, `/readyz`
  (postgres+redis+minio+litellm all `ok`), `/v1/me` (JIT `dev@aleph.local` principal),
  `/v1/projects` all respond with real data.
- **Two real projects exist** ("Sovereign AI", "AI Distillation") with committed wiki pages,
  cost ledgers, agent runs, and synthesis proposals — i.e. the pipelines have actually run.
- **Client-side router** (`apps/web/src/App.tsx`): 4 states — projects list, workspace,
  login, OIDC callback. No react-router; `pushState` + `popstate`. Only reachable route into a
  workspace is `/projects/:id`.

### Conversational agent (traced FULLY WIRED end-to-end)
- Browser `CopilotChat agentId="assistant"` (`CopilotChatSurface.tsx:96`) → Node CopilotRuntime
  `http://localhost:4000/api/copilotkit` (`copilot.tsx:29`) → API AG-UI endpoint
  `/copilotkit/agent/assistant`, mounted in **lifespan** (`lifespan.py:170`,
  `copilotkit_endpoint.py:25`) not `main.py`.
- Live probe: the runtime `/info` reports `agents.assistant` present and `a2uiEnabled:true`;
  the API endpoint returns 405 on GET / 422 on empty POST (i.e. it exists and validates AG-UI input).
- Orchestrator (`copilot_agent.py:1080` `build_assistant_deep_agent`) is a real `deepagents`
  agent with 5 orchestrator tools + **6 subagents**, each wrapping **real service calls** that
  write ledger events (retrieval, research dispatch, source ingest, note promote, artifact build,
  hypothesis create/evidence, editorial review). No stubs found in the tool implementations.
- ApprovalCard → `POST /v1/projects/{id}/cards/actions` → `ActionRouter.dispatch`
  (`a2ui_handlers.py`) → real endpoint execution + `approval_request.approved` ledger event.

### Wiki + retrieval (verified live)
- `POST /retrieval/debug` returns a real composed answer for "What is sovereign AI?" — the
  **wiki-first router** (WikiIndex page-selector → load pages → compose) works.
- Wiki pages list/read, approve/reject, and `repair-links` endpoints are present and called from
  the Wiki surface. Wikilink resolved/broken-chip rendering exists (`WikilinkChip.tsx`,
  spec `10-wiki-links-*`).

### Source → wiki pipeline + workers (traced FULLY WIRED)
- `arq.py:107-120` registers **12 jobs**; enqueue call-sites verified for 11:
  `normalize_job`→`{chunk_embed_job, wiki_ingest_job}`→`{mechanical_review, curate_page}`, plus
  `aiq_submit`, `aiq_synthesis_poll`, `bootstrap_project`, `builder`, `editorial_review`,
  `reembed`, `curate_page`. Upload (`POST /sources/upload`) and URL-ingest both kick off normalize.

### AIQ research → wiki (traced FULLY WIRED; artifacts present live)
- `POST /synthesize` → `dispatch_research` → AIQ `POST /v1/jobs/async/submit` (base URL
  configurable, default `http://aiq-server:8000`) → `aiq_synthesis_poll_job` polls →
  `SynthesisWorkflow` (5-node LangGraph) commits a **draft page + pending SynthesisProposal** →
  `curate_page_job` runs `CuratorService` which calls `AliasService.repair_broken_links`.
- Live proof: both projects have `synthesis-proposals` rows (all `approved`), and Sovereign AI's
  cost ledger shows `bootstrap.scope` + `wiki.curate.overview` phases → **bootstrap-on-create
  actually ran** and produced an approved overview page.

### Surfaces + realtime (traced FULLY WIRED, with a nuance)
- All 5 tabs stream `GET /surfaces/{tab}/stream` (SSE). **Wiki** and **Briefs** carry real
  server-built data models (pages/claims; pending proposals/merge-proposals/findings). **Library,
  Notes, Hypotheses** stream a *structural* surface only and self-fetch their data client-side via
  react-query (`NotesSurface.tsx`, `HypothesesSurface.tsx` are fully interactive: create/edit/
  promote note; +New hypothesis modal).
- Realtime push: one supervised Postgres `LISTEN/NOTIFY` connection (`realtime.py`) fed by
  `AFTER INSERT/UPDATE` triggers (`realtime_notify_triggers` migration) fans out to the SSE
  streams; each stream keeps a poll fallback. Frontend `useWikiLiveSignals` consumes `changes/stream`.

### Cross-cutting (verified live)
- **Cost**: `/cost` returns real spend (`spent_usd: 0.0349`, per-phase + per-model breakdowns).
- **Ledger**: `/ledger/verify` returns `{ok:true, count:36}` — the hash chain is intact.
- **Model profile switch**, **note CRUD round-trip**, **hypothesis create**, **smoke/llm**
  (200) all verified with live write probes on a throwaway project (ledger stayed consistent).

---

## 3. Gaps, unwired pieces, dead code, and mistakes

Ordered roughly by significance. These are the things a passing `npm test` would never catch.

0. **Artifact download is unwired (a real broken flow found by the harness).** The
   Library/Artifacts surface renders a **Download** button linking to
   `/v1/projects/{id}/artifacts/{aid}/versions/{ver}/download` (`ArtifactsSurface.tsx:266`),
   but **no such route exists** — `apps/api/src/aleph_api/routes/artifacts.py` registers
   `GET artifacts`, `GET artifacts/{id}`, `GET artifacts/{id}/versions`, `POST artifacts/build`,
   and `GET rendered-assets`, but **no `.../versions/{ver}/download`**. The build pipeline works
   end-to-end (builder agent succeeds, artifact + version rows are created), but clicking Download
   always 404s. This is exactly a "UI entry point with no working backend" gap. *(Verified live:
   the `artifacts-build` check builds successfully, then the download returns 404.)*

1. **The existing e2e viz suite is stale against the streaming architecture.** `tests/playwright/
   specs/05-charts-tables-graphs.spec.ts` intercepts the **non-stream** `/surfaces/wiki` endpoint,
   but the right panel now consumes the **SSE** `/surfaces/wiki/stream` (`A2UISurfaceView.tsx:152`,
   `EventSource`). The intercept never matches, so the test fails against the live app — even though
   the renderers themselves work (the audit's `viz-renderers` check intercepts the *stream* and the
   real Vega `<canvas>` renders). A passing-looking suite name hides a broken test.

2. **Typed connector suite is orphaned (by design, but it's dead on the live path).**
   `packages/aleph-connectors` `ConnectorBase`/`ConnectorRegistry` `search`/`fetch` are **not**
   on the research path — research runs against AIQ's built-in Tavily web search. Wiring the typed
   connectors requires a custom AIQ image with NAT plugins (sequenced infra). CLAUDE.md admits this.
   *Impact: the "connectors" abstraction is a large body of code with no runtime caller.*

2. **JS/SPA render worker is specced but NOT built.** URL ingest is raw-HTTP
   (`aleph-workers` note in CLAUDE.md): JavaScript/SPA pages capture as static HTML. A
   Playwright render worker is named in the spec but has no implementation. *Impact: URL ingest
   silently under-captures dynamic pages.*

3. **`smoke_llm_job` is a registered-but-orphaned worker** (`arq.py:109`): no enqueue call-site
   found. The `POST /smoke/llm` endpoint runs its LLM check inline (returns 200), so the arq job
   is dead code. *Minor.*

4. **`Notes` / `Hypotheses` / `Library` surface streams do not push their own data.** They emit
   a structural surface once and idle; the data arrives via client-side react-query. This is a
   deliberate "self-fetching" design, but it means the **SSE-delta live-update guarantee only
   truly holds for Wiki and Briefs.** A new note created by an agent will not appear in another
   client's Notes tab until its react-query refetch interval, not "the instant an agent writes."

5. **Briefs hides approved/rejected proposals.** The Briefs surface only renders *pending*
   proposals/merge-proposals/findings. Both live projects show `badge_count:0` because every
   synthesis proposal was already `approved`. Correct by design, but there is **no UI to review
   the history** of what was approved — it only lives in the ledger/wiki.

6. **Backend endpoints with no user-facing UI entry (orphan-ish):**
   - **Evals** (`/v1/eval-datasets`, `/v1/eval-runs`, `POST` runner) — no frontend references at
     all; purely a CI gate (`aleph_evals`). `eval-datasets` returns `[]` live.
   - **Merge-proposals** (`GET /merge-proposals`, `POST` accept) — no direct frontend caller;
     data only surfaces inside the Briefs stream and is actioned via `cards/actions`.
   - **Reviews trigger** (`POST /reviews/editorial|mechanical`) — never invoked from the UI;
     only agent/worker-triggered. Findings surface in Briefs.
   - **Datasets** — no UI creates a dataset; `chart-spec` is called from a card. `datasets`
     returns `[]` live in both projects, so the dataset→chart-spec→artifact path is unexercised.
   These are agent/worker-facing by intent, not bugs, but as *user-facing capabilities* they are
   effectively **not wired to a UI entry point**.

7. **OIDC auth path is dormant.** Only `local` mode is exercised (JWT verification skipped, fixed
   `dev@aleph.local`). The `oidc` code path exists but is untested in this environment; a
   deferred SSE×OIDC gap is documented in `docs/security/auth.md`.

8. **Bootstrap-on-create is gated by a setting** (`bootstrap_auto_enabled`, `projects.py:180`).
   It is enabled in this stack (Sovereign AI bootstrapped), but a project created with the flag
   off gets an empty wiki with no signal to the user that nothing will happen.

9. **The docs oversell in places / are stale.** `docs/system-assessment.md` ("surfaces ~30%
   realized") and project memory predate this branch's remediation and now understate what works.
   Conversely, subagent traces of this branch tend to declare everything "production-ready" —
   which is why Phase 2 verifies behavior rather than trusting the read.

**Not observed:** no `TODO/FIXME/NotImplementedError` in production paths (CI greps for these);
no obvious secret-RAG shortcut; no direct provider-SDK calls bypassing the gateway on the paths read.

---

## 4. How Phase 2 verifies this

`audit/claims.yaml` turns each intended capability into a claim. `audit/checks/` holds one
executable check per claim, and `audit/run.sh` runs them all against the **live stack** and writes
`audit/scorecard.json`. Checks prefer `http`/`dataflow` (fast, deterministic round-trips against
the running API) with a focused set of `e2e` Playwright specs for genuinely UI-only behavior
(chat streaming, viz renderers, wikilink navigation, workspace shell), plus `route`/`static`
checks. A check that errors because a feature is unbuilt is recorded as a **FAIL**, not skipped —
skips are reserved for genuine infra unavailability (e.g. the web dev server or a browser binary
being absent), and are reported distinctly.
