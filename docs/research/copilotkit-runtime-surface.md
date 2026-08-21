# What CopilotRuntime actually is — a re-assessment

**Area:** runtime-server (the server half of CopilotKit)
**Date:** 2026-08-20
**Verified against:** the copy of `@copilotkit/runtime@1.63.2` installed at
`/Users/jpmullins/Documents/code/aleph/apps/copilot-runtime/node_modules/@copilotkit/runtime`,
its dependency `@ag-ui/a2ui-middleware@0.0.10`, `@ag-ui/client@0.0.57`, the thirteen official
CopilotKit skills under `/Users/jpmullins/Documents/code/aleph/.agents/skills/`, and the published
Python packages `ag-ui-langgraph@0.0.43` / `ag-ui-a2ui-toolkit@0.0.4` (downloaded and read).
Latest published `@copilotkit/runtime` is **1.68.2**; Aleph runs **1.63.2**.

---

## In one paragraph

Aleph runs a small Node service called `apps/copilot-runtime`. A previous review said it could be
deleted because "its only job — A2UI tool injection — now exists in Python." That conclusion does not
survive reading the code. The Node service is not a proxy that adds one tool. It is a **run
supervisor**: it holds a registry of agents, makes a fresh copy of one per request, wraps that copy in
a stack of interceptors (declarative UI, MCP tool access, open-ended HTML UI, tool-call filtering),
copies the browser's login header onto the outgoing call, runs the result through a *runner* that
records the event stream so a reloaded browser tab can rejoin a run already in progress, and answers a
**nineteen-method** HTTP surface — not five. Most importantly for Aleph specifically: the piece that
turns an agent's declarative-UI output into something the React app can actually draw — the A2UI
*middleware* — has **no Python implementation at all**. The Python packages the prior review cited are
the *other half* of that mechanism; their own source comments say they depend on the JavaScript
middleware being present in the run. Deleting `apps/copilot-runtime` today would silently turn off
every generative-UI card in Aleph. Where the prior review was right: Aleph is using perhaps 15% of
what this service can do, so "the service earns its keep" and "the current 80-line `server.ts` earns
its keep" are different claims, and only the first is true.

---

## 1. The prior recommendation, tested claim by claim

| Prior claim | Verdict | Evidence |
| --- | --- | --- |
| "The browser-facing protocol is just five documented HTTP routes." | **Refuted as stated; partly true in practice.** | `core/fetch-router.mjs` matches **19 route methods** (`RouteInfo` in `core/hooks.d.mts` enumerates all of them). The 1.58 client Aleph ships already calls seven of them — `/info`, `/agent/:id/run`, `/agent/:id/connect`, `/agent/:id/stop/:threadId`, `/agent/:id/suggest`, `/threads*`, plus `/transcribe` when enabled. "Five" describes the *minimum* Aleph exercises today, not the protocol. |
| "A2UI tool injection now exists in Python (`get_a2ui_tools` + `ag-ui-a2ui-toolkit`)." | **Half true, and the wrong half.** | Both Python packages are real and do what the review said. But they build the *subagent tool*. The `A2UIMiddleware` — which injects the catalog, sets the flag those Python tools read, gates painting on validation, and bridges clicks back — is TypeScript-only. `ag_ui_langgraph/a2ui_tool.py:192-196` says outright: *"only a validated surface is committed (**the middleware gate** suppresses any unvalidated attempt, so a rejected one never paints)."* |
| "FastAPI can serve those routes directly." | **True but misleading.** | FastAPI can serve the *shapes*. `POST /agent/:id/run` is not a pass-through: see §5 for the eight things it does before a byte reaches the agent. |
| "The service is no longer necessary." | **Refuted.** | Removing it removes the only implementation of the A2UI middleware contract, plus run-resume, tool-call filtering, header forwarding, suggestions, and the MCP/open-generative-UI extension points. |

