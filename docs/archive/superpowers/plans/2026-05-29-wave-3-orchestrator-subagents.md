# Wave 3 — Orchestrator + purpose-built subagents — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Live assistant into a thin Deep-Agents **orchestrator** that plans (`write_todos`) and **delegates heavy work to purpose-built subagents running in isolated contexts** (retriever, researcher, wiki-builder, viz-builder, analyst, reviewer), each WRAPPING existing services — so enduring collaborative sessions stay lean, with cost attributed per subagent and the live plan surfaced in the Activity card.

**Architecture:** The assistant (`copilot_agent.py`, already `create_deep_agent`) gains `subagents=[...]` (in-process `SubAgent` TypedDicts delegated via the built-in `task` tool — each runs in its own context and returns a distilled summary) + `skills=[...]` (SKILL.md progressive disclosure). The Wave-6 cost callback is extended to subagent models. Long-running work (research, big ingest) uses the EXISTING arq background-job pattern (NOT deepagents `AsyncSubAgent`, which requires a remote hosted LangGraph graph we don't run). The orchestrator's `todos` state streams to the Activity card.

**Tech Stack:** deepagents 0.6.x (`SubAgent` TypedDict, `task` tool, `skills=`, `write_todos`), `ChatOpenAI`→gateway, arq jobs, React/CopilotKit Activity card, the Wave-4 shared catalog + SurfaceStreamer.

**Key API facts (probed 2026-05-29):**
- `create_deep_agent(..., subagents=[SubAgent], skills=[str], ...)`.
- `SubAgent` = TypedDict, required `{name, description, system_prompt}`, optional `{tools, model, middleware, interrupt_on, skills, permissions, response_format}` — **in-process**, delegated via the `task` tool, returns a distilled final message.
- `AsyncSubAgent` = `{name, graph_id, url, headers}` → **remote hosted graph (LangGraph Server)** — OUT OF SCOPE (we run in-process). Async = arq jobs.
- Context isolation/offloading/summarization are built-in harness middleware (already active in `create_deep_agent`). `write_todos` keeps a `todos` array in state.

**Build order:** T1 substrate (cost-to-subagents + a proven sync-subagent exemplar) → T2 retriever → T3 researcher (arq-async) → T4 wiki-builder → T5 viz-builder → T6 analyst + reviewer → T7 skills + orchestrator prompt → T8 todos→Activity card → T9 verification. Each task one commit. api changes need `docker compose up -d --build aleph-api`; web needs `--build aleph-web` (baked images).

---

## File Structure
- **New:** `apps/api/src/aleph_api/subagents/__init__.py` + one module per subagent (`retriever.py`, `researcher.py`, `wiki_builder.py`, `viz_builder.py`, `analyst.py`, `reviewer.py`) — each exports a `SubAgent` dict (`build_*_subagent(settings, store) -> SubAgent`) wrapping existing tools/services.
- **New:** `apps/api/src/aleph_api/skills/<name>/SKILL.md` (research, ach, report-authoring, wiki-style).
- **Modify:** `copilot_agent.py` (orchestrator: assemble `subagents=`, `skills=`, plan-and-delegate prompt; thin its own tool list), `copilot_cost_callback.py` (attach to subagent models).
- **Modify (frontend):** the Activity card to render the orchestrator's streamed `todos`.
- **Reuse (no rewrite):** `WikiFirstRetrievalRouter`, `aleph_wiki.agent` ingest workflow, `aleph_reviewer.editorial`, AIQ `/synthesize` + poller, the W4 shared catalog + SurfaceStreamer, the W6 `/memories/` store + approval flow.

---

## Task 1 — Substrate: cost callback → subagent models + proven sync-subagent exemplar

**Files:** Modify `apps/api/src/aleph_api/copilot_cost_callback.py`, `copilot_agent.py`; Test `apps/api/tests/unit/test_agent_cost_callback.py`.

- [ ] **Step 1: Probe the `task`-tool subagent mechanism in-process.** Confirm a minimal `SubAgent` works: `uv run python -c "from deepagents import create_deep_agent; from langchain_openai import ChatOpenAI; print('ok')"` and read `apps/api/node`-free — read how `build_assistant_deep_agent` currently builds the model + cost callback (`copilot_agent.py` ~lines 744-835). Record how `subagents=` is passed and confirm each `SubAgent` may carry its own `model` + `middleware`.

- [ ] **Step 2: Make the cost callback reusable per purpose.** In `copilot_cost_callback.py`, ensure `AgentCostCallbackHandler(model=..., purpose=...)` accepts a `purpose` (it already takes `purpose="assistant.turn"`). Add a factory `subagent_model(settings, name) -> ChatOpenAI` in `copilot_agent.py` that builds a gateway `ChatOpenAI` (same base_url/api_key/model as the orchestrator) with `callbacks=[AgentCostCallbackHandler(model=_AGENT_MODEL, purpose=f"assistant.subagent.{name}")]` and `stream_usage=True`. Every subagent will use `subagent_model(settings, "<name>")` as its `model` so its LLM calls are ledgered (rule #5).

- [ ] **Step 3: Unit test the subagent purpose tag.**
```python
def test_subagent_model_tags_purpose(monkeypatch):
    from aleph_api.copilot_agent import subagent_model
    from aleph_api.settings import Settings
    m = subagent_model(_fake_settings(), "retriever")
    cbs = m.callbacks or []
    assert any(getattr(c, "_purpose", None) == "assistant.subagent.retriever" for c in cbs)
```
Run `uv run pytest apps/api/tests/unit/test_agent_cost_callback.py -v` → add this test; make it pass (expose `_purpose` on the handler if not already).

- [ ] **Step 4: Add a trivial exemplar subagent + wire `subagents=`.** In `copilot_agent.py`, define an inline exemplar `SubAgent` (e.g. `{"name":"echo","description":"echoes back a distilled note for testing delegation","system_prompt":"Return a one-line confirmation.","model": subagent_model(settings,"echo")}` ) and pass `subagents=[echo]` to `create_deep_agent`. (This proves the `task`-tool delegation path end-to-end; it's removed/replaced in T2+.)

- [ ] **Step 5: Build + verify delegation works.** `uv run python -c "from aleph_api.copilot_agent import build_assistant_deep_agent; print('ok')"`; `docker compose -f deploy/compose/docker-compose.yml up -d --build aleph-api`; `/healthz` 200. (Controller browser-verifies: ask the agent to "use the echo subagent" → a `task` delegation occurs + a `model_calls` row with `purpose="assistant.subagent.echo"` appears.)

- [ ] **Step 6: Commit** `git commit -am "Wave 3 T1: subagent cost attribution + proven sync-subagent (task tool) substrate"`

---

## Task 2 — retriever subagent (the exemplar heavy subagent)

Wraps `WikiFirstRetrievalRouter` (the deep read currently inlined as `read_wiki`), so the large composed body is isolated from the orchestrator's context. This is the PATTERN every other subagent follows.

**Files:** Create `apps/api/src/aleph_api/subagents/__init__.py`, `apps/api/src/aleph_api/subagents/retriever.py`; Modify `copilot_agent.py`.

- [ ] **Step 1: Implement the retriever subagent.** In `subagents/retriever.py`:
```python
from typing import TYPE_CHECKING, Any
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

def build_retriever_subagent(*, settings: Any) -> dict:
    @tool
    async def deep_read(query: str, config: RunnableConfig) -> str:
        """Run the full wiki-first retrieval (page-select → wikilink → compose → descent) and return a cited answer."""
        # reuse the exact logic of read_wiki in copilot_agent.py: build WikiFirstRetrievalRouter
        # via _runtime session_maker + litellm + _dev_principal + project ModelProfile, call .retrieve(...)
        from aleph_api.copilot_agent import _read_wiki_impl  # extract read_wiki body into a reusable impl
        return await _read_wiki_impl(query, config)
    return {
        "name": "retriever",
        "description": "Reads the project's wiki in depth and returns a cited, composed answer. Delegate any substantive question that needs grounding in the wiki.",
        "system_prompt": "You are Aleph's retrieval specialist. Call deep_read with a focused query, then return ONLY a concise cited answer (with [[Page]] markers) and a one-line coverage note. Do NOT dump raw page bodies.",
        "tools": [deep_read],
        "model": __import__("aleph_api.copilot_agent", fromlist=["subagent_model"]).subagent_model(settings, "retriever"),
    }
```
> Extract the current `read_wiki` body in `copilot_agent.py` into `_read_wiki_impl(query, config)` so both the (now-removed) inline tool and the subagent reuse it (DRY). The retriever subagent REPLACES the inline `read_wiki` tool — remove `read_wiki` from the orchestrator `tools=[...]`; the orchestrator now delegates via `task` to `retriever`.

- [ ] **Step 2: Register + prompt.** Add `build_retriever_subagent(settings=settings)` to `subagents=[...]`. Update SYSTEM_PROMPT: "For substantive questions, delegate to the `retriever` subagent (via the task tool) rather than answering from memory; it returns a cited answer."

- [ ] **Step 3: Verify.** import check; rebuild api; controller browser-verifies a substantive question delegates to `retriever` and returns a cited answer, and the orchestrator context stays lean (the composed body isn't in the main thread). A `model_calls` row `purpose=assistant.subagent.retriever` appears.

- [ ] **Step 4: Commit** `git commit -am "Wave 3 T2: retriever subagent (isolates deep wiki reads); read_wiki delegated"`

---

## Task 3 — researcher subagent (async via the existing arq job pattern)

`AsyncSubAgent` needs a remote graph (out of scope); use the EXISTING research pipeline: the subagent kicks off `/synthesize` (which already enqueues the AIQ + poller arc) and returns immediately with a distilled "research started + will land in Briefs/Wiki" note; progress surfaces via the existing AgentEvent/Activity SSE.

**Files:** Create `apps/api/src/aleph_api/subagents/researcher.py`; Modify `copilot_agent.py`.

- [ ] **Step 1: Implement.** A `SubAgent` whose tool self-calls `POST /synthesize` (reuse the existing `start_research` body — extract `_start_research_impl`). System prompt: "Kick off research via start_research, then return a one-line confirmation naming the topic and that results will appear in the Wiki/Briefs tabs. Do not wait." Use `subagent_model(settings,"researcher")`.

- [ ] **Step 2: Register; remove the inline `start_research` tool** (now delegated). Orchestrator prompt: "To research a topic, delegate to the `researcher` subagent."

- [ ] **Step 3: Verify.** Rebuild; controller verifies "research X" delegates to `researcher`, returns immediately, and the research lands (Activity + Wiki/Briefs) via the existing pipeline. Commit `git commit -am "Wave 3 T3: researcher subagent (delegates the arq research arc)"`

---

## Task 4 — wiki-builder/editor subagent

Wraps the existing wiki-ingest workflow (`aleph_wiki.agent`) + `ingest_source`/promote/hand-edit paths. Delegating makes "let's add/revise X" first-class without bloating chat with compile output.

**Files:** Create `apps/api/src/aleph_api/subagents/wiki_builder.py`; Modify `copilot_agent.py`.

- [ ] **Step 1: Implement.** A `SubAgent` with tools that reuse the existing `ingest_source` impl + a `commit_wiki_edit`/promote path (reuse `notes promote` / wiki service via self-call routes; do NOT call the DB directly — rule #3). System prompt: "Ingest/compile/commit the requested wiki change, then return the committed page/source short_id and a one-line summary." `subagent_model(settings,"wiki_builder")`.

- [ ] **Step 2: Register; move `ingest_source` under this subagent** (orchestrator delegates "add this source/page" to `wiki_builder`). Prompt updated.

- [ ] **Step 3: Verify.** Rebuild; controller verifies ingesting a URL / promoting a note delegates to `wiki_builder` and the page appears (Wiki tab via W4 stream); ledger + cost rows present. Commit `git commit -am "Wave 3 T4: wiki-builder subagent (delegated ingest/commit)"`

---

## Task 5 — viz/artifact-builder subagent

Builds Vega/A2UI cards / Builder artifacts in isolation; returns a finished A2UI card rendered via the Wave-4 shared catalog. Approval-gated for artifact builds (W6 B).

**Files:** Create `apps/api/src/aleph_api/subagents/viz_builder.py`; Modify `copilot_agent.py`.

- [ ] **Step 1: Implement.** A `SubAgent` with tools reusing `build_artifact` (already approval-gated via the agent-actions/ApprovalCard path) + a `make_chart` tool that builds a Vega spec / ChartCard. System prompt: "Build the requested visualization or report; return the finished A2UI card (ChartCard/ArtifactCard) instruction, not raw spec dumps." `subagent_model(settings,"viz_builder")`.

- [ ] **Step 2: Register; move `build_artifact` under this subagent.** Prompt: "For charts/reports/exports, delegate to the `viz_builder` subagent."

- [ ] **Step 3: Verify.** Rebuild; controller verifies "make a chart of X" delegates to `viz_builder` → a ChartCard renders inline (via the W4 catalog); "build a report" still goes through the ApprovalCard gate. Commit `git commit -am "Wave 3 T5: viz/artifact-builder subagent (returns A2UI cards)"`

---

## Task 6 — analyst + reviewer subagents

**Files:** Create `apps/api/src/aleph_api/subagents/analyst.py`, `reviewer.py`; Modify `copilot_agent.py`.

- [ ] **Step 1: analyst.** A `SubAgent` reusing the hypotheses tools (`list/create/add_evidence`) + the ACH endpoint; system prompt: "Structure hypotheses + weigh evidence (ACH); return a HypothesisCard/matrix summary." Moves the hypotheses tools under this subagent. `subagent_model(settings,"analyst")`.

- [ ] **Step 2: reviewer.** A `SubAgent` whose tool enqueues/invokes the existing `EditorialReviewerWorkflow` (reuse `editorial_review_job` path) for a given page; returns the findings summary. `subagent_model(settings,"reviewer")`.

- [ ] **Step 3: Register both; prompt** ("delegate competing-explanation analysis to `analyst`; delegate 'review this page' to `reviewer`").

- [ ] **Step 4: Verify + commit.** Rebuild; controller verifies "what are the competing explanations for X" delegates to `analyst` (HypothesisCard) and "review the X page" delegates to `reviewer` (findings). `git commit -am "Wave 3 T6: analyst + reviewer subagents"`

---

## Task 7 — Skills (SKILL.md) + orchestrator plan-and-delegate prompt

**Files:** Create `apps/api/src/aleph_api/skills/{research,ach,report-authoring,wiki-style}/SKILL.md`; Modify `copilot_agent.py`.

- [ ] **Step 1: Author the SKILL.md files.** Each: YAML frontmatter (`name`, `description`) + a concise procedure body (research methodology; ACH method; report/artifact authoring; wiki style + hand-edit conventions). Keep each focused (one workflow).

- [ ] **Step 2: Wire `skills=[...]`** into `create_deep_agent` (absolute paths to the skill dirs). Probe that deepagents loads SKILL.md frontmatter at startup (`uv run python -c "..."` or a startup log).

- [ ] **Step 3: Rewrite the orchestrator SYSTEM_PROMPT to "plan + delegate".** The orchestrator should: use `write_todos` to lay out a plan for multi-step work; delegate heavy work to the named subagents via `task`; keep its own replies conversational and concise; consult skills when relevant. Thin the orchestrator's own `tools=[...]` to only light/conversational tools (search_wiki quick scan, open_surface); heavy tools now live in subagents.

- [ ] **Step 4: Verify + commit.** Rebuild; controller verifies a multi-step request ("research X then draft a brief") produces a `write_todos` plan + delegations. `git commit -am "Wave 3 T7: skills (progressive disclosure) + orchestrator plan-and-delegate prompt"`

---

## Task 8 — todos → Activity card (live plan legibility)

**Files:** Modify the Activity card (`apps/web/src/components/ActivityCard*.tsx` — find it) + the agent-state/stream wiring in `CopilotChatSurface.tsx`/`lib/copilot.tsx`.

- [ ] **Step 1: Probe that CopilotKit v2 streams the agent's `todos` state to the frontend.** The orchestrator's `write_todos` writes a `todos` array to DeepAgentState. Confirm CopilotKit exposes agent state (e.g. `useCoAgentState`/the AG-UI state stream) and that `todos` surfaces. Read `lib/copilot.tsx`/`CopilotChatSurface.tsx` for how agent state is already read (`useAgentContext`). Record the access path.

- [ ] **Step 2: Render the plan.** In the Activity card, read `todos` (`[{content/title, status}]`) from streamed state and render a live list with per-item status (pending ◻ / in_progress ⏳ / completed ✓), alongside the existing AgentEvent activity. If CopilotKit doesn't surface `todos` directly, fall back to reading it via a small `/threads/{id}/state` fetch or the existing agent-events stream — document which.

- [ ] **Step 3: Verify + commit.** Rebuild web; controller verifies a multi-step delegated request shows the live plan (todos with statuses) in the Activity card. `git commit -am "Wave 3 T8: orchestrator todos plan surfaced in the Activity card"`

---

## Task 9 — Verification + final

- [ ] **Step 1: Enduring-session check.** Drive a long multi-turn session; confirm it doesn't degrade (the harness offloads large subagent results / summarizes — inspect that the orchestrator thread stays coherent). Document behavior.
- [ ] **Step 2: Delegate-and-isolate check.** Confirm a deep retrieval via `retriever` keeps the orchestrator context lean (the composed body isn't in the main thread; only the distilled answer).
- [ ] **Step 3: Cost-per-subagent.** Query `model_calls`: each exercised subagent has `purpose="assistant.subagent.<name>"` rows; Profile→Usage shows them. No double-count.
- [ ] **Step 4: Regression.** All Wave-6 flows (approval gate) + Wave-4 rendering (5 tabs, deltas) still pass.
- [ ] **Step 5: Commit any fixups; final review.** `git commit -am "Wave 3 T9: verification fixups"` (if any).

---

## Self-review notes (for the implementer)
- **Spec coverage:** W3.1=T1; W3.2 subagents=T2–T6; W3.3 skills=T7; W3.4 planning→Activity=T8 (+async-progress via existing Activity); W3.5 async → resolved as arq-jobs (T3) since `AsyncSubAgent` is remote-graph-only; verification=T9.
- **Rule #3:** every subagent tool calls a service/route or reuses an existing tool impl — never the DB directly.
- **Rule #5:** every subagent uses `subagent_model(...)` so its LLM calls are ledgered (T1). Verify in T9.
- **Rule #4 / #2 / approval (W6 B):** inherited from the reused tools/services; viz/artifact builds keep the ApprovalCard gate.
- **DRY:** extract `_read_wiki_impl` / `_start_research_impl` / `build_artifact` impls so subagents and any remaining inline tools share one body.
- **Probe-before-trust:** T1 (subagent task mechanism), T2 (read_wiki extraction), T8 (todos in CopilotKit state) each pin a live unknown.
- **Async honesty:** deepagents `AsyncSubAgent` = remote hosted graph (out of scope); long-running work = the existing arq pipeline. Documented in the header + T3.
- **Baked images:** `up -d --build aleph-api`/`aleph-web`, not `restart`.
- **Out of scope:** rewriting the wiki-ingest/editorial LangGraph workflows (wrapped, not rewritten); LangGraph Server deployment for true remote async subagents.
