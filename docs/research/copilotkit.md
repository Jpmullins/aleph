# CopilotKit — current state, capability surface, and whether Aleph still needs the Node bridge

Research date: **19 August 2026**. Everything below was checked against live sources on that date
(npm registry, PyPI, the GitHub API, the CopilotKit repo's own in-tree skill files, and the published
docs). Where sources disagree I say so and say which I trust.

---

## In one paragraph

CopilotKit is a **frontend kit for agent-powered apps**. An agent running on a server produces a
stream of small typed events — "here is a token of text", "I am calling this tool", "here is my
current internal state", "I need you to approve this". CopilotKit gives you (a) the React pieces that
turn that stream into a live interface, and (b) a small server that sits between the browser and the
agent. The event format it speaks is a separate open standard called **AG-UI**, which CopilotKit's
authors also created and which many other agent frameworks now implement. Before this existed,
everyone hand-rolled a websocket or SSE endpoint, invented their own JSON message shapes, and
rebuilt streaming chat, tool-call rendering and approval prompts from scratch in every project.
Aleph today runs a whole extra Node service (`apps/copilot-runtime/`) purely as CopilotKit's
server-side half. **The central finding of this research is that Aleph no longer needs that
service**: the one job it was created to do — injecting the A2UI drawing tool — now exists in
Python, and the browser-facing protocol the Node process speaks is five HTTP routes that FastAPI can
serve directly.

**Terms used throughout, defined once:**

- **AG-UI** — the open event protocol (agent → browser). ~30 typed event kinds over Server-Sent
  Events. MIT, governed at `ag-ui-protocol/ag-ui`.
- **A2UI** — a separate open protocol (originally from Google, now the `a2ui-project` org,
  Apache-2.0) that lets an agent *compose a layout* out of components the **frontend already owns**.
  The agent sends a description ("a Card containing a Column of these three things"), never code.
  Aleph already uses this — `packages/aleph-a2ui/…/catalog.json` is Aleph's component vocabulary.
- **Runtime** — CopilotKit's server-side piece. In Aleph this is `apps/copilot-runtime/`.
- **Generative UI** — the general idea of the agent producing interface, not just text.
- **Intelligence** — CopilotKit's *commercial* hosted backend (durable conversation threads, memory,
  a realtime websocket plane). Not required, and not what Aleph uses.
- **Channels** — CopilotKit's *commercial* Slack/Teams/Discord product line. Irrelevant to Aleph.

---

## 1. Current state as of August 2026

### Versions

| Thing | Latest | Date | What Aleph has |
|---|---|---|---|
| `@copilotkit/runtime` | **1.68.1** | 2026-08-14 | **1.63.2** (5 minors behind) |
| `@copilotkit/react-core` | **1.68.1** | 2026-08-14 | **1.58.0** (10 minors behind) |
| `@ag-ui/client` (npm) | **0.0.58** | 2026-08-14 | 0.0.53 (web) / 0.0.57 (runtime) |
| `copilotkit` (PyPI) | **0.1.95** | 2026-08-16 | `>=0.1.91` |
| `ag-ui-langgraph` (PyPI) | **0.0.43** | 2026-08-16 | **0.0.36** (2026-05-22, 3 months old) |
| `ag-ui-protocol` (PyPI) | **0.1.20** | 2026-08-14 | 0.1.18 |
| `@a2ui/react` | 0.10.2 | 2026-07-17 | `^0.10` — current |
| A2UI **spec** | **v0.9** | tag on `a2ui-project/a2ui` | v0.9 — current |

Note the two version schemes for A2UI: the *spec* is v0.9, the *renderer packages* are 0.10.x. Aleph
is on spec-current.

### Maintenance health — strong, arguably too fast

- `CopilotKit/CopilotKit`: 36,842 stars, ~199 contributors, MIT at the repo root, last push the day
  of this research. Weekly commit volume over the last eight weeks: 475, 316, 355, 250, 395, 501,
  314, 265.
- Release cadence is roughly **a minor per week with patches in between**. Between tags `v1.67.1`
  (2026-08-10) and `v1.68.1` (2026-08-14) there were **332 commits in four days**.
- Release notes are unusually good — long, specific, PR-linked — *except* that `v1.68.0` and
  `v1.68.1` shipped with empty bodies, and the repo's `changelog.txt` is stale at 1.8.2. Verify
  against tag diffs, not the changelog.
- Backed by CopilotKit Inc., a commercial company. Pricing tiers as published: Developer (free),
  Pro $39/mo, Team $100/seat/mo, Enterprise (custom).

### Momentum — the protocol is outrunning the framework

Weekly npm downloads, checked 2026-08-19:

| Package | Downloads/week | Stars |
|---|---|---|
| `ai` (Vercel AI SDK) | 18,372,863 | 26,292 |
| `@langchain/langgraph-sdk` | 3,020,851 | — |
| `@assistant-ui/react` | 1,375,126 | 11,728 |
| **`@ag-ui/client`** | **963,882** | 15,385 (protocol repo) |
| **`@copilotkit/react-core`** | **328,989** | 36,842 (framework repo) |

`@ag-ui/client` is downloaded roughly **three times as often as `@copilotkit/react-core`**. That is
the single most useful number in this document: people are adopting the *protocol* far more widely
than the *React framework built on it*. CopilotKit has more GitHub stars than assistant-ui but a
quarter of the npm installs. Read that as: CopilotKit is highly visible and genuinely healthy, but
its React layer is one of several competing choices, whereas AG-UI is becoming the shared substrate.

---

## 2. What it can do today — the full capability surface

Architecture is three layers joined by AG-UI:

```
Browser (React/Angular/Vue/React Native/web components)
   ↕  AG-UI over SSE  (plus a handful of JSON routes)
Runtime (a fetch handler you host — Node, Bun, Deno, Workers, Next.js, …)
   ↕  AG-UI over SSE
Agent (LangGraph, CrewAI, Mastra, PydanticAI, ADK, Claude Agent SDK, AWS Strands,
       Google ADK, LlamaIndex, Agno, MS Agent Framework, AG2, A2A, or your own)
```

### Frontend hooks (all from `@copilotkit/react-core/v2`)

| Hook / component | What it does |
|---|---|
| `useAgent` | Read and subscribe to an agent: its `state`, `messages`, `isRunning`. Also how you *start* a run programmatically and how you resume an interrupt. |
| `useAgentContext` | Push app state *up* to the agent as context ("the user is looking at page X"). Aleph uses this. |
| `useCapabilities` | **Read what the agent declares it can do**, from the `/info` handshake. See §6 — this is the most Aleph-relevant unused feature. |
| `useFrontendTool` | Register a browser-side tool the agent can call. Aleph uses this (`focus_tab`, `open_page`, `highlight_claim`). |
| `useHumanInTheLoop` | Register a tool whose execution is *gated on the user*: render an approval/choice UI, call `respond(result)` to unblock the agent. |
| `useInterrupt` | v2 hook for LangGraph `interrupt()` pause/resume. |
| `useRenderTool` / `useComponent` / `useDefaultRenderTool` | Attach React UI to a specific tool call, to a named component, or to *every* unmatched tool call. |
| `useRenderToolCall` | A **resolver**, not a registration hook — for building your own chat surface. |
| `useRenderActivityMessage` | Render structured, non-chat "activity" messages (progress, status, generated surfaces) inline. |
| `useRenderCustomMessages` | Inject UI before/after specific messages. |
| `useConfigureSuggestions` / `useSuggestions` | Static or LLM-generated follow-up suggestion chips. |
| `useThreads` | List/rename/archive durable threads — **Intelligence-only**, errors otherwise. |
| `useAttachments` | File/image attachments (drag, paste, upload). |
| `useCopilotKit` | The core object; also `copilotkit.subscribe({ onAgentsChanged })`, since there is deliberately no `useAgents()`. |

Components `CopilotChat` / `CopilotPopup` / `CopilotSidebar` ship from `react-core/v2`. **Not** from
`react-ui` — in v2, `@copilotkit/react-ui` is CSS only. `CopilotPanel` does not exist.

### The three flavours of generative UI

CopilotKit's own framing (from their `generative-ui` repo) is a spectrum from most-controlled to
most-open:

1. **Controlled** — `useRenderTool` / `useComponent`. You write the React component; the agent
   supplies props by calling a tool. Maximum control, zero novelty.
2. **Declarative** — **A2UI** or **Open-JSON-UI**. The agent composes a *layout* from a catalog of
   components the frontend registered. The agent can invent arrangements you never anticipated but
   cannot introduce a component you did not ship. This is what Aleph uses.
3. **Open-ended** — two mechanisms:
   - **Open Generative UI**: the agent calls a `generateSandboxedUi` tool and streams raw HTML/CSS,
     rendered inside `@jetbrains/websandbox` (a sandboxed iframe). Enabled with
     `new CopilotRuntime({ openGenerativeUI: … })`.
   - **MCP Apps**: an MCP server registers a resource with mime type `text/html+mcp` and links it to
     a tool via `_meta["ui/resourceUri"]`. `@ag-ui/mcp-apps-middleware` fetches the tool list and its
     UI resources at request time and emits an activity event; the built-in `MCPAppsActivityRenderer`
     draws it as a sandboxed iframe. **The UI ships with the tool, not with your frontend build.**

### Shared state, streaming, and intermediate progress

AG-UI's state family is `STATE_SNAPSHOT` (full replacement) and `STATE_DELTA` (RFC-6902 JSON Patch).
`useAgent({ updates: [UseAgentUpdate.OnStateChanged, …] })` gives the browser a live mirror of the
graph's state. `agent.isRunning` gives you a LIVE badge for free.

More interesting: **predictive state updates**. A LangGraph agent can declare
`StateStreamingMiddleware(StateItem(state_key="outline", tool="set_outline", tool_argument="outline"))`
and the *partially streamed arguments* of a not-yet-finished tool call are projected into shared
state and rendered token by token. The user watches a document assemble itself before the tool has
returned. This is in the Python SDK (`copilotkit.StateStreamingMiddleware`, `StateItem`) and is
essentially unknown outside the docs.

### Human-in-the-loop, three ways

1. **`useHumanInTheLoop`** — the agent calls a tool, the browser renders a UI, `respond(result)`
   unblocks it. If you never call `respond` (including on reject paths) **the run hangs forever**.
   Unmounting the component mid-execution abandons the run.
2. **Tool-call approval as two runs** — agent emits a tool call, run finishes, user decides, client
   sends the tool result back in a *new* run. Stateless; survives page reloads.
3. **LangGraph `interrupt()`** — the graph pauses at a checkpoint; `ag_ui_langgraph/interrupts.py`
   converts a LangGraph `Interrupt` into an AG-UI `Interrupt`; the browser resumes via
   `agent.runAgent({ forwardedProps: { command: { resume: … } } })`. This is the durable one: the
   pause lives in the checkpointer, not in a mounted React component.

### Multi-agent

`AgentCapabilities.multi_agent` declares `supported`, `delegation`, `handoffs`, and a list of
`sub_agents` with names and descriptions. On the client, per-panel `useAgent({ agentId })`,
agent-scoped tool registration (`useFrontendTool({ …, agentId })`), agent-scoped activity renderers,
and a documented key-remount pattern for switching. Agents are discovered from `/info`, not
hardcoded.