**Where the prior review was right and should be kept:** Aleph's `server.ts` is 80 lines and uses one
option (`a2ui`) out of eleven. The version skew is real (`@copilotkit/runtime` 1.63.2 in the Node
service vs `@copilotkit/react-core` 1.58.0 in the web app vs 1.68.2 published). And Aleph *is* paying
the operational cost of a second language runtime for a thin slice of value. The correct conclusion is
"underused", not "unnecessary."

---

## 2. What the runtime does that is NOT "adapt AG-UI for the browser"

Enumerated from `core/runtime.d.mts` (the constructor options) and the handler modules.

### 2.1 Per-request agent construction

`agents` accepts a static record, a Promise, **or a factory function** `({ request }) => agents`
(`AgentsFactory`). The factory sees the raw `Request`, so the agent that serves a call can be chosen
by header, cookie, or tenant. Every run then gets `agents[agentId].clone()`
(`handlers/shared/agent-utils.mjs`) so per-request mutation never leaks between users.

*For Aleph:* the project id and the caller's principal are both on the request. Aleph could resolve a
**different agent per project** — or per installed plugin set — at the runtime boundary rather than
inside the graph.

### 2.2 A middleware stack applied to the agent, not the HTTP request

`configureAgentForRequest` composes up to four interceptors onto the cloned agent via
`AbstractAgent.use()`:

- `A2UIMiddleware` (see §3),
- `MCPAppsMiddleware` — attaches tools from external MCP servers, optionally scoped `agentId` by
  `agentId` (`mcpApps: { servers: [...] }`),
- `OpenGenerativeUIMiddleware` — the "Open-Ended" band of the generative-UI spectrum: it streams a
  `generateSandboxedUi` tool call's HTML through an incremental JSON parser (`clarinet`) and emits an
  `open-generative-ui` activity progressively as the HTML arrives,
- and `MCPMiddleware` for Intelligence enterprise-learning tools.

`@ag-ui/client` ships more middleware the runtime can mount: **`FilterToolCallsMiddleware`**
(`allowedToolCalls` / `disallowedToolCalls`) plus three protocol back-compat shims.

*This is the composability seam Aleph's kernel thesis is about.* A middleware is
`run(input, next) => Observable<BaseEvent>` — a revertible, stackable interceptor over an agent run,
with `runNextWithState()` giving each middleware the post-event message list and state. That is
structurally the same idea as Aleph's kernel effects, and it exists here as a shipped, tested
abstraction. **It has no Python counterpart** — `ag_ui` (Python) ships `core` types and an `encoder`
and nothing else.

### 2.3 Inbound-header forwarding — with a security policy

`mergeForwardableHeaders` (in `handlers/header-utils.mjs`) copies the browser's headers onto the
outgoing agent call. Default policy: `authorization` and any `x-*` header forward; a built-in denylist
strips infrastructure leakage (`x-forwarded-*`, `x-real-ip`, `x-request-id`, `x-vercel-*`, `x-amz-*`,
`x-copilotcloud-*`). Server-configured headers win on collision, matched case-insensitively so two
`Authorization` variants can't comma-join into an invalid double-JWT. Configurable via
`forwardHeaders: { useDefaultDenylist, deny, denyPrefixes, allow }`.

> **This falsifies a "Known broken" entry in Aleph's CLAUDE.md.** That entry reads: *"The runtime
> bridge does not forward the caller's credential. `apps/copilot-runtime/src/server.ts` constructs
> `new HttpAgent({ url: AGENT_URL })` with no headers."* Constructing `HttpAgent` with no headers is
> exactly the case `mergeForwardableHeaders` is written for — at 1.63.2 the runtime merges the
> inbound `Authorization` onto the clone at both `/run` and `/connect` before dispatch. The remaining
> gap is browser → runtime (the SSE `EventSource` problem), not runtime → API. Worth re-testing
> before that entry is quoted again.

### 2.4 The AgentRunner — run bookkeeping, replay, and stop

`runner` is a pluggable object with four methods: `run`, `connect`, `isRunning`, `stop`. It owns
whether a run can be **rejoined**. `GET /agent/:id/connect` replays the recorded event stream for a
thread, so a browser that reloads mid-answer catches up instead of losing the run. `POST
/agent/:id/stop/:threadId` cancels in flight. Concurrency policy is a knob: `onConcurrentRun:
"throw"` (default) or `"supersede"` (abort the in-flight run, start the new one).

