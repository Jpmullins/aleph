# Wave 6 — Complete the conversational pivot: Live is the only surface

Status: **approved design** (not started). Author handoff doc. Read the
"References / probe-first" section before coding — pin every moving API by
probing the installed package, not memory.

## Why this wave

The conversational pivot (`memory/project_conversational_pivot.md`,
2026-05-28) set the product direction: everything — research, wiki reading,
hypotheses, ingestion, product-building, project config — should be driven
through the **Live** assistant conversationally or via dynamic A2UI cards, not
manual buttons/drawers/toggles. The Live agent today has only two tools
(`search_wiki`, `start_research`) and an in-memory checkpointer. "Classic" (the
legacy enqueue+poll chat) still ships behind a toggle that **defaults to
classic**.

This wave closes that gap: it gives the Live Deep Agent the tool surface the
pivot named, makes consequential actions safe, attributes agent LLM cost,
gives the agent cross-session memory, consolidates the cost UI into one place,
and **retires Classic** so Live is the only chat surface.

### Ground truth verified 2026-05-29 (do not re-discover)

- **Live's `search_wiki` is shallow** — it only calls
  `IndexService.select_pages` (FTS ranking → titles + summaries). It does
  **not** run the page-selector LLM, 1-hop wikilink expansion, the answer
  composer, intra-source descent, or coverage judgment.