### Runtime-side

- `createCopilotRuntimeHandler({ runtime, basePath, hooks })` — the canonical primitive, a plain
  `(Request) => Promise<Response>`. Express/Hono adapters exist and are explicitly discouraged.
- **Agent runners** decide where run state lives: `InMemoryAgentRunner` (default),
  `SqliteAgentRunner` (file-backed, needs `better-sqlite3`), `IntelligenceAgentRunner` (managed,
  auto-wired), or your own subclass of `AgentRunner` for Redis/Postgres.
- **Hooks**: `onRequest`, `onBeforeHandler({ route })` (route-aware — knows `agentId`, `threadId`,
  and the method), `onResponse`, `onError`. Throwing a `Response` short-circuits.
- **Header forwarding**: the runtime forwards the browser's `authorization` and `x-*` headers onto
  the agent URL by default, with a denylist for CDN/platform artifacts. Server-configured agent
  headers win on collision. See §7 — this fixes a bug Aleph has documented as open.
- `transcription` (voice), `mcpApps`, `a2ui`, `openGenerativeUI`, `memory` (Intelligence),
  `channels` (Intelligence).

---

## 3. What changed in the last 6–12 months

**The whole v2 rewrite.** v2 replaced a GraphQL transport with AG-UI. Hook renames:
`useCopilotAction` → `useFrontendTool` + `useHumanInTheLoop`; `useCopilotReadable` →
`useAgentContext`; `useCoAgent` → `useAgent`; `useCoAgentStateRender` → `useRenderTool` /
`useRenderActivityMessage`; `useLangGraphInterrupt` → `useInterrupt`. `@copilotkit/runtime-client-gql`
and `@copilotkit/sdk-js` are removed from the v2 path; `CopilotTextarea` is gone. Package *names* did
not change — v2 lives at the `/v2` subpath. **Aleph is already on v2**, so this is not migration work
for Aleph, but note that `@copilotkit/react-core@1.58` still drags in `runtime-client-gql` and
`graphql@16` transitively; 1.68 does not.

Specific releases worth knowing, newest first:

- **1.68.0 / 1.68.1** (2026-08-14) — release notes not published. 332 commits since 1.67.1, mostly
  React Native, tests, demos, and a generated public-API manifest.
- **1.67.0** (2026-08-10) — trusted Inspector metadata; activity renderers refresh every frame again;
  `CopilotSidebar` gains `fullHeightChildren`.
- **1.66.3 / 1.66.4** (2026-08-07..11) — **`InMemoryAgentRunner` is bounded.** It previously kept
  every thread, every run, and a `ReplaySubject(Infinity)` per run in an unbounded process-global
  Map; one production deployment hit a fatal V8 OOM after ~173 threads / ~15.5k runs. Now
  `maxThreads: 1000` (LRU), `maxRunsPerThread: 100` (FIFO), `maxBytes: 512 MiB`. Also HITL
  run-identity fixes that stopped follow-up runs duplicating or losing tool calls. **Aleph runs
  1.63.2, which predates this — Aleph's Node service has the unbounded-memory bug.**
- **1.65.0** (2026-08-03) — one Memory access policy for agent and browser; Channels-only runtimes;
  construction-time validation of runtime config.
- **1.64.0** (2026-07-28) — **`@copilotkit/react-core/v2/headless`**: hook-only entry point,
  **~0.03 MB gzip vs ~2.96 MB from `/v2`**, because it drops the built-in chat rendering stack
  (streamdown, shiki, mermaid, cytoscape, katex). Also: Open Generative UI no longer renders an empty
  gray box; parallel frontend tool results keep their order; Stop no longer surfaces as an error.
- **1.63.0** (2026-07-15) — handler-owned managed Channels; stop-mid-stream no longer crashes chat;
  `/info` request storms fixed (70–80 requests on a single page load under StrictMode).

**Direction shift, clearly visible in the release notes:** the *open-source* surface is now fairly
settled, and almost all new engineering is going into **Intelligence** (durable threads, memory,
enterprise learning, a realtime gateway, the Inspector) and **Channels** (Slack/Teams/Discord/
Telegram/WhatsApp, at `channels/v0.9.0`). Six of the last ten releases were substantially or wholly
about those two. That is the commercial centre of gravity.

---

## 4. How it relates to the neighbours

- **AG-UI** — CopilotKit *made* it and *uses* it, but it is a genuinely separate MIT project with its
  own repo, its own daily release train (`release/2026-08-18` and so on), and roughly 3× CopilotKit's
  install base. Betting on AG-UI is safer than betting on CopilotKit, and AG-UI is the layer Aleph
  already speaks from Python.
- **A2UI** — an independent Apache-2.0 protocol (`a2ui-project`, 16.2k stars) with renderers for
  React, Flutter, Lit, Angular, Vue, Android. CopilotKit consumes it via `@ag-ui/a2ui-middleware` and
  `@copilotkit/a2ui-renderer`. **Aleph's dependency here is on A2UI, not on CopilotKit** — Aleph uses
  `@a2ui/react` directly and builds its own catalog.
- **MCP** — converging, not competing. `@ag-ui/mcp-middleware` connects a run to MCP servers and
  injects their tools; `@ag-ui/mcp-apps-middleware` renders MCP servers' UI resources. MCP is *how
  the agent gets tools*; AG-UI is *how the user sees the agent*.
- **assistant-ui** — the direct competitor for the React layer. More npm installs, fewer stars,
  narrower scope (chat UI primitives, no runtime, no protocol).