A runner may additionally declare `ɵsupportsLocalThreadEndpoints = true` and implement
`listThreads` / `getThreadMessages` / `getThreadEvents` / `getThreadState` / `clearThreads`. When it
does, `GET /threads`, `/threads/:id/messages`, `/threads/:id/events`, `/threads/:id/state` light up
**without the managed Intelligence service** — `handlers/get-runtime-info.mjs` advertises them in
`/info` off `supportsLocalThreadEndpoints(runtime.runner)`. `InMemoryAgentRunner` already sets that
flag. (The installed skill doc says thread routes are Intelligence-only; the shipped code is ahead of
it.)

### 2.5 Suggestions as a first-class route

`POST /agent/:id/suggest` runs the agent **without** the runner, without a thread, lock, or
telemetry, and without any middleware — deliberately side-effect-free — and streams the result as
SSE so suggestion chips fill in live. Aborting the HTTP request cancels the provider call.

### 2.6 Voice transcription

`transcriptionService: new MyService()` (subclass `TranscriptionService`, one method
`transcribeFile({ audioFile, mimeType, size }) => Promise<string>`) turns on `POST /transcribe`,
accepting multipart (`audio` field) or base64 JSON (`audio` + required `mimeType`). Errors are
auto-categorised into a typed enum by keyword scan (`rate`/`429` → `rate_limited`, `auth`/`401` →
`auth_failed`, `too long`/`duration` → `audio_too_long`, else `provider_error`).

### 2.7 Live event tracing

`debug: {...}` builds a `DebugEventBus`; `GET /cpk-debug-events` is an SSE firehose of every AG-UI
event with `{agentId, threadId, runId}` attached (404s in production). This is what the CopilotKit
web inspector attaches to.

### 2.8 Response and telemetry hooks

`afterRequestMiddleware` receives `{ response, path, messages?, threadId?, runId? }` — the runtime
**parses its own SSE response** (`core/middleware-sse-parser.mjs`, reconstructing text messages, tool
calls, and `MESSAGES_SNAPSHOT`) so a post-run hook gets a structured transcript rather than a byte
stream. It runs non-blocking, fire-and-forget.

### 2.9 Licensing and managed-mode surfaces

`licenseToken` → `LicenseChecker`, surfaced as `licenseStatus` in `/info`. `intelligence` +
`identifyUser` + `channels` switch the whole runtime into managed mode (durable threads, websocket
transport, Slack/Teams Channels, memories, annotations). `channels` is typed `undefined` on the SSE
runtime — **Channels are not available self-hosted**, and Intelligence itself is explicitly
not self-hostable today.

---

## 3. What `CopilotRuntime({ a2ui: … })` actually does

Not tool injection. Enabling the key attaches `A2UIMiddleware` (`@ag-ui/a2ui-middleware@0.0.10`) to
every matching agent's run. Reading its class members and config type, it owns **eight** jobs:

1. **Catalog registration into the run.** `injectSchemaContext` puts the catalog into
   `RunAgentInput.context` under an exact-match description string. The LangGraph adapter
   (`ag_ui_langgraph/agent.py:869-894`) splits on that string and routes it to
   `state["ag-ui"]["a2ui_schema"]`. The comment in the Python toolkit is blunt: *"MUST stay
   byte-identical to `A2UI_SCHEMA_CONTEXT_DESCRIPTION` in `@ag-ui/a2ui-middleware` (the TypeScript
   twin cannot import this Python copy)."*
2. **Tool injection** (`injectToolAndFlag`) — the `render_a2ui` tool with a fixed structured schema,
   always replacing any existing definition, plus the `injectA2UITool` flag on forwardedProps that
   every framework adapter reads to decide whether to auto-wire its `generate_a2ui` tool.
3. **Protocol guidelines injection** (`injectToolGuidelines`) — the usage instructions that let a
   generic model emit valid A2UI without app-specific prompting.
