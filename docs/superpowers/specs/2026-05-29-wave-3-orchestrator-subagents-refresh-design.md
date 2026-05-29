# Wave 3 (reconsidered) — Orchestrator + purpose-built subagents (living multi-agent workspace)

Status: **approved design** (pending final spec review). **Supersedes**
`2026-05-29-wave-3-deep-agents-design.md`, whose framing ("migrate the editorial
+ wiki LangGraph agents onto the harness") was low-value framework-churn. This
refresh, grounded in the LangChain Deep Agents docs (read 2026-05-29) and the
post-Wave-6/4 reality, targets the capability that matters: **context isolation
+ compression + planning + delegation** to make enduring, collaborative,
multi-agent sessions actually work.

## Why this (and why the old spec was wrong)

The Live assistant is ALREADY a `deepagents.create_deep_agent` (Wave 2), so it
already ships the harness's context-management, filesystem, and planning
middleware. **We just aren't using the layer that matters: subagents + skills.**
That is not a migration — it's unlocking the reason the harness was chosen.

Grounding facts from the Deep Agents docs (`docs.langchain.com/oss/python/deepagents`):
- **Context compression (automatic):** tool results >20k tokens are **offloaded**
  to the filesystem (replaced with a reference + 10-line preview); at ~85% of
  `max_input_tokens` the harness **summarizes** history (full transcript preserved
  to disk). Essential for enduring "think-together" sessions — the assistant today
  uses a plain `MemorySaver` and dumps large tool outputs (`read_wiki` composed
  bodies, research reports) straight into the one conversation thread.
- **Context isolation via subagents:** a subagent runs in its **own fresh context**
  and returns only a distilled final report — "the main agent receives only the
  final result, not the dozens of tool calls that produced it." Each subagent has
  its own tools/prompt/model/skills. Delegated via the built-in **`task` tool**.
- **Task planning:** the built-in `write_todos` tool maintains a `todos` array in
  agent state (`pending`/`in_progress`/`completed`); the docs' "Todo list" frontend
  pattern reads that array to render a **live progress dashboard**.
- **Async/parallel delegation:** `AsyncSubAgentMiddleware`/`AsyncSubAgent` (BETA —
  see risks) for background subagents while the conversation continues.
- **Skills (progressive disclosure):** `SKILL.md` frontmatter is read at startup;
  full skill body loads only when relevant — keeps the orchestrator prompt lean.
- **Long-term memory:** per-project `/memories/` via `CompositeBackend`+`StoreBackend`
  — already shipped in Wave 6 D1.

## Vision → capability mapping

| Vision (user) | This wave |
|---|---|
| Collaborate conversationally; think + research + build the wiki together | Orchestrator stays a thin conversational planner; **researcher** + **wiki-builder** subagents do the heavy arcs in isolation |
| Organic real-time retrieve/update during enduring sessions | **retriever** subagent isolates deep reads; harness compression keeps long sessions alive; per-project `/memories/` carries context across sessions |
| Dynamic visualizations, reports, generative-UI exploration | **viz/artifact-builder** subagent returns finished A2UI cards (Wave 4 shared catalog) |
| Async jobs / background agents / "and more" | async subagents + Activity-card progress + the orchestrator's live `todos` plan |

## Architecture

The Live assistant (`apps/api/src/aleph_api/copilot_agent.py`) becomes the
**orchestrator**: it plans with `write_todos`, converses, and **delegates** heavy
work to subagents via the `task` tool. Its context stays lean because subagents
return only distilled results and the harness offloads/summarizes. Subagents are
configured on `create_deep_agent(subagents=[...])` (sync `SubAgent` specs) and,
for long-running work, `AsyncSubAgent` via `AsyncSubAgentMiddleware`.

## Components

### W3.1 — Harness substrate + cost-to-subagents (do first)
- Confirm/tune the harness context middleware against the project `ModelProfile`'s
  `max_input_tokens` (offloading threshold, 85% summarization trigger). Keep the
  per-thread checkpointer (MemorySaver) AND the Wave-6 `/memories/` CompositeBackend.
- **Extend the Wave-6 `AgentCostCallbackHandler` (rule #5) to subagent models** —
  attach the cost callback to every subagent's `ChatOpenAI` (gateway), tagged
  `purpose="assistant.subagent.<name>"`, so subagent LLM calls are ledgered (today
  it's only on the orchestrator model).
- Keep rule #3 (subagents call typed services/tools, never the DB) and the Wave-6
  approval gate (consequential subagent actions still emit an ApprovalCard).

### W3.2 — Subagents
Each is a `SubAgent`/`AsyncSubAgent` with its own `tools`, `system_prompt`
(instructed to return a concise distilled summary, not raw data), optional `model`
+ `skills`:
1. **retriever** (sync) — wraps `WikiFirstRetrievalRouter` + intra-source descent;
   returns a cited composed answer. Isolates the large body from chat. Replaces the
   orchestrator calling `read_wiki` inline.
2. **researcher** (async) — owns the AIQ research→wiki arc (reuses the existing
   `/synthesize` + poller path); returns distilled findings + a draft-page pointer.
3. **wiki-builder/editor** (async for big ingests) — wraps the existing wiki-ingest
   LangGraph workflow (`aleph_wiki.agent`) as a delegated subagent: compiles/commits
   a page or revision (promote-note, hand-edit, "add X"), returns "committed Sxxxx".
4. **viz/artifact-builder** (sync/async) — builds Vega specs / A2UI cards / Builder
   artifacts in isolation; returns a finished A2UI card rendered via the Wave-4
   shared catalog (`aleph://v1`).
5. **analyst** (sync) — ACH / hypothesis structuring + evidence weighting; returns a
   HypothesisCard/matrix (reuses `aleph_hypotheses` + the ACH endpoint).
6. **reviewer** (async) — the existing EditorialReviewer invokable as a subagent for
   "review this page" (reuse `EditorialReviewerWorkflow`; no rewrite).

The subagents WRAP existing tools/services/workflows — this is delegation +
context isolation, NOT rewriting the wiki/editorial agents. The wiki-ingest and
editorial LangGraph workflows stay as-is and are invoked by their subagent wrappers.

### W3.3 — Skills (SKILL.md, progressive disclosure)
Author `SKILL.md` files (frontmatter: `name`, `description`, body = procedure) for:
research methodology, ACH method, report/artifact authoring, wiki style/hand-edit
conventions. Passed via `create_deep_agent(skills=[...])`. Loaded on demand → keeps
the orchestrator prompt lean. Store under `packages/aleph-assistant/.../skills/` (or
an api path), one focused workflow per skill.

### W3.4 — Planning + async progress → Activity card (legibility)
- The orchestrator's `write_todos` plan lives in agent state as `todos`
  (`pending`/`in_progress`/`completed`). AG-UI streams agent state to the frontend
  (CopilotKit exposes it). The **Activity card** (`apps/web/.../ActivityCard` +
  the `agent-events`/state stream) reads `todos` and renders the **live plan** — what's
  planned, done, in flight, next — per the docs' "Todo list" frontend pattern.
- **Async subagents** surface as Activity entries too (running/done), and their
  results (new wiki page, built artifact) stream into the right panel via the
  Wave-4 delta `SurfaceStreamer`. The workspace becomes legible: you see the plan
  + the background agents working.

### W3.5 — Async (beta — accepted with fallback)
Use `AsyncSubAgentMiddleware`/`AsyncSubAgent` for long-running subagents
(researcher, wiki-builder, reviewer). **Because async is beta**, design a graceful
fallback: if async dispatch misbehaves, the orchestrator falls back to the existing
**background-job pattern** (the research poller / arq jobs already exist) and reports
progress via Activity the same way. The reliable default for fast subagents
(retriever, analyst, viz) is sync `task`-tool delegation; async is additive for the
long ones. Probe the installed `deepagents 0.6.x` async API before wiring (it moves
fast).

## Data flow
You ↔ orchestrator (plans via `write_todos`, converses) → `task`-delegates to a
subagent (fresh context) → subagent does heavy work (tools/services/workflows),
offloads large artifacts to the filesystem/`/memories/`, emits A2UI cards → returns
a concise summary → orchestrator weaves it into the conversation. The live `todos`
plan + async-subagent status render in the Activity card; subagent results stream
to the right panel via the W4 streamer. Cross-session continuity via per-project
`/memories/`.

## Integration constraints (load-bearing)
- **Rule #3:** subagents call typed `aleph-api` services / existing tools — never DB/S3.
- **Rule #5:** the W6 cost callback MUST be extended to subagent models (else their
  LLM calls go unledgered). Verified by per-subagent `ModelCall` rows.
- **Rule #4:** subagent mutations write ledger events via the services they call.
- **Approval (W6 B):** consequential subagent actions keep the ApprovalCard gate.
- **Rule #2:** subagent models are `ChatOpenAI`→gateway only.
- The wiki-ingest + editorial LangGraph workflows are REUSED (wrapped), not rewritten.

## Verification
- **Enduring session:** a long multi-turn collaborative session triggers
  offloading/summarization and does NOT degrade or overflow (inspect that large
  tool outputs are offloaded to files; the orchestrator still tracks intent).
- **Delegate-and-isolate:** the orchestrator delegates a deep retrieval to the
  retriever subagent; the orchestrator's own context stays lean (the composed body
  doesn't appear in the main thread; only the distilled answer does).
- **Async research while conversing:** kick off the researcher; keep chatting; the
  Activity card shows the running subagent + the `todos` plan; the draft page lands
  in the Wiki tab via the W4 stream.
- **Viz-builder:** "make a chart of X" → viz subagent returns a rendered ChartCard.
- **Cost:** per-subagent `ModelCall`/`CostLedgerEvent` rows exist (rule #5); Profile→Usage
  shows `assistant.subagent.<name>` lines.
- **Planning legibility:** the Activity card renders the live `todos` with statuses.
- All Wave-6 flows (tools, approval gate) and Wave-4 rendering still pass.

## File structure
- **Modify:** `apps/api/src/aleph_api/copilot_agent.py` (orchestrator: `subagents=`,
  `skills=`, async middleware, prompt → "plan + delegate"), `copilot_cost_callback.py`
  (attach to subagent models).
- **New:** `apps/api/src/aleph_api/subagents/` (one module per subagent spec:
  retriever, researcher, wiki_builder, viz_builder, analyst, reviewer — each wraps
  existing tools/services), `.../skills/*/SKILL.md`.
- **Modify (frontend):** the Activity card to read `todos` from streamed agent state;
  async-subagent status entries.
- **Reuse (no rewrite):** `aleph_wiki.agent` (wiki ingest), `aleph_reviewer.editorial`,
  `WikiFirstRetrievalRouter`, the AIQ `/synthesize` path, the W4 shared catalog +
  SurfaceStreamer, the W6 `/memories/` store + cost callback + approval flow.

## Risks / honest notes
- **Async is beta** — accepted per user direction; sync `task` delegation is the
  reliable default + arq-job fallback for the long subagents. Probe the 0.6.x async
  API before building.
- **Delegation latency:** a `task` round-trip adds a model hop. Delegate only
  genuinely heavy/context-bloating work; keep trivial answers inline. Tune via the
  orchestrator prompt + skills.
- **Orchestrator must reliably choose to delegate** — prompt engineering + skill
  descriptions; verify the model actually uses `task` for heavy work.
- **Cost-attribution gap** if the callback isn't extended to subagent models (W3.1
  addresses this explicitly).
- **Activity card todos** depends on AG-UI streaming agent state to the frontend —
  confirm CopilotKit v2 exposes `todos` from state (probe; it exposes agent state
  generally, but verify the field surfaces).

## Out of scope
- Rewriting the wiki-ingest or editorial LangGraph workflows (they're wrapped, not
  rewritten).
- New connectors / new card types.
- Managed Deep Agents / LangSmith hosted runtime (we run in-process).

## References — probe before coding
- LangChain Deep Agents docs (`docs.langchain.com/oss/python/deepagents/`):
  `harness` (planning/offloading/summarization), `context-engineering`, `subagents`,
  `frontend/todo-list`. Skills: `deep-agents-core`, `deep-agents-orchestration`,
  `deep-agents-memory` (authoritative for installed 0.6.x).
- Probe the installed API: `uv run python -c "import deepagents; print(dir(deepagents))"`
  for `SubAgent`/`AsyncSubAgent`/`AsyncSubAgentMiddleware`/`SubAgentMiddleware`
  signatures + how `subagents=`/`skills=` are passed to `create_deep_agent`.
- In-repo: `copilot_agent.py` (the orchestrator to extend), `copilot_cost_callback.py`
  (extend to subagents), the W4 shared catalog + streamer, the W6 `/memories/` store.