- **Vercel AI SDK / AI Elements** — overlaps on streaming-chat UI, but is provider-first (its own
  `useChat` transport) rather than agent-protocol-first. Two orders of magnitude more installs
  because it is the default for anything Next.js.
- **LangGraph** — complementary. LangGraph is the agent; CopilotKit is one of several front ends for
  it, and LangChain's own docs list CopilotKit as an integration.

**Is a standard converging?** Yes, on the wire: AG-UI for agent↔UI events, A2UI for declarative UI,
MCP for tools. All three are separately governed and all three are permissively licensed. The React
framework layer above them is *not* converging and is a genuine three-way contest.

---

## 5. Assessing the dependency honestly

### How heavy is it

`@copilotkit/runtime@1.68.1` is 3.9 MB unpacked and its **direct** dependencies include
`@ai-sdk/openai`, `@ai-sdk/anthropic`, `@ai-sdk/google`, `@ai-sdk/google-vertex`, `@ai-sdk/mcp`,
`@modelcontextprotocol/sdk`, `@graphql-yoga/plugin-defer-stream`, `@hono/node-server`,
`@remix-run/node-fetch-server`, `@copilotkit/license-verifier`, and `@scarf/scarf` (an
install-analytics package). Most of that exists to support `BuiltInAgent` — the mode where the
runtime itself calls a model provider. **Aleph never uses `BuiltInAgent`**, so Aleph is shipping four
model-provider SDKs into a container for nothing. Aleph's own rule is *"the agent path talks to the
gateway and nothing else… no provider SDK is called directly."* Nothing calls them today, but they
are on disk and in the image, and the rule is review-enforced only.

`@copilotkit/react-core@1.68.1` is 6.7 MB unpacked and pulls `streamdown`, `katex`, `lit`,
`@lit-labs/react`, `@radix-ui/*`, `lucide-react`, `react-markdown`, `@jetbrains/websandbox`,
`@tanstack/react-virtual`, `tailwind-merge`, `rxjs`. That is the **~2.96 MB gzip** figure. The
`/v2/headless` entry point is **~0.03 MB gzip** — a 100× difference — and gives you every hook with
none of the chat rendering.

### Licensing — mostly clean, with real gaps

The repo root is MIT and `@copilotkit/runtime`, `@copilotkit/react-core`, `@copilotkit/react-ui`,
`@copilotkit/a2ui-renderer`, `@ag-ui/*` core packages, and `@ag-ui/a2ui-middleware` all publish
`"license": "MIT"`. But these publish with **no `license` field at all**:

- `@copilotkit/core` (a hard dependency of `react-core`)
- `@copilotkit/sqlite-runner`, `@copilotkit/agentcore-runner`
- `@ag-ui/mcp-middleware`, `@ag-ui/mcp-apps-middleware`