4. **Progressive paint from a partially-streamed tool call.** `extractCompleteItems` /
   `extractCompleteObject` / `extractDataArrayItems` run a streaming JSON parser over the tool-call
   arg deltas and emit complete components as they close — the surface draws while the model is still
   writing it. The extractors are deliberately top-level-scoped so a component's own `data` key can't
   be mistaken for the arg.
5. **A generation lifecycle** (`buildLifecycleActivity`). The whole lifecycle rides one stable
   `messageId` (`a2ui-surface-${key}`): `status: "building" | "retrying" | "failed"` pre-paint, then
   the operations on paint, each state replacing the last — so there is never more than one skeleton
   and no separate "resolved" signal. `recovery: { debugExposure, showProgressTokens, maxAttempts }`
   controls how much retry detail the renderer shows and whether the skeleton carries a live token
   estimate.
6. **Semantic validation against the catalog** (`getValidationCatalog`) — component name → JSON
   Schema with `required`. This is the **paint gate**: an attempt that fails validation never renders,
   and the adapter's retry loop re-prompts with the structured errors.
7. **Action bridging.** `processUserAction` reads `forwardedProps.a2uiAction.userAction` and appends
   a synthetic `log_a2ui_event` tool call **and its result** into the conversation, so the agent sees
   a button click as a real event with resolved form values. The client half sets that forwardedProp
   and re-runs the agent (`createA2UIMessageRenderer` → `A2UIProvider onAction` in
   `@copilotkit/react-core`).
8. **Surface assembly and dedup.** `createA2UIActivityEvents` groups operations by `surfaceId` and
   isolates surfaces between invocations by `toolCallId`; `defaultCatalogId` stamps the host's catalog
   id onto streamed surfaces so a subagent cannot invent a catalog the frontend never registered.
   It also **holds back `RUN_FINISHED`** until pending A2UI tool calls have synthetic results, so the
   conversation stays protocol-valid.

