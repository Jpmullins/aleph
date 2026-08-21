# Deep Agents and the LangChain / LangGraph stack — state of play, August 2026

Research date: **19 August 2026**. Every version number below was read from PyPI's JSON API or the
GitHub API on that date, not from memory. Where a claim comes from a blog post or a search summary
rather than a primary source, it is labelled.

---

## In one paragraph

**Deep Agents is a library.** It is a Python (and TypeScript) package published by LangChain that
takes the plain "call the model, run the tool it asked for, call the model again" loop and wraps it
in the machinery that long-running agents turn out to need: a to-do list the agent maintains for
itself, a *virtual filesystem* (a place the agent can park big intermediate results so they do not
sit in the prompt), the ability to hand a sub-task to a fresh child agent with its own clean context,
automatic summarisation when the conversation gets too long for the model's context window, and a
pause-for-human-approval mechanism. Before it existed, every team building a serious agent wrote
those five things by hand, badly, and re-wrote them every three months. Deep Agents sits on top of
LangChain's `create_agent` (the standard tool-calling loop) which in turn sits on LangGraph (the
execution engine that saves state so a run can survive a crash). Aleph already uses it: the assistant
orchestrator in `apps/api/src/aleph_api/copilot_agent.py` is a `create_deep_agent(...)` graph with six
subagents. **The single most important finding of this research is not about Deep Agents' features —
it is that none of the five production agent systems in `~/Documents/code/inspiration/` use an agent
framework at all**, and that the most architecturally similar one (DeepSeek's harness) implements the
agent loop as a plugin on a composability kernel, which is exactly the shape Aleph is building.

### Vocabulary, defined once

- **Agent loop** — ask model → model asks for a tool → run tool → give result back → repeat until the
  model stops asking. Everything else is scaffolding around this.
- **Harness** — the scaffolding. Prompt assembly, tool registry, context trimming, retries,
  approvals, persistence. LangChain now uses this word for Deep Agents explicitly.
- **Context window** — the maximum number of tokens a model can read in one call. When a conversation
  exceeds it, something must be thrown away or compressed.
- **Compaction / summarisation** — replacing old messages with an LLM-written summary of them.
- **Offloading** — moving a large tool result out of the conversation and into a file, leaving a
  pointer behind. The agent re-reads the file only if it needs to.
- **Subagent** — a child agent given one task and its own empty conversation. It reports back a short
  answer. The point is that the parent never sees the child's 40 intermediate tool calls.
- **Checkpointer** — storage that records agent state after each step so a crashed or interrupted run
  can resume. Aleph uses the Postgres one.
- **Middleware** — a hook that runs before/after the model call or the tool call and can modify what
  is sent or returned. This is LangChain's extension point; almost every Deep Agents feature is one.
- **Interrupt** — the agent stops mid-run and waits for a human decision, then resumes from the
  checkpoint.
- **MCP / A2A / AG-UI / ACP** — four protocols: agent-to-tool, agent-to-agent, agent-to-user-interface,
  and agent-to-code-editor respectively.

---

## 1. Current state — hard numbers

All read from PyPI/GitHub on 2026-08-19.

| Package | Latest | Released | What Aleph has installed |
|---|---|---|---|
| `deepagents` | **0.7.7** | 2026-08-18 | **0.6.6** (2026-05-28) — pinned `>=0.6,<0.7` |
| `langchain` | 1.3.15 | 2026-08-11 | 1.3.11 |
| `langchain-core` | 1.5.6 | 2026-08-17 | 1.4.8 |
| `langgraph` | 1.2.11 | 2026-08-11 | 1.2.6 |
| `langgraph-checkpoint` | 4.2.0 | 2026-08-07 | 4.1.1 |
| `langgraph-checkpoint-postgres` | 3.1.2 | 2026-08-07 | 3.1.0 |
| `langchain-openai` | 1.5.2 | 2026-08-18 | 1.2.2 |
| `langsmith` | 0.11.0 | 2026-08-14 | 0.9.1 |
| `deepagents-code` | 0.1.57 | 2026-08-18 | n/a — a terminal coding agent, not a library |
| `langchain-protocol` | 0.0.18 | 2026-06-18 | 0.0.18 (transitive) |
| `langgraph-api` | 0.12.6 | — | **not installed** (see licensing, §7) |

**Repo health** (GitHub API, 2026-08-19):

- `langchain-ai/deepagents` — 27,927 stars, 3,897 forks, 204 open issues, created 2025-07-27,
  MIT, last push **today**. 100 commits in the six days 2026-08-13 → 2026-08-19.
- **119 releases in 13 months.** 0.7.0 shipped 2026-07-29 after 8 alphas and 2 betas; seven patch
  releases since, roughly one every three days.
- `langchain-ai/langchain` 144,547 stars · `langchain-ai/langgraph` 40,006 stars, both pushed today.
- `langchain-ai/deepagentsjs` (TypeScript) 1,489 stars, actively pushed.
- **`langchain-ai/deep-agents-ui` is ARCHIVED** — last push 2026-06-21. If Aleph were ever going to
  borrow that reference UI, that door is closed. `agent-chat-ui` is the surviving reference client
  (per web search; not independently verified).

**Momentum: strongly rising, not falling.** LangChain the company raised $125M Series B at a $1.25B
valuation (Oct 2025, led by IVP with CapitalG, ServiceNow, Cisco, Datadog, Databricks). LangSmith
trace volume reportedly grew 12× year over year; ARR was reported at $12–16M annualised as of June
(these revenue figures come from press coverage, not a primary filing — treat as indicative). Deep
Agents is clearly where the company's engineering attention now is: the release train, the docs
restructuring around it, and a shipped end-user product (`deepagents-code`, a Claude-Code-style
terminal agent) all point the same way.

---

## 2. What changed in the last 6–12 months

This is the part where being out of date hurts, so it is specific.

### LangChain v1 (Oct 2025) — the restructuring that reset everything

- `AgentExecutor`, `initialize_agent`, and `langgraph.prebuilt.create_react_agent` are all
  **deprecated** in favour of one function, `langchain.agents.create_agent`, which runs on LangGraph
  and takes a **middleware** list.
- All the legacy chains (`LLMChain`, `ConversationChain`, the whole 2023-era surface) moved out to a
  separate package, **`langchain-classic`** (currently 1.0.8, 2026-06-10). The `langchain` package
  itself is now small.
- **LCEL (`prompt | llm | parser`) is NOT deprecated.** It survives as a composition idiom.
- **Content blocks**: model responses are normalised across providers, so text, reasoning traces,
  tool calls and citations arrive in the same shape from OpenAI, Anthropic and Google.
- `StateGraph` — the hand-built graph API — is **still fully supported** and is still the right tool
  for parallel fan-out, supervisor/worker, and custom branching. Aleph's five `StateGraph` workflows
  (research, reviewer ×2, wiki ×2) are **not** dated on this axis.

### LangGraph 1.2 (12 May 2026) — four primitives worth knowing

1. **`DeltaChannel` (beta)** — a state channel that stores incremental deltas instead of the full
   accumulated value at every checkpoint. LangChain's own blog cites a test case going from 5.27 GB
   of checkpoint storage to 129 MB. For Aleph, whose checkpoints are Postgres rows that grow with
   conversation length, this is the difference between a checkpointed long-running research run being
   cheap and being a storage problem.
2. **`TimeoutPolicy`** — per-node run and idle timeouts (Python only; TS unchanged).
3. **Node-level error handlers** with `Command`-based recovery — a failing node can return a routing
   command rather than blowing up the run.
4. **`RunControl.request_drain()`** — graceful shutdown: stop accepting new work, let in-flight work
   finish. Directly relevant to a docker-compose deploy that restarts containers.

Also in 1.2: **streaming `version="v3"`** — a content-block-centric event protocol with typed
per-channel projections (`run.messages`, `run.lifecycle`, `run.subgraphs`).

### deepagents 0.7.0 (29 July 2026) — the breaking-change boundary Aleph is pinned below

Aleph's `apps/api/pyproject.toml` says `deepagents>=0.6,<0.7`. Here is what is on the other side of
that pin, taken verbatim from the release notes:

- **`TodoListMiddleware` is no longer included by default.** The `write_todos` tool, the `todos` state
  channel and the todo-planning prompt are all absent unless you pass
  `middleware=[TodoListMiddleware()]` — and separately on each `SubAgent`. This is the single most
  visible behaviour change; an upgrade without this line silently removes the agent's planning tool.
- **Prompts are lean by default.** The authored base prompt is now empty. `BASE_AGENT_PROMPT` still
  imports and still returns the old text, but is deprecated for removal in 0.9.0. `TASK_SYSTEM_PROMPT`,
  `ASYNC_TASK_SYSTEM_PROMPT`, `SUMMARIZATION_SYSTEM_PROMPT`, `FILESYSTEM_SYSTEM_PROMPT` and
  `EXECUTION_SYSTEM_PROMPT` are **removed**. Measured result: default-agent input tokens dropped 65%
  (5,395 → 1,895) and tool-description tokens dropped 43%.
- **`FilesystemBackend` and `LocalShellBackend` default to `virtual_mode=True`.** Paths anchor under
  `root_dir`, `..` traversal is rejected, and escaping raises. Aleph already passes `virtual_mode=True`
  explicitly on its skills backend, so this is a non-event there.
- **Backend compatibility shims removed**: `BackendFactory`, `BACKEND_TYPES`, `FileFormat`, `Unset`,
  and the deprecated positional `runtime` parameters on `StateBackend`/`StoreBackend`. Aleph's
  `_memory_backend` already has a comment saying it deliberately does not pass `_rt` — so Aleph is
  **already 0.7-clean on this point**.
- **A recursive `delete` tool is now exposed** whenever the backend supports it, and delete is
  classified as a *write* for permission purposes. A rule that allows writing to a path now also
  authorises recursively deleting that subtree. **This is a genuine new blast radius.** Any Aleph
  upgrade must add an explicit deny or interrupt rule if agents should not be able to `rm -rf` a
  routed path.
- Tool output format changes: empty `ls`/`glob` returns `No files found` rather than `[]`;
  `read_file` no longer emits a fixed-width `cat -n` gutter. Anything parsing raw tool text breaks.

### deepagents 0.6.x during 2026 — features Aleph has access to *today* and is not using

- **0.6 (13 May 2026)** brought the **QuickJS code interpreter** (`deepagents[quickjs]`,
  `CodeInterpreterMiddleware`) enabling *programmatic tool calling* — the agent writes a small script
  that calls several tools and returns one result, instead of doing N round trips through the model.
- **Harness profiles** — per-model tuning bundles, with explicit support for open-weight models
  (the docs and release notes name Kimi K2.6, GLM 5.1/5.2, DeepSeek V4, NVIDIA Nemotron 3 Ultra).
- **Streaming v3** with typed subscribable projections and framework bindings for React/Vue/Svelte/Angular.
- **`ContextHubBackend`** — a versioned filesystem backed by LangSmith Context Hub. **Requires a
  LangSmith account.** See §7.
- **0.4 (Feb 2026)** brought pluggable sandbox support and made the OpenAI Responses API the default
  for OpenAI models.

---

## 3. What it can do today — the full surface

Deep Agents describes itself as an "agent harness" with four capability layers. Here is the whole
thing, with the names you actually type.

**Execution environment**

- Tools (plain Python functions or MCP servers via `langchain-mcp-adapters`).
- **Virtual filesystem** with pluggable backends, all implementing `BackendProtocol`
  (`ls`, `read`, `write`, `edit`, `glob`, `grep`, optional `delete`; all return result objects with an
  `error` field, none raise):
  - `StateBackend` — files live in LangGraph state, thread-scoped. No external service.
  - `FilesystemBackend` — real disk under a `root_dir`. No external service.
  - `StoreBackend` — cross-thread durable storage over a LangGraph `BaseStore` (Postgres/Redis).
  - `CompositeBackend` — route path prefixes to different backends. **Aleph already uses this.**
  - `ContextHubBackend` — LangSmith-hosted versioned repo. Requires LangSmith.
  - `LocalShellBackend` — filesystem + unsandboxed `subprocess.run`. Development only.
  - Sandbox backends (`SandboxBackendProtocol` adds `execute`) — LangSmith, AWS AgentCore, Daytona, E2B.
  - Custom backends are a documented first-class extension point (the docs ship an S3 skeleton).
- **Code interpreter** — `CodeInterpreterMiddleware` from `langchain-quickjs`, an in-process
  JavaScript sandbox. No container, no network.

**Context management**

- `SummarizationMiddleware` — default. Fires at **85% of the model's `max_input_tokens`**, keeps 10%
  of tokens as recent context, and writes the full pre-summary conversation to a file on the backend
  as the canonical record. Falls back to a hard-coded **170,000-token** trigger and 6 kept messages
  when model profile data is unavailable. **Read that sentence again — §8 depends on it.**
- **Tool-result offloading** — results over 20,000 tokens are written to the backend and replaced in
  the conversation with a path plus a 10-line preview.
- **Tool-call-input offloading** — old `write_file`/`edit_file` calls carrying whole file bodies get
  truncated to pointers once context hits 85%.
- `create_summarization_tool_middleware(...)` — gives the agent a `compact_conversation` tool so it
  can compact *deliberately at a task boundary* rather than being interrupted mid-thought by a
  threshold. This is a meaningful quality difference and almost nobody uses it.
- **Skills** — `SKILL.md` files with YAML frontmatter (`name`, `description`, optional `license`,
  `compatibility`, `metadata`, `allowed-tools`), following the open **agentskills.io** specification.
  Three-layer progressive disclosure: name+description in the system prompt at startup, full body on
  activation, `scripts/`/`references/`/`assets/` on demand. Skills load from any backend, and
  **an agent can write a new `SKILL.md` to a writable path at runtime and thereby give itself a new
  capability.** Aleph already mounts `skills=["/skills"]` from a read-only `FilesystemBackend`.
- `MemoryMiddleware` — long-term memory over a store.
- Prompt caching middleware for Anthropic and Bedrock; automatic Fireworks prompt-cache session
  affinity.

**Delegation**

- `SubAgent` (a dict: `name`, `description`, `system_prompt`, optional `tools`, `model`, `middleware`,
  `skills`, `response_format`). Runs synchronously, blocks, returns one `ToolMessage`. **Skills do not
  inherit** except for the built-in `general-purpose` subagent.
- `CompiledSubAgent` — wrap an existing compiled LangGraph as a subagent; it just needs a `messages`
  state key. **This is the bridge for Aleph's existing `StateGraph` workflows.**
- `AsyncSubAgent` / `AsyncSubAgentMiddleware` — background work. Gives the parent five tools:
  `start_async_task`, `check_async_task`, `update_async_task` (steer a running task mid-flight),
  `cancel_async_task`, `list_async_tasks`. Task metadata lives in its own `async_tasks` state channel,
  deliberately separate from message history so task IDs survive compaction. **Constraint: this
  requires an Agent Protocol server.** Reading the installed source, `async_subagents.py` imports
  `langgraph_sdk.get_client` and calls `client.runs.cancel(...)`; `url=None` selects in-process ASGI
  transport, which still means mounting the LangGraph API app. Aleph has no `langgraph-api` installed.
- `RubricMiddleware` — a cheap model grades the expensive model's output against a rubric and loops
  up to `max_iterations`. Requires a checkpointer.
- **Harness profiles / provider profiles** — `register_harness_profile(identifier, HarnessProfile(...))`
  can replace the base system prompt, append a suffix, override individual tool descriptions, exclude
  tools, exclude or add middleware, and configure the general-purpose subagent — *keyed on a model or
  provider identifier*. `register_provider_profile(name, ProviderProfile(init_kwargs=...))` packages
  model-construction arguments. **Both are discoverable via `pyproject.toml` entry points**
  (`deepagents.harness_profiles`, `deepagents.provider_profiles`). This is a real, documented plugin
  system inside the framework and it is almost entirely unknown.

**Steering**

- `HumanInTheLoopMiddleware` via `interrupt_on={"tool_name": True | False | InterruptOnConfig}`.
  Four decision types: **approve**, **edit** (change the arguments), **reject** (skip and send feedback
  to the model), **respond** (return a human message as the tool result). A `when` predicate lets you
  interrupt only on specific *arguments* — e.g. only when writing outside `/workspace/`.
- Filesystem permission rules with `mode="interrupt"` route through the same flow.
- Subagents can carry their own `interrupt_on`, or call `interrupt()` directly from inside a tool.
- Resumption needs a checkpointer, the same `thread_id`, and decisions in the same order as the
  `action_requests`.

**Streaming**

- Typed projections at every agent level: `messages`, `tool_calls`, `values`, `subagents`, `output`.
  Every subagent stream exposes the same projections, so `stream.subagents[...].subagents` recurses.
- Raw protocol events carry a `namespace` field — empty for the coordinator, populated for subagents.
- `stream.interleave()` merges projections in order; `asyncio.gather` over `astream_events()` for async.

---

## 4. How the pieces relate

```
  LangSmith (SaaS: traces, evals, Context Hub, deployment)   ← commercial, optional
  ────────────────────────────────────────────────────────
  deepagents 0.7.7      "batteries-included agent harness"    ← MIT
        │ depends on
  langchain 1.3.15      create_agent + middleware + content blocks
        │ depends on
  langgraph 1.2.11      execution engine, checkpoints, interrupts, streaming
        │
  langgraph-api 0.12.6  the Agent Protocol *server*           ← ELASTIC LICENSE 2.0
```

Notably, **`deepagents` no longer declares a direct dependency on `langgraph`.** Its
`requires_dist` is `langchain>=1.3.14`, `langchain-core>=1.5.0`, `langchain-anthropic`,
`langchain-google-genai`, `langsmith`, `packaging`, `wcmatch`. LangGraph arrives transitively through
`langchain`. That is a deliberate layering statement: LangGraph is now an implementation detail of
LangChain, and Deep Agents is the front door.

**They compose rather than compete.** But there is real overlap with the protocol layer, and this is
where a converging standard is emerging:

- **MCP** (agent → tools), **A2A** (agent → agent), **AG-UI** (agent → user interface), **ACP**
  (agent → code editor). Per search results, MCP, A2A and ACP all moved under Linux Foundation
  governance in Q1 2026 — governance convergence rather than technical convergence. AWS Bedrock
  AgentCore added AG-UI support in March 2026. (These items come from secondary sources; I did not
  verify them against primary announcements.)
- LangChain has its own: **`langchain-ai/agent-protocol`**, whose `streaming/protocol.cddl` is the
  wire-format source of truth, with generated typed bindings published as `langchain-protocol` on
  PyPI (0.0.18, 2026-06-18) and an npm twin. It is a thread-centric command/event protocol with
  channels `messages`, `tools`, `lifecycle`, `input`, `values`, `updates`, `checkpoints`, `custom`,
  over SSE/HTTP, WebSocket, or in-process transports. **It is language-agnostic and framework-neutral
  in design.** Aleph already has `langchain_protocol` installed transitively.
- `deepagents-acp` (0.0.10, 2026-08-12) wires Deep Agents into the Agent Client Protocol, i.e. Deep
  Agents can be driven from Zed / an ACP-speaking editor.
- Aleph's current transport, **AG-UI via CopilotKit**, is alive and moving: `copilotkit` 0.1.95 and
  `ag-ui-langgraph` 0.0.43 both released 2026-08-16, and `ag-ui-langgraph` now depends on
  `ag-ui-a2ui-toolkit>=0.0.4` — i.e. **AG-UI has grown a first-class A2UI toolkit**, which is directly
  relevant to Aleph's A2UI catalog.

---

## 5. The gap between what people use and what it can do

Ranked by value to Aleph.

1. **`ChatModel(profile={...})` — inject your own model metadata.** Verified in the installed source:
   `langchain_openai/chat_models/base.py:1272` resolves the profile from a **static baked-in table**,
   `langchain_openai.data._profiles._PROFILES`, keyed on the model *name string*. `ChatOpenAI` also
   accepts `profile=` as an explicit constructor field (`langchain_core/.../chat_models.py:366`).
   `ModelProfile` is a `TypedDict` with, among others: `max_input_tokens`, `max_output_tokens`,
   `tool_calling`, `tool_choice`, `tool_call_streaming`, `structured_output`, `image_inputs`,
   `pdf_inputs`, `audio_inputs`, `reasoning_output`, `temperature`, `open_weights`, `status`.
   **That list is nearly a one-to-one match for what `aleph_models.discovery` already reads from
   LiteLLM `/model/info`.** Almost nobody passes this. Everyone silently inherits the fallback.
2. **`create_summarization_tool_middleware` — agent-triggered compaction.** Letting the agent choose
   when to compact, at a task boundary, produces materially better summaries than a threshold that
   fires mid-reasoning. Two lines of code; near-zero adoption.
3. **Harness/provider profile entry points.** A packaging-level plugin system: any installed
   distribution can declare `[project.entry-points."deepagents.harness_profiles"]` and thereby change
   prompts, tool descriptions, excluded tools and injected middleware for a given model. This is a
   ready-made, already-shipping mechanism for exactly the "capability added at runtime by
   installation" model Aleph is building — and it is barely documented outside the profiles page.
4. **`CompiledSubAgent`.** Wrap an existing compiled LangGraph as a subagent. Aleph has five mature
   `StateGraph` workflows (research, editorial review, mechanical review, wiki ingest, synthesis) that
   the orchestrator currently reaches through HTTP tool calls. They could be subagents directly, with
   token accounting, streaming projections and interrupts for free.
5. **`when` predicates on interrupts.** Approval keyed on *arguments*, not on tool identity. "Interrupt
   only when the agent tries to delete something outside `/scratch`" is one predicate. Most people
   interrupt on whole tools and then train their users to click Approve reflexively, which destroys the
   value of approval entirely.
6. **`response_format` on subagents.** A subagent that returns a validated Pydantic object instead of
   prose. For Aleph's belief layer — where a subagent should return *claims with evidence spans*, not
   a paragraph — this is the difference between a parseable contract and a regex.
7. **Programmatic tool calling via `CodeInterpreterMiddleware`.** The agent writes one QuickJS script
   that calls five tools and returns one number, replacing five model round-trips. Hermes-Agent
   independently built the same idea ("write Python scripts that call tools via RPC, collapsing
   multi-step pipelines into zero-context-cost turns"), which is decent evidence the pattern works.
8. **`DeltaChannel`** for checkpoint size on long runs (LangGraph 1.2, beta).

---

## 6. Context engineering as practised in 2026

The field converged on a consistent set of moves. Deep Agents implements most of them; the
hand-rolled systems implement all of them, and more aggressively.

| Move | Deep Agents | Evidence from the shipped systems in `inspiration/` |
|---|---|---|
| Summarise at a threshold | `SummarizationMiddleware`, 85% / keep 10% | hermes-agent: `context_compressor.py` is **7,858 lines**; `conversation_compression.py` another 4,373 |
| Offload big tool results to files | automatic at 20k tokens | deepseek-harness has a whole `spill/` package family: `spill`, `spill-local`, `spill-policy` |
| Prune tool results specifically | tool-call-input offloading | deepseek-harness `compaction/compaction-tool-result-pruner` — a dedicated plugin |
| Agent-triggered compaction | `compact_conversation` tool | deepseek-harness `compaction/command-compact` — a user/agent command |
| Subagent context isolation | `task` tool, results only | deepseek-harness has **9** subagent packages incl. `subagent-fork-in-process`, `subagent-claude-code`, `subagent-codex` |
| Progressive-disclosure skills | `SKILL.md`, agentskills.io | deepseek-harness `skill/`; hermes-agent explicitly "compatible with the agentskills.io open standard" |
| Structured context assembly | middleware ordering | deepseek-harness `context/` package family: `agent-instructions`, `session-reference`, `time-context`, `tmux-context` — context assembly is *itself* pluggable |
| Cross-session recall | `StoreBackend` / `MemoryMiddleware` | hermes-agent: SQLite FTS5 session search + LLM summarisation + Honcho user modelling |
| Long-run durability | checkpointer + `durability` mode | deepseek-harness `session/session-persistence-sqlite`; hermes-agent's own session store |

**The convergent lesson: context management is not one feature, it is a policy layer with many small
composable pieces, and the good systems make each piece separately replaceable.** deepseek-harness's
`spill-policy` being a distinct package from `spill-local` is the tell. Deep Agents gets this
approximately right through middleware, but its policies are parameters on built-in middleware rather
than separately swappable plugins.

**On long-running work specifically**, the two patterns that hold up are (a) *deliberate compaction at
task boundaries*, not threshold compaction mid-reasoning, and (b) *the durable record is a file, not
the conversation* — Deep Agents does exactly this, writing the pre-summary conversation to the backend.
Aleph's belief layer is a stronger version of the same idea: claims in Postgres are the durable record
and the conversation is disposable.

**A caution on checkpointing.** A 2026 study ("Crab", surfaced via search, not read directly) reports
that over 75% of agent turns produce no recovery-relevant state, so blanket checkpointing is largely
waste. Treat as directionally useful, unverified. LangGraph's own answer is the `durability` dial:
`"exit"` (checkpoint only at the end — fastest, no mid-run recovery), `"async"` (write while the next
step runs — small crash-loss risk), `"sync"` (write before continuing — safest, slowest).

---

## 7. Honest assessment

### Where it is genuinely good

- **The context-management defaults are correct and hard-won.** 85%/10% thresholds, 20k offload
  cutoffs, writing the full conversation to a file before summarising, keeping `async_tasks` out of
  message history so IDs survive compaction. These are the details you only get right after being
  burned, and they are free.
- **The middleware model is a real extension point**, not a marketing word. Custom middleware merges
  into the default stack by `.name` match, so you can *replace* the built-in summariser rather than
  ending up with two.
- **Interrupt/approval is the best-shipped implementation in the ecosystem.** Four decision types
  including argument editing, argument-level `when` predicates, and subagent-scoped configuration.
- **The Postgres checkpointer is boring and works**, which is exactly what you want.
- **Performance is a non-issue at the framework level.** Measured on this machine, 2026-08-19:
  a 10-node `StateGraph` costs **0.605 ms** end-to-end with no checkpointer (**~60 µs per node**) and
  **1.44 ms** with `MemorySaver` (**~144 µs per node**). Against an LLM call of 300 ms–30 s, that is
  noise. The blog claim of "20–80 ms of framework overhead per call" does not match what I measured and
  I would not repeat it. The two costs that *are* real: **`import deepagents` takes ~0.38 s and pulls
  in 2,069 modules** (a process-startup cost, not a per-request one), and **a Postgres checkpoint
  write per super-step** — network + serialisation, growing with message-history size. That second one
  is what `durability="exit"` and `DeltaChannel` exist to fix.

### Where it is immature or oversold

- **Release velocity is a liability, not just a virtue.** 119 releases in 13 months, seven patches in
  three weeks, and a minor version that removed the default planning tool. A dependency shipping a
  breaking change every ~10 weeks is a standing tax on a small team.
- **Model metadata is a static table.** The whole context-management layer keys off
  `model.profile.max_input_tokens`, resolved from a hard-coded per-provider dict keyed on model name.
  **For any OpenAI-compatible gateway with operator-chosen model names — which is Aleph's entire
  deployment model — this silently returns nothing and the summariser falls back to a fixed 170,000
  tokens.** On a 32k-context local model behind Ollama, that means the framework will never compact
  before the model overflows. This is a direct, verified collision with one of Aleph's fixed
  constraints. It is fixable (§8) but you have to know.
- **Harness profiles are keyed on model/provider identifier strings.** With a gateway, those strings
  are whatever the operator named the deployment. The per-model tuning that makes open-weight models
  work well is therefore not automatic behind a gateway.
- **The best pieces pull toward LangSmith.** `ContextHubBackend` requires a LangSmith account. The
  sandbox backends are LangSmith / AgentCore / Daytona / E2B. Async subagents need an Agent Protocol
  server.
- **Licensing split is real and under-advertised.** `langchain`, `langgraph` and `deepagents` are MIT.
  **`langgraph-api` 0.12.6 and `langgraph-runtime-inmem` 0.32.6 are Elastic License 2.0** — verified
  from PyPI metadata on 2026-08-19. ELv2 restricts providing the software to third parties as a
  managed service. Anything in Deep Agents that needs the Agent Protocol *server* — async subagents,
  and the server side of the streaming protocol — crosses from MIT into ELv2. For a self-hosted
  docker-compose workbench that might one day be offered to someone else, that is a decision, not a
  detail.
- **Archived reference UI.** `deep-agents-ui` is archived. The polished front-end story is now either
  `agent-chat-ui`, LangSmith's hosted UI, or your own — which for Aleph it already is.

### Realistic cost of depending on it

One breaking minor per quarter, a docs surface that moves faster than you can read it, and a stack
trace that goes through several framework layers when something goes wrong. In exchange you get
maybe 8,000–15,000 lines you do not write, in a domain (context compaction, offload policy, interrupt
resumption) where the details are genuinely subtle. For a solo/small team that trade is currently
positive.

### Exit path

**Better than average, and worth stating precisely.** Deep Agents' three most valuable subsystems have
clean, non-LangChain-shaped seams:

- `BackendProtocol` is seven plain methods returning result objects with an `error` field. That is a
  portable interface you could re-implement in an afternoon.
- Skills are `SKILL.md` files on the **agentskills.io open standard**, shared with Claude Code and
  hermes-agent. They outlive the framework.
- The streaming protocol has a **CDDL wire spec** in a public repo with generated bindings. A client
  written against it is not written against LangChain.

The parts that *are* sticky: `create_deep_agent`'s middleware assembly, LangGraph state channels and
reducers, and the checkpointer schema. Migrating off means re-implementing the loop and re-homing the
checkpoint data — a matter of weeks, not months, and only if you kept your tools as plain functions.

---

## 8. The harder question: is a framework the right choice at all?

I looked at all five systems in `~/Documents/code/inspiration/`. The result is unambiguous.

**Not one of them uses LangChain, LangGraph, or Deep Agents.** `grep -ril "langgraph|langchain|deepagents"` across every `package.json`, `pyproject.toml` and `requirements.txt` in all five repos returns **zero matches**.

| System | Version | What it builds on | Loop |
|---|---|---|---|
| **hermes-agent** (Nous Research) | 0.20.4 | raw `openai==2.24.0` SDK, every dep exact-pinned | hand-rolled: `conversation_loop.py` is 8,298 lines |
| **opencode** | 1.18.18 | Vercel AI SDK (`ai` + ~22 `@ai-sdk/*` providers) + Effect + Drizzle | hand-rolled in `core/src/session` |
| **prime-agent** (Prime Intellect) | 0.7.3 | `@earendil-works/pi-ai` + `typebox`. **Two dependencies.** | hand-rolled |
| **deepseek-harness** | 0.1.0-rc.7 | **cordis 4.0.0-rc.8** — a plugin/DI kernel, not an agent framework | hand-rolled, *as a plugin* |
| **cordis** | 4.0.0-rc.8 | — | it *is* the kernel: "A Meta-Framework of Spatiotemporal Composability" |

Read that in a different order and it says something sharper. These teams did not reject frameworks
in general. They rejected *agent* frameworks, and three of them adopted a different kind of framework
underneath — an SDK-normalisation layer (Vercel AI SDK), an effect system (Effect), or a plugin kernel
(cordis). **The abstraction they wanted was over providers and lifecycle, not over the agent loop.**

**deepseek-harness is the case that matters most for Aleph**, because it has already solved Aleph's
exact problem. Its `packages/core/agent-loop/package.json` describes itself as *"The concrete agent
loop plugin for the DeepSeek Harness"*, and its README says:

> "This is the only package in the harness that contains concrete loop logic. Everything else is an
> abstract service or a plugin against extension points — new behavior goes into plugins, not here."

That loop is **1,643 lines across six files**. The other ~40 package families around it — `compaction`,
`context`, `spill`, `skill`, `subagent` (9 packages), `plan`, `todo`, `hooks`, `guard`, `acp`,
`sandbox`, `workflow`, `session`, `llm` — are plugins on cordis. The kernel provides scoped contexts,
service injection, disposal, and rollback-covered transactional creation; the agent loop is a
first-class *consumer* of that kernel, not a competitor to it.

The critical detail: **their agent loop is small.** The scaffolding is large, the loop is not. Which
means the question "do I need an agent framework?" is really the question "do I want someone else's
scaffolding, and will it sit inside my kernel or fight it?"

### The reasoned recommendation

**Keep Deep Agents. Demote it. Do not build the kernel on it.**

Concretely, three layers with a hard line between them:

1. **The kernel is Aleph's.** Capabilities, effects with inverses, revertible mounts, probes, the
   spawn ledger. Deep Agents is *mounted as one capability*, not as the substrate. deepseek-harness
   proves this arrangement works: the loop is a plugin, the kernel is theirs.
2. **Deep Agents is a swappable orchestration plugin behind an Aleph-owned interface.** Aleph should
   own the types the rest of the system sees — a run, a step, a tool call, an approval request, a
   streamed event. Deep Agents produces those; it does not define them. That interface is what makes
   the exit path real rather than theoretical.
3. **Everything durable stays in Aleph.** Claims, evidence, the action ledger, the cost ledger, the
   asset store, the belief layer. Deep Agents' virtual filesystem is *scratch space and offload
   space*, never the record. Aleph's existing `CompositeBackend` routing already encodes this
   correctly — `/memories/` to Postgres via `StoreBackend`, everything else ephemeral.

**Why not hand-roll?** Because the four systems that did are all older, larger and better-resourced
than Aleph (Nous Research, Prime Intellect, DeepSeek, SST), and because the parts Deep Agents gives
you — offload policy, compaction with a durable file record, interrupt resumption across a
checkpointer, typed subagent streaming — are exactly the parts that are subtle rather than laborious.
Writing the loop is easy; writing the compaction policy that does not lose the thing you needed is
not. hermes-agent spent 12,000 lines on compaction alone.

**Why not build on it?** Because Deep Agents is not a composability kernel and does not intend to be.
Its extension points (middleware, backends, profiles) are shaped for "customise this agent", not
"mount, swap and revert capability at runtime under a guardrail". Middleware is assembled at
`create_deep_agent` time; changing it means rebuilding the graph. Aleph's premise — an agent that
writes plugins for itself and activates them — needs mounting semantics that Deep Agents does not
have and is not going to grow.

**The one thing that would change my answer:** if the agent's self-authored plugins turn out to be
mostly `SKILL.md` files and tool functions rather than kernel capabilities, then Deep Agents' skills
system plus a writable `StoreBackend` route already *is* runtime self-extension, and the case for a
separate kernel narrows considerably. That is worth testing early and cheaply — before the kernel is
finished, not after.

---

## 9. Fit with Aleph specifically

Aleph's current usage, read from `apps/api/src/aleph_api/copilot_agent.py`: `create_deep_agent` with a
gateway-pointed `ChatOpenAI`, nine orchestrator tools, six subagents, `skills=["/skills"]` from a
read-only `FilesystemBackend(virtual_mode=True)`, a `CompositeBackend` routing `/memories/` to a
per-project-namespaced `StoreBackend` over `AsyncPostgresStore`, `CopilotKitMiddleware`, and a Postgres
checkpointer.

**Verdict: this is a current, non-dated, well-shaped usage.** The pattern is right. Two things are
behind and one is a genuine bug-in-waiting.

**Behind:** the `deepagents>=0.6,<0.7` pin, and langchain/langgraph two to four patch releases back.
Aleph's `StateGraph` + `TypedDict` workflows are *not* dated — `StateGraph` remains the supported API
for exactly the parallel/branching shapes Aleph uses. Aleph's `_memory_backend` comment already
anticipates the 0.7 removal of positional `runtime`, so that migration is pre-done.

**Bug-in-waiting:** `_gateway_chat_model` builds `ChatOpenAI(model=<gateway model name>, ...)` and does
**not** pass `profile=`. `ChatOpenAI._resolve_model_profile` looks up a static table keyed on model
name, finds nothing for a LiteLLM-named deployment, and `SummarizationMiddleware` falls back to a
fixed 170,000-token trigger. On a gateway fronting a 32k or 128k model — which "connect to Ollama or
vLLM" makes likely — **the agent will overflow the model's context before the framework ever tries to
compact.** This is not hypothetical; it follows directly from the code paths cited in §5.1.

**The fix is small and it is a perfect fit with Aleph's architecture**, because `ModelProfile`'s fields
(`max_input_tokens`, `max_output_tokens`, `tool_calling`, `structured_output`, `image_inputs`,
`reasoning_output`, `temperature`) are essentially the fields `aleph_models.discovery` already reads
from LiteLLM `/model/info`, with `aleph_models.hints` filling the gaps. Aleph's "no hardcoded model
list" rule and Deep Agents' need for model metadata are not in conflict — Aleph is *better positioned
than the framework's own defaults*, and just needs to hand the data over.

**On the interface requirements** — live progress, structured cards, approvals, streamed results —
Deep Agents covers all four, but Aleph's transport choice matters. Aleph is on AG-UI via CopilotKit
(`copilotkit` 0.1.95, `ag-ui-langgraph` 0.0.43, both 2026-08-16, both alive). Deep Agents' native
streaming v3 is richer: per-subagent projections with namespace discrimination, which maps directly
onto Aleph's `PipelineStrip` and `MAX_PANES` reading region. The choice is not urgent, but note that
`ag-ui-langgraph` now depends on `ag-ui-a2ui-toolkit` — AG-UI is growing A2UI support, which argues
for staying on AG-UI rather than migrating to the LangChain streaming protocol.

**On the known-broken cost-attribution hole** (`AgentCostCallbackHandler` skips uncosted responses):
Deep Agents does not fix this, and `deepagents-code` uses a separate package (`genai-prices`) for
pricing, confirming that per-call cost accounting remains the harness author's problem.

---

## What Aleph should do

1. **Pass `profile=` when building the agent model.** In `_gateway_chat_model`, construct a
   `ModelProfile` dict from `aleph_models.discovery` + `aleph_models.hints` and pass it to
   `ChatOpenAI(profile=...)`. Populate at minimum `max_input_tokens`, `max_output_tokens`,
   `tool_calling`, `structured_output`. This makes Deep Agents' entire context-management layer
   correct behind a gateway instead of guessing 170k. Ship a test that asserts the built model's
   `profile["max_input_tokens"]` equals what discovery reported — the same shape as the existing
   `test_subagent_model_points_at_gateway`.
2. **Plan the 0.6 → 0.7 upgrade as a real work package, not a version bump.** The checklist:
   re-add `TodoListMiddleware()` on the orchestrator *and each subagent* or accept losing `write_todos`;
   decide explicitly whether to pass `system_prompt=BASE_AGENT_PROMPT` or adopt lean prompts (a
   measured 65% input-token reduction is worth taking, but it changes behaviour); add deny or
   `mode="interrupt"` rules for the new recursive `delete` tool on every routed backend path; and grep
   for anything parsing raw `ls`/`glob`/`read_file` output.
3. **Keep Deep Agents behind an Aleph-owned interface, mounted as one kernel capability.** Aleph should
   define its own run / step / tool-call / approval / event types and adapt Deep Agents to them.
   This is what makes the exit path (§7) real, and it is what deepseek-harness does with its own loop.
4. **Give the agent `create_summarization_tool_middleware`.** Deliberate compaction at task boundaries
   beats threshold compaction mid-reasoning, especially for the research loop. Two lines.
5. **Convert one existing `StateGraph` workflow to a `CompiledSubAgent` as a spike** — the research
   workflow is the obvious candidate. It needs only a `messages` state key. If it works, the
   orchestrator gets typed streaming and interrupts over Aleph's real pipelines for free, and the
   HTTP re-entry hop goes away for that path.
6. **Use `response_format` on the subagents that feed the belief layer.** A retriever or reviewer
   subagent returning a validated Pydantic object with claims and evidence spans is a contract; one
   returning prose is a parsing problem.
7. **Use argument-level `when` predicates for approvals**, not tool-level ones. Interrupt on
   *destructive or out-of-scope arguments*. Blanket per-tool approval trains users to click through.
8. **Set `durability` deliberately per graph.** `"exit"` for cheap ephemeral chat turns, `"async"` or
   `"sync"` for the research loop where resumption matters. Measured cost of the checkpointer at the
   framework level is ~144 µs per super-step in memory; the real cost is the Postgres round-trip, and
   this is the dial that controls it. Evaluate `DeltaChannel` (LangGraph 1.2, beta) for the
   long-running research graph.
9. **Track `langchain-ai/agent-protocol` as the protocol to watch.** It has a CDDL wire spec, typed
   bindings in two languages, and framework-neutral design. Aleph's SSE surfaces could speak it later
   without adopting more of LangChain. Meanwhile stay on AG-UI, which is growing an A2UI toolkit.
10. **Test the cheap hypothesis early**: can the agent extend itself usefully by writing `SKILL.md`
    files to a writable `/skills/personal/` route on the `StoreBackend`? If yes, a large slice of
    "the agent writes its own plugins" is already shipping and the kernel can stay smaller.

## What Aleph should avoid

1. **Do not build the plugin kernel on Deep Agents or LangGraph.** Middleware is assembled at graph
   construction time; there is no mount/unmount/revert semantics, no capability scoping, no inverse.
   deepseek-harness ran the experiment and put the loop *inside* a kernel rather than the reverse.
2. **Do not let the virtual filesystem become the record.** It is scratch and offload space. Claims,
   evidence, ledgers and assets stay in Postgres and the asset store. Aleph's existing
   `CompositeBackend` routing is right — do not widen it.
3. **Do not adopt `ContextHubBackend` or the hosted sandbox backends.** They require a LangSmith
   account or a third-party sandbox service, which breaks the "docker compose, connects out to
   OpenAI-compatible endpoints only" constraint. Aleph already has `code-runner`; keep it.
4. **Do not adopt async subagents without pricing the server.** `AsyncSubAgentMiddleware` needs an
   Agent Protocol server (`langgraph_sdk.get_client`, `runs.cancel`). `langgraph-api` and
   `langgraph-runtime-inmem` are **Elastic License 2.0**, not MIT — verified from PyPI metadata
   2026-08-19. Aleph's existing arq workers already do background work under an MIT-licensed stack.
5. **Do not bump `deepagents` past `<0.7` without the §2 checklist.** The default removal of
   `TodoListMiddleware` is a silent capability loss, and the new recursive `delete` tool is a silent
   blast-radius increase. Both pass CI green.
6. **Do not lean on harness profiles for open-weight model tuning behind a gateway.** They key on
   model/provider identifier strings, which a gateway makes operator-defined. If Aleph wants
   per-model tuning it must register profiles against its *own* discovered identifiers — which is
   possible via `register_harness_profile` and entry points, but is Aleph's work, not free.
7. **Do not assume framework overhead is the performance problem.** Measured here: ~60 µs per graph
   node, ~144 µs with an in-memory checkpointer, against LLM calls three to five orders of magnitude
   larger. The costs that are real are process import time (~0.38 s, 2,069 modules — matters for
   worker cold start, not per request) and Postgres checkpoint writes. Optimise those, and do not
   let a plugin architecture be blamed for latency that belongs to the model.
8. **Do not treat "LangChain is bloated" blog posts as evidence.** The 2026 discourse is full of
   confident unsourced numbers (the "20–80 ms per call" figure did not survive contact with a
   benchmark on this machine). Measure Aleph's own path.