There is a known open issue (#4704) about `@copilotkit/license-verifier` shipping without a LICENSE
file, which blocked some organisations' npm mirrors. I read these as **packaging omissions rather
than proprietary licensing** — the repo LICENSE is MIT and these are in-tree — but if Aleph has any
licence-scanning gate, `@copilotkit/core` will trip it, and it is not optional.

### Lock-in

Low-to-moderate, and asymmetric:

- **The wire is not lock-in.** AG-UI is a separate open protocol with a Python SDK; A2UI is a
  separate open protocol with its own React renderer. Aleph's Python side already emits AG-UI and its
  frontend already renders A2UI with `@a2ui/react`. Neither of those is CopilotKit.
- **The React hooks are moderate lock-in.** ~500 lines of Aleph code use them; the equivalents in
  assistant-ui or hand-rolled `@ag-ui/client` are different but not exotic.
- **The runtime is *low* lock-in but *high* operational cost.** See below.
- **Intelligence and Channels are real lock-in** — the managed service is explicitly not
  self-hostable from the OSS packages ("Intelligence mode targets the managed CopilotKit Intelligence
  service and is **not self-hostable**", per the repo's own `skills/runtime/SKILL.md`). The pricing
  page says self-hosting is optional on Team and standard on Enterprise, which I read as *available
  under an enterprise contract, not as an npm install*. **Aleph must not build on Intelligence.**

### Where it's genuinely good

Streaming correctness. The hard parts — partial-JSON tool-argument extraction so a surface can paint
before the call closes, event ordering across parallel tool calls, stop-mid-stream, replay after
reconnect, chunk-event expansion, state deltas as JSON Patch — are done properly and are the parts
that take months to get right by hand. The release notes read like a team that reproduces bugs before
fixing them. The in-tree `skills/` directory is the best documentation in the project and is worth
reading in preference to the website.

### Where it's immature or oversold

- **`selfManagedAgents` is genuinely ambiguous, and that matters** (see §7).
- Release velocity is a hazard: 332 commits between two patch tags in four days, two releases shipped
  with empty notes, a stale root changelog.
- The `/info` request-storm and unbounded-runner bugs were both basic and both shipped.
- The website docs and the in-repo skill files disagree in at least two places. Trust the repo.
- Marketing implies the runtime is load-bearing infrastructure. For an external-agent deployment like
  Aleph's, it is a thin SSE proxy plus a middleware chain plus a JSON handshake.

### Exit path if it stalls

Excellent, and this is the reassuring part. Because AG-UI is a separate MIT project with a Python
SDK, and A2UI is a separate Apache-2.0 project with its own React renderer, **the only thing Aleph
would have to replace is the React hook layer and the five HTTP routes**. Aleph's agent, its event
stream, its component catalog, and its renderer would all survive unchanged. Replacement options in
descending order of effort saved: `@ag-ui/client` directly (MIT, 615 KB, minimal deps) with Aleph's
own chat components; or assistant-ui with an AG-UI adapter.

---

## 6. The gap: what people use vs what it can do

Aleph uses maybe 20% of the surface. The high-value unused parts, ordered by what they would buy:

**1. `useCapabilities` + AG-UI `AgentCapabilities` — the plugin discovery surface Aleph needs.**
The Python `ag_ui.core.capabilities` module defines a rich typed schema an agent declares and the
client reads through `/info`: `identity` (name, type, version, provider, docs URL, arbitrary
metadata), `transport` (streaming, websocket, binary, resumable), `tools` (`supported`,
`items: List[Tool]`, `parallel_calls`, `client_provided`), `output` (structured output, MIME types),
`state` (snapshots, deltas, memory, persistent state), `multi_agent` (delegation, handoffs,
`sub_agents: List[SubAgentInfo]`), `reasoning`, `multimodal` (per-modality in and out),
`execution` (`code_execution`, `sandboxed`, `max_iterations`, `max_execution_time`),
`human_in_the_loop` (`approvals`, `interventions`, `feedback`, `interrupts`, `approve_with_edits`),
and a free-form `custom` dict. `useCapabilities()` returns it in React, `undefined` until the
handshake settles. **For a workbench where plugins are added and removed at runtime, this is
exactly the "what can I do right now" channel** — the UI feature-gates itself off a declaration the
backend computes from the live plugin set, with no frontend redeploy. Gotcha: `undefined` means
"handshake pending", never "no capabilities" — treat them differently or features stay hidden
forever.

**2. Activity messages as first-class structured progress.** `ACTIVITY_SNAPSHOT` / `ACTIVITY_DELTA`
carry `{ id, role: "activity", activity_type, content }`. Register a renderer with an `activityType`
and a Zod schema for `content`, and the agent emits typed progress cards that render *inline in the
message stream but are not chat bubbles* — resolved by `(activityType, agentId)` → `(activityType)`
→ `'*'`. Aleph currently uses this channel only for A2UI. It is the natural transport for the
pipeline strip, ingest progress, retrieval traces, reviewer passes. `ActivityMessage` is in the
Python `ag_ui.core.types`, so Aleph can emit these today. Silent-failure trap: `safeParse` is called
on every incoming activity and a schema mismatch renders nothing with only a `console.warn`.

**3. Predictive state updates.** `StateStreamingMiddleware(StateItem(state_key=…, tool=…,
tool_argument=…))` in Python projects a *partially streamed tool argument* into shared state.
The reader watches a report body, an outline, or a claim set assemble live rather than appearing all
at once when the tool returns. Ships in `ag_ui_langgraph.middlewares.state_streaming`, re-exported
from `copilotkit`.

**4. Durable interrupt/resume rather than component-lifetime approvals.** Aleph routes approvals
through card actions into its own ledger — which is right for auditing — but the *pause* is not
durable. LangGraph `interrupt()` puts the pause in the checkpointer;
`ag_ui_langgraph/interrupts.py` maps it to an AG-UI `Interrupt` (and refuses to synthesise a missing
id, precisely so multi-interrupt resumes cannot misroute); the browser resumes with
`forwardedProps: { command: { resume: … } }`. A reviewer can close the tab and come back. Contrast
with `useHumanInTheLoop`, where unmounting the component mid-execution abandons the run and never
calling `respond()` hangs it forever.

**5. `agent/connect` — reattach to a run in progress.** A separate SSE route that replays an active
run's events (or historic runs for a thread). This is how a long research run survives a page reload.
Aleph does not use it.

**6. Stateless suggestions.** `POST /agent/:id/suggest` runs the agent with the client forcing
`toolChoice: copilotkitSuggest`, deliberately bypassing the runner so no thread, lock, or telemetry
is written. `useConfigureSuggestions({ instructions, minSuggestions, maxSuggestions, available,
consumerAgentId })` supports LLM-generated *or* static chips, scoped per agent, recomputed from deps.
For a research workbench, "what should I ask next about this claim" is a strong affordance and it
costs one hook.

**7. Open-ended generative UI — directly relevant to agent-authored plugins.** Two mechanisms let an
agent ship UI that was never in the frontend build: **MCP Apps** (`text/html+mcp` resource +
`_meta["ui/resourceUri"]`, rendered as a sandboxed iframe) and **Open Generative UI** (a
`generateSandboxedUi` tool streaming raw HTML/CSS into `@jetbrains/websandbox`). Aleph's thesis is an
agent that authors plugins for itself; Aleph already renders agent-built artifacts in a `sandbox`
iframe. This is the same pattern with a protocol and a renderer already written.

**8. `@copilotkit/react-core/v2/headless`.** Aleph renders its own workspace shell and its own A2UI
cards. It pays ~2.96 MB gzip for a chat rendering stack (streamdown, shiki, mermaid, cytoscape,
katex) it substantially does not use. `/v2/headless` is ~0.03 MB gzip for the same hooks. Note this
entry point landed in **1.64.0**, after Aleph's pinned 1.58.

**9. `hooks.onBeforeHandler({ route })` — route-aware authorization at the runtime edge.** The route
object carries `method`, `agentId` and `threadId`. Aleph currently defends project scope at the
FastAPI boundary and again inside the graph; if the Node service survives, this is a third
free checkpoint.

---

## 7. Fit with Aleph — and the Node bridge question

### What Aleph runs today

`apps/copilot-runtime/src/server.ts` is 40 lines. It constructs a `CopilotRuntime` with one
`HttpAgent` pointed at `http://aleph-api:8000/copilotkit/agent/assistant`, turns on
`a2ui: { injectA2UITool: true, schema: ALEPH_A2UI_CATALOG, defaultCatalogId: "aleph://v1" }`, and
serves it on :4000. The Python side already speaks AG-UI natively via
`ag_ui_langgraph.add_langgraph_fastapi_endpoint`. **The entire justification for the Node process is
the A2UI middleware** — its own file comment says so.

### Finding A — that justification no longer holds

The A2UI toolchain now exists in Python, verified by downloading the wheels:

- **`ag-ui-a2ui-toolkit`** on PyPI (0.0.4, 2026-06-17) — the framework-agnostic half: op builders,
  prompt assembly, history walkers, the request/envelope orchestration, `RENDER_A2UI_TOOL_DEF`,
  `run_a2ui_generation_with_recovery`.
- **`ag_ui_langgraph.get_a2ui_tools(A2UIToolParams(...))`** (0.0.43) — the LangGraph adapter. Its own
  docstring: *"On LangGraph this is FREE: the subagent runs `model.astream` inside the graph, so its
  nested `render_a2ui` tool-call arg deltas surface natively as `OnChatModelStream` events, which the
  generic `agent.py` translator already turns into inner TOOL_CALL_START/ARGS/END."* Progressive
  surface painting, in Python, with no extra events to emit.
- **`copilotkit.CopilotKitMiddleware`** — a LangGraph `AgentMiddleware` that injects the A2UI tool
  automatically, guarded so a version skew degrades to "no auto-A2UI" rather than breaking.
- **`copilotkit.a2ui`** — `create_surface`, `update_components`, `update_data_model`, `render`,
  `a2ui_prompt`, and the `a2ui_operations` container key. The Node middleware's own type definitions
  say this key *"must match the key used by `copilotkit.a2ui.render()` (Python SDK)"* — the two
  halves are designed to be interchangeable.

Aleph pins `ag-ui-langgraph==0.0.36` (2026-05-22), which predates `get_a2ui_tools`. Upgrading to
0.0.43 is the prerequisite.

### Finding B — the browser-facing protocol is five routes

Read from `packages/runtime/src/v2/runtime/core/fetch-router.ts` and
`handlers/get-runtime-info.ts`. In SSE mode (no Intelligence) with an external agent, the client
touches:

| Route | Purpose |
|---|---|
| `GET {base}/info` | Handshake. Returns `{ version, agents: {name → {name, description, className, capabilities}}, mode, suggestions, a2uiEnabled, a2ui: {enabled, agents?}, openGenerativeUIEnabled, audioFileTranscriptionEnabled, threadEndpoints: {list, inspect, mutations, realtimeMetadata}, telemetryDisabled }` |
| `POST {base}/agent/{agentId}/run` | The run. SSE of AG-UI events. |
| `POST {base}/agent/{agentId}/connect` | Reattach/replay an in-flight or past run. SSE. |
| `POST {base}/agent/{agentId}/stop/{threadId}` | Cancel. |
| `POST {base}/agent/{agentId}/suggest` | Stateless suggestion run. SSE. Optional. |

Everything else — `threads/*`, `memories/*`, `inspector/metadata`, `annotate`, `cpk-debug-events` —
is Intelligence-only or optional. On the browser side, `packages/core/src/core/agent-registry.ts`
does exactly `fetch(runtimeUrl + "/info")` and then builds an `@ag-ui/client` `HttpAgent` against the
run route. There is no hidden handshake, no GraphQL, no websocket in this mode.

**FastAPI can serve these five routes.** `/info` is a static JSON document computed from Aleph's
plugin registry. The run/connect/stop routes are what `add_langgraph_fastapi_endpoint` already does,
plus path shaping and a cancel. This is not a reimplementation of CopilotKit; it is implementing a
small documented protocol that the MIT client already speaks.

### Finding C — do not rely on `selfManagedAgents`; sources disagree

There is a supported-looking prop for pointing the React provider straight at an AG-UI agent:

```tsx
<CopilotKit selfManagedAgents={{ "assistant": new HttpAgent({ url: "…" }) }}>
```

But:

- The **docs page** (`/backend/self-managed-agents`) says *"`selfManagedAgents` is part of
  CopilotKit's Enterprise Intelligence offering"* and *"Talk to an engineer about licensing for
  production use"*, and warns that without the runtime you lose server-side auth, middleware and
  routing, so *"your agent endpoint must authenticate and authorize every request."*
- The **repo's own in-tree skill files** (`packages/react-core/skills/react-core/SKILL.md` and
  `skills/runtime/SKILL.md`) say *"`agents__unsafe_dev_only` and `selfManagedAgents` are dev-only
  aliases of each other. **Not production-safe.**"*
- The `/backend/copilot-runtime` doc says direct AG-UI connection is *"intended strictly for
  development and prototyping … not recommended or supported for production."*

**I trust the in-tree skill files**, because they are versioned with the code, name their source
files, and are what the maintainers' own agents read. Either way the conclusion is the same: this is
not a foundation to build on. There is also a long-running open feature request (#2950 closed,
plus a 30-comment open request for "Direct Integration Between AG-UI and CopilotKit" since July 2025)
showing the community wants this and it has not been resolved.

### Finding D — three bugs in Aleph's CLAUDE.md are addressed upstream

1. **"The runtime bridge does not forward the caller's credential."**
   `packages/runtime/src/v2/runtime/handlers/shared/agent-utils.ts::configureAgentForRequest` ends
   with `agent.headers = mergeForwardableHeaders(agent.headers, request, forwardHeadersPolicy)`,
   forwarding `authorization` and `x-*` from the browser to the agent URL **by default** (denylist
   mode, stripping CDN/platform artifacts; server-configured agent headers win on collision;
   configurable via `ForwardHeadersConfig`). Upgrading `@copilotkit/runtime` 1.63.2 → 1.68.x plausibly
   closes this. **Verify against the tree — do not take my word for the version boundary.**
2. **Unbounded memory in the Node service.** Aleph's 1.63.2 predates the 1.66.3/1.66.4 fix; its
   `InMemoryAgentRunner` retains every thread, every run, and a `ReplaySubject(Infinity)` per run in
   a process-global Map. One reported deployment OOMed at ~173 threads / ~15.5k runs. This is a live
   production risk in Aleph today, not a theoretical one.
3. **The hand-mirrored catalog.** Since the catalog can now ride on the *provider*
   (`<CopilotKit a2ui={{ catalog }}>` is forwarded per-run and, per `configureAgentForRequest`,
   *"a catalog alone is enough to enable A2UI and inject the render tool"*), the server-side
   `a2ui.schema` copy is no longer required — `apps/copilot-runtime/src/catalog.generated.ts` can go.

### Finding E — an upgrade trap, if Aleph keeps the Node service

`apps/web/src/lib/copilot.tsx` passes `renderActivityMessages={[alephA2UIMessageRenderer]}`
by hand. In current versions the provider **auto-injects** the A2UI renderer whenever `/info`
advertises A2UI, and the a2ui-renderer skill flags manual wiring as a HIGH-severity mistake:
*"duplicates the renderer and can race with the auto-injected one."* If Aleph upgrades `react-core`
past 1.58 without deleting that prop, expect double-rendered or flickering surfaces. Switch to
`<CopilotKit a2ui={{ theme, catalog }}>`.

### Performance — is a plugin architecture going to be slow?

The plugin question and the CopilotKit question are separable, and CopilotKit's contribution is
measurable:

- **The Node hop costs one process, one network hop, and a full decode/re-encode of every SSE event
  in a JS event loop.** For token streaming that is a per-token cost on a hot path. It is not
  catastrophic — SSE proxying is cheap — but it is pure overhead for an external agent, and it puts a
  second thing between the user and the answer that can restart, OOM, or drop a connection.
- **The bundle is the bigger number.** ~2.96 MB gzip for `/v2` vs ~0.03 MB for `/v2/headless`. That
  is first-paint time on every load, and Aleph does not use the chat rendering stack.
- **Capability declarations are the anti-slowness tool for plugins.** `/info` is fetched once per
  connection and cached; the client feature-gates off it. That means "which plugins are live" costs
  one request, not a per-render probe.
- Watch: `useAgentContext` values are serialised into **every** request. Aleph currently pushes
  recent card actions on a 15-second poll into agent context — that is prompt weight on every turn.
- Memoize `renderActivityMessages` and every array prop; the provider console-errors on unstable
  array identity, and unstable identity means re-registration churn.

---

## What Aleph should do

1. **Delete `apps/copilot-runtime/` and serve the five routes from FastAPI.** Implement
   `GET /info`, `POST /agent/{id}/run`, `POST /agent/{id}/connect`, `POST /agent/{id}/stop/{threadId}`
   and (optionally) `POST /agent/{id}/suggest`. Keep `@copilotkit/react-core/v2` on the browser and
   point `runtimeUrl` at aleph-api. This removes a Node service, a Dockerfile, a second lockfile, a
   duplicated catalog, four unused model-provider SDKs, and an unbounded-memory bug — and it deletes
   the auth gap and the SSE-token gap by construction, because there is no longer a hop that needs to
   forward a credential. Do it behind a feature flag and cut over with the Playwright suite green.
2. **Move A2UI tool injection into Python** as the prerequisite for (1). Upgrade `ag-ui-langgraph`
   0.0.36 → 0.0.43 and use `get_a2ui_tools` / `CopilotKitMiddleware`, with Aleph's
   `catalog.json` as the schema. Ship the consumer in the same change: the frontend must render the
   Python-emitted surfaces before the Node service is removed.
3. **Generate `/info` from the live plugin registry, and populate `AgentCapabilities` honestly.**
   Declare `tools.items`, `human_in_the_loop.{approvals,interrupts,approve_with_edits}`,
   `multi_agent.sub_agents`, `state.{snapshots,deltas}`, `execution.sandboxed`, and put the plugin
   manifest hash in `identity.metadata`. Then feature-gate the UI on `useCapabilities()` rather than
   on hardcoded feature flags. This is how a runtime-mutable plugin set reaches the interface without
   a frontend redeploy — and it is the single best fit between this dependency and Aleph's thesis.
4. **Switch the browser to `@copilotkit/react-core/v2/headless`** (requires ≥1.64) and drop the
   built-in chat rendering stack. Aleph draws its own shell and its own cards; ~2.96 MB gzip → ~0.03 MB.
5. **Adopt activity messages as the transport for live progress.** Emit typed
   `ACTIVITY_SNAPSHOT`/`ACTIVITY_DELTA` from Python for ingest, retrieval, reviewer passes and the
   research loop; register one Zod-schema'd renderer per activity type. Pin each schema with a test —
   a mismatch fails silently with only a `console.warn`.
6. **Move approvals onto durable LangGraph `interrupt()`** while keeping the `ActionLedgerEvent`
   write. The pause then lives in the checkpointer, survives a reload, and matches Aleph's
   "agents never write state directly" posture better than a component-lifetime `respond()` callback.
7. **Add predictive state streaming** (`StateStreamingMiddleware` + `StateItem`) for anything the
   agent composes at length — report bodies, claim sets, outlines — so the reading region fills live
   instead of appearing at the end.
8. **Treat AG-UI, not CopilotKit, as the contract you depend on.** Pin `ag-ui-protocol` and
   `ag-ui-langgraph` deliberately, keep a conformance test over the event stream Aleph emits, and
   make sure nothing in `apps/api` imports a CopilotKit type it could not get from `ag_ui.core`.
9. **If you keep any Node runtime at all, upgrade to ≥1.66.4 today** — the unbounded-runner OOM is
   real and Aleph is on the wrong side of it.
10. **Prototype MCP Apps for agent-authored plugin UI.** An agent-written plugin that registers a
    `text/html+mcp` resource gets a sandboxed inline interface with no frontend redeploy. It matches
    the "no agent-emitted code runs in the app context" rule exactly — sandboxed iframe, versioned
    artifact — and it is the only mechanism found that lets a self-authored plugin bring its own UI.

## What Aleph should avoid

1. **Do not build on `selfManagedAgents` or `agents__unsafe_dev_only`.** The repo's own skill files
   call both "dev-only aliases … not production-safe"; the docs site calls the former part of the
   Enterprise Intelligence offering and tells you to talk to sales. Whichever is true, it is not a
   foundation. If you want to skip the runtime, own the five routes.
2. **Do not adopt CopilotKit Intelligence, Channels, Memory, or `useThreads`.** Intelligence is
   explicitly not self-hostable from the OSS packages; `useThreads` errors with "Runtime URL is not
   configured" outside it. These are the commercial core and the one place real lock-in lives. Aleph
   owns its own threads, its own memory, and its own ledger already.
3. **Do not keep a Node runtime "just for A2UI".** That was true when the service was written and is
   no longer true. Every week it survives, it drifts further from the pinned frontend and hides the
   auth gap it created.
4. **Do not upgrade `@copilotkit/react-core` past 1.58 while `renderActivityMessages` is passed by
   hand.** The provider auto-injects the A2UI renderer off `/info`; the manual prop then duplicates
   and races it. Move the catalog onto `<CopilotKit a2ui={{ catalog }}>` in the same change.
5. **Do not let `useAgentContext` become a dumping ground.** Every registered value is serialised
   into every request. The 15-second card-action poll currently feeds agent context on every turn;
   scope it, cap it, or move it to a tool the agent calls when it needs it.
6. **Do not use `useHumanInTheLoop` for anything that must be auditable or durable.** Forgetting
   `respond()` on a reject path hangs the run forever; unmounting mid-execution abandons it. Use
   graph interrupts for anything a reviewer might walk away from.
7. **Do not treat CopilotKit's React layer as a converging standard.** AG-UI, A2UI and MCP are
   converging; the React framework above them is a live three-way contest and CopilotKit has a
   quarter of assistant-ui's installs. Keep the coupling to hooks thin and confined to the shell, so
   swapping the React layer never touches the agent, the catalog, or the renderer.
8. **Do not assume the published docs are current.** Two direct contradictions were found between the
   website and the in-repo skill files in a single afternoon. Read `skills/` in the repo; it names its
   source files and ships with the code.
9. **Do not depend on `@copilotkit/sqlite-runner` or `@copilotkit/agentcore-runner`** — both publish
   with no `license` field, and Aleph has Postgres. If run persistence is ever wanted, subclass
   `AgentRunner` against Aleph's own database, or skip the abstraction entirely with (1).
10. **Do not let the runtime's provider SDKs into the image.** `@copilotkit/runtime` directly depends
    on the OpenAI, Anthropic, Google and Vertex AI SDKs to support `BuiltInAgent`, which Aleph must
    never use. Aleph's stated rule — everything goes through the LiteLLM gateway — is review-enforced
    only, and shipping four provider SDKs into a service is exactly how that rule quietly dies.

---

## Sources

Live-checked 2026-08-19. Version and health figures come from the npm registry API, the PyPI JSON
API, and the GitHub API; capability and architecture claims come from the CopilotKit repository's
own `skills/` tree and from package sources read directly.

- [CopilotKit repository](https://github.com/copilotkit/copilotkit) — releases, commit stats, `skills/`
- [CopilotKit docs](https://docs.copilotkit.ai/) and [self-managed agents](https://docs.copilotkit.ai/backend/self-managed-agents)
- [CopilotKit pricing](https://www.copilotkit.ai/pricing)
- [AG-UI protocol](https://github.com/ag-ui-protocol/ag-ui), [AG-UI docs](https://docs.ag-ui.com/introduction)
- [A2UI project](https://github.com/a2ui-project/a2ui) (Apache-2.0, spec v0.9)
- [Google Developers Blog — introducing A2UI](https://developers.googleblog.com/introducing-a2ui-an-open-project-for-agent-driven-interfaces/)
- [InfoQ — Google releases A2UI v0.9](https://www.infoq.com/news/2026/07/google-a2ui-genui/)
- [CopilotKit generative-ui resources](https://github.com/copilotkit/generative-ui)
- [Issue #2950 — v2 with self-hosted AG-UI + LangGraph](https://github.com/CopilotKit/CopilotKit/issues/2950), [issue #3159 — serving the runtime in Python](https://github.com/CopilotKit/CopilotKit/issues/3159), [issue #4704 — license-verifier LICENSE](https://github.com/CopilotKit/CopilotKit/issues/4704)
- [LangChain docs — CopilotKit integration](https://docs.langchain.com/oss/python/langchain/frontend/integrations/copilotkit)
- Packages read directly: `@ag-ui/a2ui-middleware@0.0.10`, `copilotkit==0.1.95` (PyPI),
  `ag-ui-langgraph==0.0.43` (PyPI), `ag-ui-protocol==0.1.20` (PyPI)