`isA2UIEnabled` is called from exactly two places — the run path and the `/info` response — with a
source comment saying the divergence between those two was the root cause of a real upstream bug
(CopilotKit#5369). `/info` reporting `a2uiEnabled` is how the client decides to mount the renderer.

---

## 4. Middleware — could Aleph use it for auth, project scoping, cost accounting?

Two surfaces, both usable, neither used by Aleph today.

**`hooks` on `createCopilotRuntimeHandler` (preferred).** Four typed callbacks:

- `onRequest({ request, path, runtime })` — before routing. **Throw a `Response` to short-circuit**
  (returning one corrupts the request object — the handler assigns any truthy return back to
  `request`). This is the correct place for auth rejection.
- `onBeforeHandler({ route, request, … })` — after routing, with a **typed** `route` carrying
  `method`, `agentId`, and `threadId` where applicable. Route-aware authorization without
  string-matching paths.
- `onResponse({ response, route, … })` — may replace the Response.
- `onError({ error, route, … })` — may override the error response.

**`beforeRequestMiddleware` / `afterRequestMiddleware` on the constructor (legacy).** Pre-routing
only; `after` is fire-and-forget and receives the reconstructed messages/threadId/runId (§2.8).

**Aleph fit, concretely:**

- *Auth* — `onRequest` is where the caller's bearer token is verified before any agent work starts.
  It closes the same class of hole `_SELF_AUTH_PREFIXES` closed on the FastAPI side, one hop earlier.
- *Project scoping* — `onBeforeHandler` gets `route.agentId` and `route.threadId`. Aleph's thread ids
  already encode the project (`middleware/agent_scope.py` parses them); the same check can run at the
  runtime boundary, giving two independent defences instead of one.
- *Cost accounting* — `afterRequestMiddleware` receives the parsed transcript per run. This is a
  natural place to close Aleph's known cost-attribution hole for the agent path, because it fires
  once per run with `threadId` + `runId` regardless of whether the provider reported token usage.
  (It is *evidence for* a `ModelCall`, not a substitute for provider usage — but a run that produced
  messages and no `ModelCall` becomes detectable, which today it is not.)

The skills are emphatic that middleware is *the hook to invoke your auth/rate-limit library*, not a
place to implement one.

---

## 5. What a FastAPI reimplementation of "the five routes" would actually have to reimplement

`POST /agent/:id/run` is not a proxy. In order, it: (1) resolves `agents` — awaiting a factory if one
is configured; (2) 404s an unknown agent id; (3) **clones** the agent so per-request mutation is
isolated; (4) validates the body against `RunAgentInputSchema`; (5) attaches A2UI / MCP-Apps /
open-generative-UI / enterprise-learning middleware per config and per agent id; (6) merges
forwardable inbound headers under the denylist policy; (7) seeds messages, state and threadId; (8)
dispatches through `runtime.runner.run(...)`, which records the stream for later `connect`; and only
then streams SSE.

If Aleph served the five routes from FastAPI and dropped the Node service, this is what it would lose,
concretely:

1. **All A2UI rendering.** No `A2UIMiddleware` means no `a2ui-surface` ACTIVITY events, and
   `createA2UIMessageRenderer` only mounts on those. Every generative-UI card goes dark. Aleph would
   have to write, in Python: catalog-into-context injection, the flag, the streaming JSON extractor,
   the lifecycle skeleton, the validation paint gate, and surface grouping/dedup. **None of this
   exists in Python in any package, from CopilotKit or anyone else.**
2. **The click round trip.** Without `processUserAction`, a button press arrives as an opaque
   `forwardedProps.a2uiAction` that Aleph's graph must decode and convert into synthetic tool
   messages itself, matching a wire contract with no Python reference implementation.
3. **Run resume.** `/connect`'s replay depends on the runner having recorded the stream. FastAPI would
   need its own event journal per thread.
4. **Header-forwarding policy.** Including the infra-leak denylist and the case-insensitive
   collision rule — both of which are there because of real CVE-class bugs upstream (#5712).
5. **Tool-call filtering** (`FilterToolCallsMiddleware`) — a guardrail Aleph's plugin-trust model will
   want and would otherwise hand-roll.
6. **MCP Apps** and **open-ended generative UI** as configuration rather than code.
7. **Suggestions** (`/suggest`), **transcription** (`/transcribe`), **live event tracing**
   (`/cpk-debug-events`), and the **thread-inspection family** — all of which the shipped client
   already knows how to call.
8. **The `/info` contract** that tells the client which of the above are on. Getting `/info` wrong is
   how the client ends up mounting or not mounting the A2UI renderer; upstream has already had one
   production bug from exactly that divergence.

An honest cost: reimplementing (1)–(3) alone is a substantial Python project tracking a wire contract
that is versioned by someone else and, per the toolkit's own comments, kept in sync **by hand**
across languages.

---

## 6. AgentRunner and BuiltInAgent — could an Aleph plugin *be* one?

**AgentRunner — yes, and this is the most interesting extension point for Aleph.** The contract is
four methods over `Observable<BaseEvent>` (`runner/agent-runner.d.mts`), plus the optional
`ɵsupportsLocalThreadEndpoints` five. A **`PostgresAgentRunner`** for Aleph would:

- take the distributed lock in Postgres instead of a process-local map, making the "thread already
  running" guard correct across multiple API replicas (`InMemoryAgentRunner`'s store is a
  process-global singleton; a shared SQLite file does **not** fix horizontal scaling either, because
  live-run bookkeeping stays process-local);
- persist the event stream to the same database as the action ledger, so run replay and the ledger
  agree and are queryable together;
- light up durable `/threads*` endpoints self-hosted, with no managed service and no license;
- give Aleph a natural place to write a `ModelCall`/`CostLedgerEvent` per run.

The skill ships a Redis skeleton for exactly this shape.

**BuiltInAgent — probably not what Aleph wants.** It is CopilotKit's own agent: Simple Mode
(`{ model: "openai/gpt-4o", tools, mcpServers, maxSteps }`, auto-injecting `AGUISendStateSnapshot` /
`AGUISendStateDelta`) or Factory Mode (you own the LLM call via TanStack AI or the Vercel AI SDK;
CopilotKit owns the AG-UI lifecycle). Aleph's agent is a LangGraph deep agent that must go through the
LiteLLM gateway; `BuiltInAgent`'s model resolver understands `openai/…`, `anthropic/…`, `google/…`,
`vertex/…` only. **Not a fit.** Its real relevance is negative and useful: it proves the runtime is
agent-framework-agnostic — twelve external frameworks plug into the same `agents` dictionary, so a
future Aleph plugin could ship a CrewAI, PydanticAI or ADK agent alongside the LangGraph one **in one
runtime**, addressed by `agentId`, with no protocol work.

---

## 7. `defineTool` server-side tools — could plugins register them?

`defineTool({ name, description, parameters: <Standard Schema V1>, execute })` returns a
`ToolDefinition` that runs **in the runtime process**. Differences from agent tools and frontend
tools:

- **vs frontend tools** (`useFrontendTool`): the browser never sees a `TOOL_CALL_START` for a server
  tool, so nothing can render against it. Server tools are for I/O — DB, keys, signed URLs. On a name
  collision the server tool silently wins.
- **vs the Aleph agent's own tools**: `defineTool` only reaches the model through
  `BuiltInAgent.config.tools` (Simple Mode) or by conversion inside a Factory-Mode factory
  (`convertToolDefinitionsToVercelAITools`). **Aleph does not use `BuiltInAgent`, so `defineTool` is
  not reachable from Aleph's current wiring.** The equivalent for an `HttpAgent`-backed setup is MCP:
  `mcpApps: { servers: [{ type: "http", url, agentId }] }` attaches external tools to any agent,
  scoped per agent id — and that *is* a plugin-registration mechanism Aleph could use.

Reserved names to avoid: `AGUISendStateSnapshot`, `AGUISendStateDelta`.

---

## 8. Is there a Python equivalent of the runtime?

**No, and the gap is structural rather than a packaging accident.**

- The Python AG-UI SDK (`ag_ui`) ships `core` (event/message types) and `encoder` (SSE/protobuf). It
  has **no `AbstractAgent`, no `.use()`, no `Middleware`**. The whole interceptor abstraction the
  runtime is built on lives in `@ag-ui/client`, TypeScript only.
- `ag_ui_langgraph.add_langgraph_fastapi_endpoint` (what Aleph calls) mounts **one agent endpoint** —
  `POST` in, SSE out. That is the *upstream* side of the runtime, not the runtime.
- The `copilotkit` Python SDK 0.1.91 has `a2ui.py` (240 lines of op builders), `integrations/fastapi.py`
  (the v1 `CopilotKitRemoteEndpoint` Aleph's own code comments call broken), and no runtime, no
  runner, no middleware, no route surface.
- `ag-ui-a2ui-toolkit@0.0.4` (Python) is a faithful mirror of the JS `@ag-ui/a2ui-toolkit`: op
  builders, prompt assembly, history walkers, envelope orchestration, validation, and the retry loop.
  It is explicitly *"the subagent tool"* half. Its own docstring for `A2UI_OPERATIONS_KEY` reads
  *"Container key **the A2UI middleware** looks for in tool results."*
- Every Python framework adapter (LangGraph, ADK, CrewAI, Strands) contains comments describing what
  the JS middleware does for it — the ADK example even says *"the runtime's `injectA2UITool` flag
  (forwarded by the dojo's per-agent A2UIMiddleware) triggers injection."*

So the Python ecosystem gained the **tool-side** of A2UI in mid-2026. The **host-side** — middleware,
runner, route surface, `/info` contract — is JavaScript and is being actively extended there
(Channels, memories, annotations, open-generative-UI all landed on the JS runtime).

---

## 9. What Aleph is leaving on the table

Ranked by value to the stated product thesis (every ability is a plugin; every plugin ships its own
UI).

1. **`hooks.onRequest` / `onBeforeHandler`** — auth and project scoping one hop earlier, with typed
   route info. Costs ~30 lines.
2. **A custom `PostgresAgentRunner`** — durable, multi-replica-correct run state and self-hosted
   thread history in Aleph's own database, no managed service. This is the single highest-leverage
   item and it is a documented, four-method interface.
3. **`agents` as a per-request factory** — the agent registry becomes dynamic, which is exactly what
   "the agent authors plugins for itself and activates them" needs at the transport layer.
4. **`mcpApps`** — per-agent-scoped external tool servers as configuration. A plugin that ships an MCP
   server becomes installable without touching the graph.
5. **`FilterToolCallsMiddleware`** — an allow/deny gate on tool calls at the runtime edge; the natural
   enforcement point for Aleph's three-tier plugin trust model.
6. **`openGenerativeUI`** — the "Open-Ended" band: agent emits complete HTML into a sandbox, streamed
   progressively. Complements Aleph's declarative catalog for one-off visualisations that no catalog
   component covers.
7. **`afterRequestMiddleware`** — per-run transcript for cost/ledger reconciliation.
8. **`/agent/:id/suggest` and `/transcribe`** — two shipped features the client already knows how to
   call, currently unreachable because they are unconfigured.

Not on the table: **Intelligence, Channels (Slack/Teams), Automatic Learning, and Product Analytics.**
These require the managed CopilotKit cloud (`api.intelligence.copilotkit.ai`); self-hosting is
explicitly unsupported at 1.63/1.68, `channels` is typed `undefined` on the SSE runtime, and the
durable-thread need is better served by item 2 above.

---

## 10. Verdict

Deleting `apps/copilot-runtime` loses something real and load-bearing: the only implementation of the
A2UI middleware contract that Aleph's own renderer depends on, plus run-resume, header-forwarding
policy, and every runtime-level extension point Aleph's plugin thesis will want. The prior review
mistook "the tool moved to Python" for "the host moved to Python"; the Python packages' own source
comments say otherwise.

The right change is the opposite of deletion: **upgrade** (1.63.2 → 1.68.2, and lift
`@copilotkit/react-core` off 1.58.0 to match), and **use** the constructor — hooks for auth and
scoping, a Postgres runner for durability, a per-request agent factory for the plugin model. The
80-line `server.ts` is what should be replaced, not the service.

---

## Appendix — the full route surface (from `core/fetch-router.mjs`)

| Method | Route | Available in SSE mode? |
| --- | --- | --- |
| `info` | `GET /info` | yes |
| `agent/run` | `POST /agent/:agentId/run` | yes |
| `agent/connect` | `POST /agent/:agentId/connect` | yes |
| `agent/stop` | `POST /agent/:agentId/stop/:threadId` | yes |
| `agent/suggest` | `POST /agent/:agentId/suggest` | yes |
| `transcribe` | `POST /transcribe` | yes, if `transcriptionService` set (else 503) |
| `cpk-debug-events` | `GET /cpk-debug-events` | yes, non-production, if `debug` set |
| `threads/list` | `GET /threads` | yes **if the runner declares `ɵsupportsLocalThreadEndpoints`** |
| `threads/messages` | `GET /threads/:id/messages` | same |
| `threads/events` | `GET /threads/:id/events` | same |
| `threads/state` | `GET /threads/:id/state` | same |
| `threads/clear` | `POST /threads/clear` | same |
| `threads/update` | `PATCH \| DELETE /threads/:id` | Intelligence only (422 otherwise) |
| `threads/archive` | `POST /threads/:id/archive` | Intelligence only |
| `threads/subscribe` | `GET /threads/subscribe` | Intelligence only |
| `memories/list` | `GET \| POST /memories` | Intelligence only |
| `memories/subscribe` | `GET /memories/subscribe` | Intelligence only |
| `memories/mutate` | `PATCH \| DELETE /memories/:id` | Intelligence only |
| `annotate` | `POST /annotate` | Intelligence only |

Single-route mode (`mode: "single-route"`) collapses `run`/`suggest`/`connect`/`stop`/`info`/
`transcribe` into one `POST basePath` taking a `{ method, params, body }` envelope — useful behind a
strict reverse proxy; pair with `<CopilotKit useSingleEndpoint />`.