- **Classic** (`apps/web/src/components/ChatSurface.tsx` →
  `POST /v1/projects/{id}/.../messages` → `assistant_turn_job` worker →
  `aleph_assistant.agent.workflow` → `WikiFirstRetrievalRouter`) **is** the
  deep wiki-first retrieval path (CLAUDE.md rule #1). Deleting Classic naively
  regresses answer quality — hence Phase A adds a `read_wiki` tool wrapping the
  *same* router so Live reaches parity first.
- **Builder/Artifacts is built and wired backend-side** but unreachable
  conversationally: `POST /v1/projects/{id}/artifacts/build` exists,
  `builder_job` is registered in `apps/workers/src/aleph_workers/arq.py`,
  `artifacts_surface()` + `apps/web/src/a2ui/components/ArtifactsSurface.tsx`
  render the Artifacts tab. There is **no agent tool** that triggers a build.
  Pre-existing Inc-7 debts remain (no chart-PNG render container; DOCX not
  wired; CSL styles fall back to plain author-year).
- **Cost leaks into multiple UI spots**: `CostBanner.tsx` (top bar), the
  Classic chat (`ChatSurface.tsx`), and `ProjectList.tsx`. A **Profile drawer**
  already exists (`LeftPanel.tsx` bottom-left ● button →
  `Drawers.tsx` `ProfileBody`) — the target home for a Usage section.
- **Agent LLM cost is not ledgered** (rule #5 gap): the Deep Agent's
  `ChatOpenAI` and the AIQ-path `ChatOpenAI` bypass `LiteLLMClient`, so they
  write no `ModelCall`/`CostLedgerEvent`. The `LiteLLMClient` path (wiki
  compile, reviewers, embeddings, page-selection) already writes them.

## Load-bearing rules this wave must honor

- **Rule #1** — wiki-first retrieval is the primary path; `read_wiki` wraps the
  existing router, no secret RAG shortcut.
- **Rule #2** — agent-framework code may use `ChatOpenAI` **only** against the
  Insights gateway (`base_url=LITELLM_BASE_URL`, `api_key=INSIGHTS_LITELLM_API_KEY`).
- **Rule #3** — agents call typed `aleph-api` service methods; never write
  Postgres/S3 directly. Every new tool routes through a service.
- **Rule #4** — every mutation writes an `ActionLedgerEvent` in the same
  transaction. New mutating tools inherit this from the service methods they
  call; integration tests assert the ledger row.
- **Rule #5** — every LLM call writes `ModelCall` + `CostLedgerEvent`. Phase C
  closes the agent-path gap. Must **not** double-count the `LiteLLMClient` path.
- **Rule #8** — A2UI surfaces are declarative; no agent-emitted JS/SQL. New
  cards use existing catalog components.

---

## Phase A — Agent tool suite

Extend the Deep Agent in `apps/api/src/aleph_api/copilot_agent.py`. Each tool
calls a typed service method and returns text the agent narrates; where a card
exists in the A2UI catalog, the agent is instructed to render it (the runtime's
inline "aleph" catalog already adapts the 17 cards).

`search_wiki` (fast) and `start_research` stay. New tools:

| Tool | Service / path it wraps | Card | Mutating | Approval |
|---|---|---|---|---|
| `read_wiki(query)` | `WikiFirstRetrievalRouter` (page-select → wikilink → compose → descent → coverage) | cited answer (markdown) | no | — |
| `list_hypotheses()` | `hypothesis_service` list | `HypothesisCard`(s) | no | — |
| `create_hypothesis(statement, …)` | `hypothesis_service.create` | `HypothesisCard` | yes | direct |
| `add_hypothesis_evidence(hypothesis_id, …)` | `hypothesis_service.add_evidence` | `HypothesisCard` | yes | direct |
| `ingest_source(url \| upload_ref)` | source registration → normalize→chunk→wiki | `SourceCard` (status) | yes | direct |
| `list_connectors()` | `connectors` list | text / `FormCard` | no | — |
| `set_connector_enabled(name, enabled)` | connector binding service | text | yes | **gated** |
| `set_model_profile(name)` | `model_profile` service | text | yes | **gated** |
| `build_artifact(title, kind, wiki_page_ids, …)` | `POST .../artifacts/build` | `ArtifactCard` + `open_surface→artifacts` | yes | **gated** |

Notes:
- `read_wiki` must reuse the router code path Classic uses (extract the router
  invocation so both the worker job and the tool call it; do not duplicate the
  composer logic). Project scope rides the `proj:<uuid>:<thread>` thread-id
  channel (the only channel `ag-ui-langgraph` threads into
  `config.configurable` — verified in W2).
- `build_artifact` self-calls the tested `/artifacts/build` path (mirror how
  `start_research` self-calls `/synthesize`) so connector resolution, agent
  token minting, and `builder_job` dispatch are reused, not re-implemented.
- After a successful `build_artifact`, the agent drives the right panel to the
  Artifacts tab via the existing `open_surface` frontend tool.

## Phase B — Approval model (write authority)

Cheap/reversible tools (`create_hypothesis`, `add_hypothesis_evidence`,
`ingest_source`) execute directly, ledger the action, and render a card.

Consequential tools (`set_connector_enabled`, `set_model_profile`,
`build_artifact`) **pause for analyst confirmation** before executing.

**Mechanism — verify before committing.** Preferred path:
`HumanInTheLoopMiddleware` (LangChain/LangGraph) on the consequential tools, so
the graph `interrupt()`s and CopilotKit surfaces an approve/reject prompt; on
approve the graph resumes and executes. **Probe first** that `deepagents 0.6.6`
+ `@copilotkit/react-core/v2` actually render the LangGraph interrupt as
approvable UI in the Live chat (drive it in a browser). If the interrupt does
**not** surface cleanly, fall back to: the tool emits an A2UI `ApprovalCard`
whose `approve` action routes through the existing `ActionRouter` to a new
`agent_action` handler that executes the deferred call (params carried on the
card). The spec's implementation plan will name the **verified** mechanism;
both honor rule #3 (the service method still does the write) and rule #4
(ledger event on execution).

The `langchain-middleware` skill documents `HumanInTheLoopMiddleware` and the
`Command(resume=…)` pattern against the installed version — consult it.

## Phase C — Agent cost attribution (rule #5)

A LangChain `BaseCallbackHandler` attached to the Deep Agent's `ChatOpenAI`
(and the AIQ-path `ChatOpenAI` where applicable) that, per LLM call, writes a
`ModelCall` + `CostLedgerEvent` costed via `aleph_models.pricing`
(cache-discount-aware). The handler resolves `project_id` from the run context
and tags `purpose="assistant.<tool-or-turn>"`.

**Must not double-count.** The `LiteLLMClient` path already writes these rows;
the callback fires only on the agent-framework `ChatOpenAI` traffic. Verify by
asserting the per-turn `CostLedgerEvent` count in an integration test (one row
per agent LLM call, none duplicated from a `LiteLLMClient` call inside the same
turn). This is the planned fix named in CLAUDE.md rule #5 and the
implementation log.

## Phase D — Cross-session memory

Replace the Live agent's `MemorySaver` with a `deepagents` `StoreBackend`
backed by Postgres, keyed `project × agent`, persisting to the existing
`AgentMemory` model (`aleph-core`/`aleph-db`). The agent retains context across
sessions. Consult `deep-agents-memory` (`StoreBackend`/`StateBackend`/
`CompositeBackend`) for the installed-version API. Keep the per-thread
checkpointer behavior the AG-UI runtime needs; memory (long-term) and
checkpointing (per-thread) are distinct — wire both.

## Phase E — Cost UI consolidation + bell polish

- Remove cost rendering from `CostBanner.tsx` (top bar), the Classic chat
  (removed in Phase F anyway), and `ProjectList.tsx`.
- Add a **Usage** section to `ProfileBody` in `Drawers.tsx` (reached via the
  bottom-left ● Profile button): budget cap, spend-to-date, and per-capability
  usage metrics, read from the existing cost endpoints (`routes/cost.py`).
- **Bell polish:** in `LeftPanel.tsx`, the Notifications `IconButton` uses
  `label="🔔"` (a full-color emoji) while the others (`⚙`, `🗒`, `●`) render
  monochrome. Swap the bell for a monochrome glyph/inline SVG so it matches the
  weight, size, and color treatment of the sibling `IconButton`s.

## Phase F — Retire Classic

Only after Phase A's `read_wiki` lands (so Live ≥ Classic on retrieval):
- Default `chatMode` to `"live"` in `ProjectWorkspace.tsx`.
- Remove the Live/Classic toggle and delete `apps/web/src/components/ChatSurface.tsx`.
- **Keep** `assistant_turn_job` (`apps/workers`) and the
  `WikiFirstRetrievalRouter` / `aleph_assistant.agent` code — now reused by the
  `read_wiki` tool. (If, after extraction, the worker job has no remaining
  caller, note it as dead-but-retained or remove it in the same PR — decide at
  implementation time based on whether the router is cleanly callable from the
  tool without the job wrapper.)

## Build order

**A + F** (visible parity + Classic gone) → **B** (make actions safe) →
**C + D** (cost + memory) → **E** (cost UI + bell). Each phase is one commit
with a clear message; verify each in a real browser (Playwright MCP) per the
standing per-wave requirement, not just API tests.

## Verification (per phase)

- **A:** browser — ask the agent to read the wiki deeply, list/create a
  hypothesis (HypothesisCard appears), ingest a URL (SourceCard shows status),
  build a report (ArtifactCard + Artifacts tab opens). Integration tests assert
  ledger rows for each mutating tool (rule #4).
- **B:** browser — a consequential action surfaces an approval prompt;
  approving executes, rejecting does not; both ledgered correctly.
- **C:** integration test — per-turn `ModelCall`/`CostLedgerEvent` count is
  correct and not double-counted.
- **D:** memory persists across two sessions in the same project (browser +
  store inspection).
- **E:** browser — cost no longer appears in top bar/chat/project list; Usage
  section in Profile shows budget/spend; bell visually matches siblings.
- **F:** browser — no toggle; Live is the only chat; retrieval depth matches
  pre-removal Classic on a fixture question. All prior Playwright specs pass.

## References / probe-first

- **Probe every moving API** — don't trust memory or even MCP docs.
  `uv run python -c "import deepagents; print(dir(deepagents))"` (pinned 0.6.6);
  `npm pack <pkg>@<ver>` → read `dist/*.d.mts` for CopilotKit v2 / a2ui
  renderer. This is how W2's APIs were pinned.
- **Skills (authoritative for installed versions):** `deep-agents-core`,
  `deep-agents-orchestration`, `deep-agents-memory`, `langchain-middleware`
  (HITL), `langgraph-persistence` (Store/checkpointer), `langchain-dependencies`.
- **In-repo prior art:** `apps/api/src/aleph_api/copilot_agent.py` (the W2 Deep
  Agent — extend its `tools=`/`subagents=`/`checkpointer=`); `start_research`
  (the self-call pattern `build_artifact` mirrors); `routes/artifacts.py`,
  `routes/hypotheses.py`, `routes/connectors.py`, `routes/sources.py` (the
  service methods the tools wrap); `aleph_assistant.agent.workflow` +
  `WikiFirstRetrievalRouter` (the router `read_wiki` reuses);
  `apps/web/src/a2ui/copilot-catalog.tsx` (how cards adapt to the runtime
  catalog); `Drawers.tsx`/`LeftPanel.tsx`/`CostBanner.tsx` (Phase E targets);
  `ProjectWorkspace.tsx` (Phase F toggle).
- **Local repo — `~/code/ARLIS/open-analyst`**: supervisor/subagent +
  `WriteAuthorityMiddleware` patterns if Phase B's gating grows.
- **Gotcha:** keep `langgraph >=1.2,<2` (1.0.6 broke `deepagents` prebuilt
  imports). `ag-ui-langgraph` threads scope only via thread-id, not
  forwarded_props (verified W2).

## Out of scope

- Builder Inc-7 debts: chart-PNG render container, DOCX exporter wiring, bundled
  CSL XML styles. `build_artifact` ships markdown/PDF with existing fallbacks.
- Full convergence of the chat (CopilotKit) and right-panel (homegrown)
  renderers — that's Wave 4 (A2UI v0.9), still deferred.
- Subagent decomposition of the agents (Wave 3) — not required here; tools are
  added to the existing single Deep Agent.
