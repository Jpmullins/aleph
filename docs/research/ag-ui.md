# AG-UI — the Agent-User Interaction protocol

Research current as of **19 August 2026**. Written for a reader who has never used AG-UI.
Every claim here was checked against the live repository, the published packages, or the
files installed in this tree — not from memory. Where I could not verify something I say so.

---

## In one paragraph

AG-UI is a list of about 35 named messages ("events") that an AI agent sends to a user
interface while it works, plus the shape of the request that starts it off. That is nearly
all it is. The agent posts nothing new to learn — it answers an ordinary HTTP POST with a
stream of small JSON objects: *I started*, *here is a bit of text*, *I am calling a tool*,
*here is a patch to the shared state*, *I need you to approve this before I continue*, *I
finished*. The interface reads those objects and draws whatever it wants. The point is that
the interface no longer has to know which agent framework is on the other end, and the agent
no longer has to know what the interface looks like. Before AG-UI, every team wrote this glue
by hand — a bespoke websocket message format, or a bag of untyped JSON blobs over
server-sent events — and every framework swap meant rewriting the front end. AG-UI is the
"kitchen sink" attempt to write that glue once. It was created by CopilotKit in May 2025,
it is MIT-licensed, and by mid-2026 it has been implemented by Microsoft, Google, AWS,
IBM, LangChain, Anthropic's agent SDK and about fifteen others — while still being governed
by one company.

**Jargon defined once, used throughout:**

| Term | Meaning |
| --- | --- |
| **Event** | One small JSON object in the stream, e.g. `{"type":"TEXT_MESSAGE_CONTENT","delta":"Hel"}`. |
| **Run** | One execution of the agent, from `RUN_STARTED` to `RUN_FINISHED`. Has a `runId`. |
| **Thread** | A conversation. Many runs share a `threadId`. |
| **SSE** (server-sent events) | A way to keep an HTTP response open and push text down it line by line. |
| **JSON Patch** (RFC 6902) | A tiny standard for "change field X to Y" instead of resending the whole object. |
| **HITL** (human in the loop) | The agent pauses and waits for a person to approve, edit or answer. |
| **Generative UI** | The agent decides what widget to draw, not just what text to write. |
| **MCP** | Model Context Protocol — connects agents to *tools and data*. Anthropic's. |
| **A2A** | Agent2Agent — connects agents to *other agents*. Google's. |
| **A2UI** | A Google spec for describing a *widget* declaratively. Aleph already uses it. |

---

## 1. What problem it solves, and what people did before

An agent doing real work produces a lot of intermediate signal: partial tokens, a plan it is
revising, seven tool calls, a half-finished table, a request for permission. A chat box that
only shows final text throws almost all of that away. So every serious agent product ends up
building a private streaming protocol between its backend and its front end.

That private protocol has three recurring costs:

1. **It is rewritten per framework.** Move from LangGraph to CrewAI and the event shapes
   change, so the React code changes.
2. **It is untyped.** Undeclared fields get hung off events and consumers hope they survive.
   (AG-UI's own docs call this out as the reason typed `metadata` was introduced.)
3. **The hard parts get skipped.** Approvals, resumption after a reload, and "the agent and
   the screen disagree about the current state" are genuinely fiddly, so teams ship the easy
   80% and live with the rest.

AG-UI standardises the message list so the front end is written once. It deliberately does
**not** standardise the transport, the UI toolkit, or the agent's internals.

---

## 2. Current state, August 2026

Verified against `https://github.com/ag-ui-protocol/ag-ui` on 19 Aug 2026.

| Fact | Value |
| --- | --- |
| Repository | `ag-ui-protocol/ag-ui`, MIT licence |
| Created | 7 May 2025 |
| Stars / forks | 15,385 / 1,387 |
| Contributors | 134 |
| Open issues | 342 |
| Last push | **19 Aug 2026** (the day I checked) |
| Commit volume | 43–202 commits/week across the last 12 weeks; ~180 in the most recent |
| Releases | Date-named, near-daily — "Release 2026-08-18", "Release 2026-08-17", … |

**There is no protocol version number.** This surprised me and it matters. The spec has no
"AG-UI 1.2". What is versioned is each package, and they are all still on `0.x`:

| Package | Latest | Date | Aleph has |
| --- | --- | --- | --- |
| `@ag-ui/core` (npm) | **0.0.58** | 14 Aug 2026 | 0.0.57 |
| `@ag-ui/client` (npm) | 0.0.58 | 14 Aug 2026 | 0.0.57 |
| `ag-ui-protocol` (PyPI) | **0.1.20** | 14 Aug 2026 | **0.1.18** (21 Apr 2026) |
| `ag-ui-langgraph` (PyPI) | **0.0.43** | 16 Aug 2026 | **0.0.36** |
| `@copilotkit/runtime` (npm) | **1.68.1** | 14 Aug 2026 | **1.63.2** |

There are also `0.0.59-canary.*` and a `0.1.1-canary.beta.0` line on npm, and the docs
repeatedly reference behaviour that will change "in version 1.0" — so a 1.0 is being
prepared, but no date is published.

**Who backs it.** CopilotKit, a venture-funded startup (reported $27M raise around May 2026).
It is **not** in a neutral foundation — unlike MCP and A2A, which sit under the Linux
Foundation's Agentic AI Foundation. This is the single most important governance fact and I
could find no evidence contradicting it.

**Who implements it.** The repo carries 21 first-party integrations:
`a2a`, `adk-middleware` (Google ADK), `ag2`, `agent-spec`, `agno`, `aws-strands`,
`claude-agent-sdk`, `claude-managed-agents`, `crew-ai`, `langchain`, `langgraph`, `langroid`,
`llama-index`, `mastra`, `microsoft-agent-framework`, `pydantic-ai`, `vercel-ai-sdk`,
`watsonx`, plus starters and a community folder. SDKs exist for TypeScript, Python and .NET
in-tree, with reference docs for Dart, Go, Java, Kotlin, Ruby and Rust.

**Momentum: clearly gaining.** Daily commits, cross-language wire-parity tests landing this
month, and implementations from four hyperscalers. I found no evidence of decline.

**Health caveat: the docs lag the code.** `docs/concepts/architecture.mdx` still says agents
emit "any of the 16 standardized event types". The installed TypeScript enum has **34**, and
the Python one has **35 event classes**. If you read the docs and stop, you will miss more
than half the protocol. This is the single biggest reason a team's mental model of AG-UI goes
stale.

---

## 3. What it can actually do today

This is the full surface, taken from the type definitions rather than the prose docs.

### 3.1 The event list (34 in TypeScript `@ag-ui/core` 0.0.57)

**Lifecycle** — `RUN_STARTED`, `RUN_FINISHED`, `RUN_ERROR`, `STEP_STARTED`, `STEP_FINISHED`

**Text** — `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT`, `TEXT_MESSAGE_END`,
`TEXT_MESSAGE_CHUNK`

**Tool calls** — `TOOL_CALL_START`, `TOOL_CALL_ARGS`, `TOOL_CALL_END`, `TOOL_CALL_RESULT`,
`TOOL_CALL_CHUNK`

**State** — `STATE_SNAPSHOT`, `STATE_DELTA`, `MESSAGES_SNAPSHOT`

**Activity** *(newer, and the one most people miss)* — `ACTIVITY_SNAPSHOT`, `ACTIVITY_DELTA`

**Reasoning** — `REASONING_START`, `REASONING_END`, `REASONING_MESSAGE_START`,
`REASONING_MESSAGE_CONTENT`, `REASONING_MESSAGE_END`, `REASONING_MESSAGE_CHUNK`,
`REASONING_ENCRYPTED_VALUE`

**Deprecated** — `THINKING_START`, `THINKING_END`, `THINKING_TEXT_MESSAGE_START`,
`THINKING_TEXT_MESSAGE_CONTENT`, `THINKING_TEXT_MESSAGE_END`
→ replaced by the `REASONING_*` set; **scheduled for removal in 1.0**.

**Escape hatches** — `RAW` (wrap a foreign system's event), `CUSTOM` (`{name, value}`, your
semantics)

**Draft** — `META` (see §4).

### 3.2 Seven message roles

`developer`, `system`, `assistant`, `user`, `tool`, and two that are new and interesting:

- **`activity`** — a *frontend-only* message. It is explicitly **stripped out of
  `RunAgentInput`** and never travels back to the model. This is how you render a live
  checklist, a search-progress panel or a plan without polluting the conversation the LLM
  sees. It carries an `activityType` discriminator (`"PLAN"`, `"SEARCH"`, …) that tells the
  UI which renderer to use, and a structured `content` payload.
- **`reasoning`** — chain-of-thought, optionally as an opaque `encryptedValue` so
  zero-data-retention providers can carry reasoning across turns without exposing it.

### 3.3 Bidirectional shared state

`STATE_SNAPSHOT` sends the whole state object; `STATE_DELTA` sends an array of RFC 6902 JSON
Patch operations. The client applies patches in order and may ask for a fresh snapshot if it
detects divergence. On the React side this surfaces as a single hook:

```jsx
const { state, setState } = useCoAgent({ name: "agent", initialState: {...} })
```

The agent and the screen read and write the *same object*. This is the feature that turns a
chat box into a workbench, and it is the most under-used thing in the protocol.

`ACTIVITY_SNAPSHOT` / `ACTIVITY_DELTA` apply the identical snapshot+patch pattern to
per-activity progress, addressed by `messageId`. `ACTIVITY_SNAPSHOT` takes a `replace` flag
so a late snapshot cannot clobber a live one.

### 3.4 Interrupts — approvals and resumption (landed 30 Apr 2026)

This is a proper spec, not a hint. The model is **terminal**: the run *ends* on an interrupt,
and the client starts a *new* run carrying the answers.

```
Agent → RunFinished { outcome: { type: "interrupt", interrupts: [ ... ] } }
Client → RunAgentInput { threadId, resume: [ { interruptId, status, payload } ] }
Agent → RunStarted (new runId) → … → RunFinished { outcome: { type: "success" } }
```

An `Interrupt` carries `id`, `reason`, optional `message`, `toolCallId`, **`responseSchema`**
(a JSON Schema for the answer — so the UI can *generate the approval form*), `expiresAt`
(ISO-8601 TTL), and `metadata`.

`reason` has three spec-defined values — `tool_call`, `input_required`, `confirmation` — and
any other string is a valid extension, namespaced as `<framework>:<name>` (e.g.
`langgraph:database_modification`). `core:` is reserved.

**Approve-with-edits** is first-class: the response schema can include `editedArgs`, so a user
can rewrite the email body before approving the send, and the agent executes the edited
arguments.

Eight contract rules are specified, including: a resume must address **every** open interrupt
(no partial resumes); a thread with pending interrupts rejects any input that omits `resume`;
resumes are idempotent on `(threadId, interruptId, status, payload)`; and — the important one
for correctness — **the agent must emit `StateSnapshot` and `MessagesSnapshot` before the
`RunFinished` that carries the interrupt**, so resumption works identically whether the
backend replays context or restores a checkpoint.

As of **19 August 2026** (a commit landed that day, `PNI-317`), resume entries also carry
`metadata`, described in the spec as the place for "a signature proving the human decision was
not tampered with".

### 3.5 Capability negotiation (landed 9 Mar 2026)

`AbstractAgent.getCapabilities()` returns a typed document describing what this agent can do.
Categories: `identity`, `transport`, `tools`, `output`, `state`, `multiAgent`, `reasoning`,
`multimodal`, `execution`, `humanInTheLoop`, and a `custom` escape hatch. Examples:

- `transport: { streaming, websocket, httpBinary, pushNotifications, resumable }`
- `multiAgent: { supported, delegation, handoffs, subAgents: [{name, description}] }`
- `execution: { codeExecution, sandboxed, maxIterations, maxExecutionTime }`
- `humanInTheLoop: { supported, approvals, interventions, feedback, interrupts, approveWithEdits }`
- `multimodal: { input: { image, audio, video, pdf, file }, output: {...} }`

Omitted means *undeclared*, not *unsupported*. The intended use is: **the UI reads this and
adapts** — hide the file-upload button if the agent cannot take files, show an agent picker if
`subAgents` is populated, render an approval affordance only if `interrupts` is true.

This is the piece that most directly matches a plugin-based system, and almost nobody uses it.

### 3.6 Multimodal input

`RunAgentInput` messages accept typed image / audio / video / document parts, each sourced
either from inline data or a URL. Not a bolt-on: it is in the discriminated union.

### 3.7 Metadata on everything (landed 18 Aug 2026)

`metadata` is now declared on every **event**, every **message**, every **tool call**, and
every **resume entry**. Open by key; any JSON value; the key `ag-ui` is reserved. Merge rule
is *last write wins, key by key, never recursive* — so an array or object under a key is
replaced whole. Event metadata merges into the message that event is building, which is how
token usage that only arrives on `TEXT_MESSAGE_END` ends up attached to the right message.

The docs are explicit that this exists because **unknown properties reaching subscribers is
being removed in 1.0**. If you are relying on undeclared fields surviving the pipeline, that
stops working.

### 3.8 Serialization, branching and time travel

`RUN_STARTED` optionally carries `parentRunId` and the exact `input`. Setting `parentRunId`
turns the thread into a git-like append-only log with branches. `compactEvents(events)` folds
a verbose stream into snapshots — concatenating text chunks, collapsing consecutive
`STATE_DELTA`s into one `STATE_SNAPSHOT` — so history can be stored cheaply without losing
observable meaning.

### 3.9 Middleware

`agent.use(m1, m2, m3)` wraps the event stream in RxJS observables. Middleware can transform,
filter, inject and recover. The repo ships six:

| Middleware | What it does |
| --- | --- |
| `mcp-middleware` | Lists tools from MCP servers, injects them into the run namespaced `mcp__{server}__{tool}`, **executes them server-side** in a loop (default cap 32 rounds), and re-runs the agent with the results. Works for *any* AG-UI agent, even one whose framework has no MCP support. |
| `mcp-apps-middleware` | MCP-UI style app surfaces. |
| `a2ui-middleware` | Streams A2UI declarative widgets; stamps a catalog id. **Aleph already uses this.** |
| `a2a-middleware` | Fronts A2A agents. |
| `event-throttle-middleware` | Coalesces high-frequency events on a time window (`intervalMs: 16` ≈ 60 fps) with an optional `minChunkSize`. See §7 on performance. |
| `middleware-starter` | Template. |

Caveat found in the docs: middleware registered with `use()` runs on `runAgent()` but **not**
on `connectAgent()`.

### 3.10 Transports

- **HTTP + SSE** — the default. Note carefully: `HttpAgent` uses **`fetch` with a POST and a
  streaming response body**, *not* the browser `EventSource` API. It therefore *can* set an
  `Authorization` header. This matters enormously for Aleph (§6).
- **HTTP binary (protobuf)** — real and shipped. `@ag-ui/proto` + `@ag-ui/encoder` provide
  `encode`/`decode` and an `EventEncoder` that content-negotiates on the `Accept` header
  against `AGUI_MEDIA_TYPE`, falling back to SSE.
- **WebSocket / push notifications / resumable streams** — these are **declared capabilities
  with no open reference implementation**. `@ag-ui/client` ships exactly one agent class,
  `HttpAgent`. Durable reconnecting threads live in CopilotKit's commercial "Intelligence"
  runtime (Redis locks, channels, `maxReconnectMs`), not in the MIT protocol packages. Treat
  the WebSocket flag as an interoperability hint, not a feature you can adopt.

---

## 4. What changed in the last 6–12 months

Dated from commit history, so these are firm.

| When | Change |
| --- | --- |
| **9 Mar 2026** | **Capability negotiation** added (`docs/concepts/capabilities.mdx`). |
| **2 Apr 2026** | `event-throttle-middleware` created. |
| **30 Apr 2026** | **Interrupts** — "interrupt-aware run lifecycle in TS + Python core SDKs" (PR #1569). `RunFinished.outcome`, `Interrupt`, `RunAgentInput.resume[]`. |
| Spring 2026 | **Activity events and the `activity` message role** — structured frontend-only progress. |
| Spring 2026 | **`REASONING_*` events** supersede `THINKING_*`, adding encrypted reasoning carry-over for zero-data-retention providers. `THINKING_*` deprecated, **removal scheduled for 1.0**. |
| ~Jun 2026 | `MCPMiddleware`, `MCPAppsMiddleware`, `A2AMiddleware` — AG-UI "fronts for" MCP and A2A. |
| **10–18 Aug 2026** | **Metadata on every event and message** (PNI-198), with cross-language .NET↔TypeScript wire-parity fixtures. |
| **19 Aug 2026** | Metadata on resume entries (PNI-317). |
| Ongoing | Repo restructured into `sdks/`, `integrations/`, `middlewares/`, `apps/` (dojo + CLI example), `skills/`. |

**Breaking changes to plan for at 1.0:** removal of `THINKING_*`; removal of undeclared-property
passthrough (use `metadata`); and the interrupt lifecycle is still labelled *draft* in
`events.mdx` even though it is implemented in three SDKs — so its shape can still move.

**In draft, not yet stable:**

- **Meta events** — a `META` event that is *not tied to a run* and can appear anywhere in the
  stream, for thumbs-up/down, tags and annotations that originate from the user or an external
  system rather than the agent.
- **Generative UI** — a two-step pattern where the agent calls a lightweight
  `generateUserInterface(description, data, output)` tool and a *second, dedicated* generator
  turns that into the actual UI. The draft is candid about why: OpenAI caps tool descriptions
  at 1024 characters, `$ref`/`oneOf`/deep nesting are unreliable across providers, and
  "agents dedicated solely to UI generation perform better than agents combining UI generation
  with other tasks."

---

## 5. How it relates to MCP, A2A, A2UI and CopilotKit

**They compose; they do not compete.** The layering is genuinely clean:

| Layer | Protocol | Originator |
| --- | --- | --- |
| Agent ↔ tools and data | **MCP** | Anthropic |
| Agent ↔ agent | **A2A** | Google |
| Agent ↔ user | **AG-UI** | CopilotKit |

A single agent commonly speaks all three. AG-UI has gone further than parallel coexistence: it
has added *handshakes* so an AG-UI client can drive an MCP or A2A agent directly, via
`mcp-middleware` and `a2a-middleware`. That makes AG-UI a superset consumer rather than a
competitor.

**AG-UI vs A2UI — do not confuse these.** The names are unfortunate and the docs carry an
explicit note about it. **A2UI is a way of describing a widget** (declarative, JSONL,
streaming, Google's). **AG-UI is the connection** that carries the widget, the tokens, the
state and the approvals. Aleph uses both, correctly. There are three competing generative-UI
specs and AG-UI carries all of them:

| Spec | Origin | Style |
| --- | --- | --- |
| **A2UI** | Google | Declarative JSONL, streaming, platform-agnostic |
| **Open-JSON-UI** | OpenAI | Open version of OpenAI's internal declarative schema |
| **MCP-UI** | Microsoft + Shopify | iframe-based, extends MCP |

AG-UI's position — "we are not a generative UI spec, we carry yours" — is strategically smart
and, more practically, means Aleph's A2UI investment is not stranded if the widget-spec race
resolves differently.

**AG-UI vs CopilotKit.** This is the one to be careful about.

- `@ag-ui/*` — the protocol. MIT, no licence checking, vendor-neutral in content.
- `@copilotkit/*` — one company's product built on it. `@copilotkit/runtime` instantiates a
  `LicenseChecker` from `COPILOTKIT_LICENSE_TOKEN`. On the code I read, that gates a status
  string in the runtime-info handler rather than core SSE operation — but the hook is there,
  and the "Intelligence" mode (durable threads, realtime channels, Redis locking, thread-name
  generation) is a commercial platform requiring `identifyUser` and a `CopilotKitIntelligence`
  client.

**Is AG-UI a real cross-vendor standard or one vendor's protocol?** Honestly: **both, and the
tension is unresolved.** The *implementations* are unambiguously cross-vendor — Microsoft
Agent Framework, Google ADK, AWS Strands, IBM watsonx, Anthropic's Claude Agent SDK, Vercel AI
SDK, LangChain, Pydantic AI all ship adapters, and there are 134 contributors. The
*governance* is unambiguously single-vendor: CopilotKit owns the repo, sets the roadmap, and
has taken venture money that needs a return. Contrast MCP and A2A, which both moved into the
Linux Foundation. If AG-UI does the same, this risk evaporates; until then it is real but
bounded by the MIT licence and the fact that four hyperscalers now depend on it.

---

## 6. The authentication question — an honest verdict

Aleph's `CLAUDE.md` records under *Known broken*:

> **The runtime bridge does not forward the caller's credential.**
> `apps/copilot-runtime/src/server.ts` constructs `new HttpAgent({ url: AGENT_URL })` with no
> headers. In `oidc` mode the agent endpoint now correctly demands a credential it never
> receives.

**I traced this end to end in the installed code. The diagnosis is wrong, and the real fix is
about three lines on the front end.** Here is the chain.

**Not a protocol gap.** AG-UI is an HTTP POST. Headers are ordinary HTTP headers. There is
nothing to fix in the spec.

**Not a CopilotKit gap.** In the installed `@copilotkit/runtime@1.63.2`,
`dist/v2/runtime/handlers/shared/agent-utils.mjs` line 65 does exactly this, per request, on a
per-request clone of the agent:

```js
agent.headers = mergeForwardableHeaders(agent.headers, request, runtime.forwardHeadersPolicy ?? resolveForwardHeadersPolicy(void 0));
```

The default policy is documented in the same package as: a built-in denylist strips
infrastructure headers (`x-forwarded-*`, `x-real-ip`, `x-vercel-*`, `x-copilotcloud-*`) while
**`authorization` and custom `x-*` headers continue to forward**. So the `HttpAgent({url})`
constructor having no `headers` is irrelevant — those are the *static* service-to-service
headers, and per-request forwarding is a separate, automatic mechanism layered on top.

Corroborating evidence: CopilotKit issue **#5712** is a complaint that *too much* was being
forwarded — "inbound `authorization` and all `x-*` are forwarded and take precedence, with no
opt-out" — filed against `@copilotkit/runtime@1.61.0` / `@ag-ui/client@0.0.57`. It was closed
by PR #5782, which added the denylist and the `forwardHeaders` option now present in 1.63.2.
Header forwarding has been the *default* since at least 1.61.

**It is Aleph's wiring.** `apps/web/src/lib/copilot.tsx`:

```tsx
<CopilotKitProvider runtimeUrl={RUNTIME_URL} renderActivityMessages={[alephA2UIMessageRenderer]}>
```

No `headers`. No `credentials`. **The browser never sends a credential**, so there is nothing
for the runtime to forward. The provider supports both, and the `headers` prop explicitly
accepts a *function* so it can be re-evaluated for token refresh:

```tsx
headers={() => ({ Authorization: `Bearer ${getToken()}` })}
// or, for HTTP-only cookies:
credentials="include"
```

**A second, larger correction.** `CLAUDE.md` also records:

> **SSE cannot carry a bearer token.** `EventSource` cannot set an `Authorization` header…

That is true of the browser `EventSource` API — which is what Aleph's *own* hand-rolled
surface stream uses (`apps/web/src/components/ActivityCard.tsx:129`,
`new EventSource(url, { withCredentials: false })`). It is **not** true of AG-UI.
`HttpAgent` is built on `fetch` (`HttpAgentFetchFn = (url, requestInit) => Promise<Response>`,
with an overridable `requestInit(input)`), streaming the response body and parsing it with
`parseSSEStream`. A `fetch` POST can set any header it likes.

So the AG-UI path has no bearer-token problem at all. Aleph's second-transport problem does.
Moving Aleph's surface stream onto AG-UI's transport would fix the OIDC SSE gap as a side
effect rather than as a project.

**One thing to actually check when wiring it:** the bridge sets `cors: true` in
`createCopilotNodeListener`. Confirm that whatever that expands to echoes `Authorization` in
`Access-Control-Allow-Headers` and sets `Access-Control-Allow-Credentials` when using
cookies — the browser will silently drop the header on the preflight otherwise. And note the
forwarding default is broad: once the browser sends a token, it goes to the agent endpoint.
That is what Aleph wants here, but use `forwardHeaders: { allow: [...] }` if the bridge ever
fronts a third party.

---

## 7. The gap between typical use and real capability

Most teams — Aleph included — use AG-UI as *a way to stream tokens and tool calls into a chat
box*. That is maybe a quarter of it. Here is what is sitting unused, with the evidence it
works.

**a. Activity messages instead of a second transport.** The `activity` role exists precisely
for live structured progress that must not reach the LLM — the docs say it is stripped from
`RunAgentInput` specifically to "avoid LLM confusion". Aleph currently runs a parallel,
hand-rolled `EventSource` connection (`SurfaceStreamProvider`, `ActivityCard`) to do this,
with its own reconnect logic and its own auth hole. `ACTIVITY_SNAPSHOT` + `ACTIVITY_DELTA`
with `activityType: "PLAN" | "SEARCH"` is the same feature, already multiplexed onto the
authenticated connection, already patch-based.

**b. `STATE_DELTA` instead of `STATE_SNAPSHOT`.** Aleph's pinned `ag_ui_langgraph@0.0.36`
emits `STATE_SNAPSHOT` and **never** `STATE_DELTA` — I checked the emitted event types in the
installed package. Every state change therefore resends the whole object. JSON Patch deltas
are the documented path and cost a fraction of the bytes.

**c. `responseSchema` on interrupts → generated approval forms.** Because the interrupt
carries a JSON Schema for the expected answer, the front end can render the approval form
*from the schema* rather than hand-coding one per approval type. A new plugin that needs a new
kind of approval then needs **no front-end change at all**. For a system whose whole thesis is
runtime-added capability, this is the highest-leverage unused feature in the protocol.

**d. `getCapabilities()` as a live plugin manifest.** Nothing says the capability document has
to be static. An agent whose plugin set changes at runtime can report a capability document
that changes with it, and the UI adapts — showing the agent picker when `subAgents` is
populated, the approval affordance when `humanInTheLoop.interrupts` is true, the upload button
when `multimodal.input.pdf` is true. I found no published example doing this dynamically, so
call it a **plausible, unproven pattern** — but the schema was clearly designed for it
("Helps clients build agent selection UIs").

**e. `MCPMiddleware` as a connector host.** It lists tools from MCP servers, injects them
namespaced `mcp__{server}__{tool}`, executes them server-side, and loops the agent with the
results — capped at 32 rounds. The agent framework needs no MCP support of its own. This is a
ready-made way to expose tools to an agent without touching the agent.

**f. `parentRunId` branching.** Set it and the thread becomes an append-only log with
branches, giving deterministic time travel to any prior point. Aleph is building a kernel with
revertible effects; this is the same idea expressed on the UI wire, and it is free.

**g. `compactEvents()`.** Fold a verbose historical stream into snapshots before storing it.

**h. Binary protobuf.** Negotiated with an `Accept` header, no code change on the agent side
beyond using `EventEncoder`.

**i. `metadata` as the provenance channel.** Aleph already stamps `pricing_source` and writes
hash-chained ledger events. Event and message `metadata` is the sanctioned place to carry a
ledger id, trace id or token usage alongside the conversation instead of inventing a side
channel — and resume-entry `metadata` was added on 19 Aug 2026 explicitly for signatures
proving a human decision was not tampered with.

---

## 8. Honest assessment

**Where it is genuinely good.** The event taxonomy is well designed and hard-won — the
snapshot/delta pattern, the start/content/end pattern, the deliberate separation of `activity`
(never seen by the model) from `assistant` (always seen). The interrupt spec is unusually
rigorous for a young protocol: eight numbered contract rules, an idempotency requirement, an
explicit statement that checkpoint-based and replay-based resumption must be observationally
identical. The back-compat discipline is real — `outcome` is optional so old producers still
validate, and there are `BackwardCompatibility_0_0_39/45/47` shims exported from the client.
The `RAW` and `CUSTOM` escape hatches mean you never have to fork the protocol to ship
something. Cross-language wire-parity fixtures landed this month.

**Where it is immature or oversold.**

- **The docs materially misrepresent the protocol** ("16 event types" vs 34). You must read
  the type definitions.
- **No spec version.** "AG-UI" names a moving target. Two systems both claiming AG-UI support
  may not interoperate on interrupts, activity or metadata.
- **The Python SDK trails the TypeScript one**, and the trailing edge is exactly where the
  interesting features are. Interrupts landed 30 Apr 2026; Aleph's pinned `ag-ui-protocol
  0.1.18` is dated 21 Apr 2026 and does **not** contain `Interrupt`, `ResumeEntry` or
  `AgentCapabilities` — I confirmed this by importing it. Nine days on the wrong side of a
  feature boundary.
- **WebSocket / resumable / push notifications are declared but not implemented** in the open
  packages. The durable-connection story is a commercial product.
- **Interrupts are still labelled draft** in `events.mdx` despite three SDK implementations.
- **A 1.0 with removals is coming** and unscheduled.

**Performance — the owner's specific worry.** This is the reassuring part. The stream is the
hot path, not the plugin layer, and AG-UI ships three separate mitigations:

1. `event-throttle-middleware` coalesces `TEXT_MESSAGE_CHUNK`, `TOOL_CALL_CHUNK`,
   `STATE_DELTA`, `ACTIVITY_DELTA`, `REASONING_MESSAGE_CHUNK` and others on a time window —
   `intervalMs: 16` is ~60 fps. Crucially it uses an **allowlist**, so new event types default
   to immediate passthrough and lifecycle/boundary events are never buffered. For token
   streams this cuts delivered event count by one to two orders of magnitude.
2. `STATE_DELTA` (JSON Patch) instead of resending state.
3. Binary protobuf transport, content-negotiated.

The middleware chain itself is RxJS: each middleware is one observable hop **per event**, not
per request. That is a real but small constant, and it is dwarfed by what throttling removes.
Middleware runs on the Node bridge, off the Python request path entirely.

**Realistic cost of depending on it.** Low for the protocol, moderate for the CopilotKit
runtime. The protocol is ~35 typed events over an HTTP POST, MIT-licensed, with schemas in
nine languages and four hyperscalers implementing it. The runtime is a venture-funded
company's product with a licence checker compiled in and a commercial tier for the durability
features.

**Exit path if it stalls.** Good, and worth stating precisely, because it is better than it
looks:

- *Losing CopilotKit but keeping AG-UI* is cheap. Aleph's Node bridge exists for exactly two
  reasons: inject the A2UI tool, and forward headers. Both can be done in Python — the
  upstream LangGraph integration already ships an `a2ui_tool.py` — so the bridge is deletable,
  not load-bearing.
- *Losing AG-UI entirely* costs you the event taxonomy, which is the valuable part. But it is
  MIT-licensed text: vendor the schemas and keep going. You lose future interop, not present
  function.
- Aleph's FastAPI endpoint already speaks the wire format, so the wire is the durable asset
  and both npm packages are replaceable clients of it.

---

## 9. Fit with Aleph specifically

Aleph is a plugin-based workbench where capability is added at runtime, the agent may write
its own plugins, and the interface must show live progress, structured cards, approvals and
streamed results. AG-UI maps onto that better than anything else in this space — but Aleph is
currently using the 2025 subset of it.

**What Aleph has today (verified in tree):**

- `apps/copilot-runtime/src/server.ts` — `@ag-ui/client@0.0.57`, `@copilotkit/runtime@1.63.2`,
  a single static `HttpAgent`, the A2UI middleware configured with `injectA2UITool`, a
  generated catalog and `defaultCatalogId: "aleph://v1"`.
- `apps/api/src/aleph_api/copilotkit_endpoint.py` — `ag_ui_langgraph.add_langgraph_fastapi_endpoint`.
- `apps/web/src/lib/copilot.tsx` — `<CopilotKitProvider>` with `renderActivityMessages`, and
  **no credential**.
- A **second, parallel** SSE transport for surfaces and activity, hand-rolled on `EventSource`.

**Four specific alignments worth acting on:**

1. **Capability negotiation is the plugin manifest Aleph is going to need anyway.** When a
   plugin is activated or rolled back, the agent's capability document changes; the UI reads
   it and adapts. `custom` is an explicit escape hatch for capabilities that do not fit the
   standard categories — that is where Aleph's plugin identifiers belong.
2. **Interrupts are the approval mechanism for self-written plugins.** An agent proposing to
   install code it authored is the textbook case for `reason: "aleph:plugin_install"`, a
   `responseSchema` describing the decision, an `expiresAt`, `approveWithEdits` so a human can
   amend the manifest before approving, and resume `metadata` carrying the signature. Aleph's
   hash-chained `ActionLedgerEvent` and the interrupt's `id` (specified as the "correlation key
   across interrupt, resume, idempotency, and audit") are the same key.
3. **Activity events replace the second transport.** One authenticated connection instead of
   two, patch-based updates, and the OIDC SSE gap closes as a consequence.
4. **`AgentsFactory` matches runtime capability swapping.** `@copilotkit/runtime` v2 accepts
   `agents: ({ request }) => ({...})` — a per-request factory rather than a static record,
   with the incoming `Request` in hand. A newly activated plugin can appear as a resolvable
   agent without a restart. Aleph currently passes a static object.

**The migration hazard to plan around.** Aleph's pinned `ag-ui-protocol 0.1.18` predates
interrupts by nine days. Upgrading Python to `0.1.20` and `ag-ui-langgraph` to `0.0.43` is a
prerequisite for anything in §9.2. Note that on the current adapter, `emit_interrupt_outcome`
defaults to **`False`** for back-compat — LangGraph's native `interrupt()` will otherwise
continue to use the legacy `forwardedProps.command.resume` path. It must be turned on
explicitly, and the client must be able to read `RunFinished.outcome` before you do.

---

## What Aleph should do

1. **Fix the auth hole on the front end, not the bridge.** Add
   `headers={() => ({ Authorization: \`Bearer ${getToken()}\` })}` (or `credentials="include"`)
   to `<CopilotKitProvider>` in `apps/web/src/lib/copilot.tsx`. The runtime already forwards
   `authorization` by default. Verify the bridge's `cors: true` echoes `Authorization` in
   `Access-Control-Allow-Headers`. Then correct the two *Known broken* entries in `CLAUDE.md`:
   the bridge does forward credentials, and AG-UI's SSE is a `fetch` POST that can carry a
   bearer token.
2. **Upgrade the Python side first.** `ag-ui-protocol` 0.1.18 → 0.1.20 and `ag-ui-langgraph`
   0.0.36 → 0.0.43. This is the gate on interrupts, capabilities and activity events. Then
   `@ag-ui/client` 0.0.57 → 0.0.58 and `@copilotkit/runtime` 1.63.2 → 1.68.1.
3. **Adopt interrupts as the single approval mechanism**, including for the agent installing
   its own plugins. Use `responseSchema` so approval forms are generated from the schema — a
   new plugin then needs no front-end work. Namespace custom reasons `aleph:<name>`. Carry the
   ledger id in resume `metadata` and use the interrupt `id` as the ledger correlation key.
   Honour the rule that state and message snapshots are emitted *before* the interrupting
   `RunFinished`.
4. **Retire the hand-rolled `EventSource` surface stream** in favour of `ACTIVITY_SNAPSHOT` /
   `ACTIVITY_DELTA` with `activityType` discriminators. One authenticated transport, patch-based,
   and the OIDC gap closes for free.
5. **Publish a live capability document** from `getCapabilities()` that reflects the currently
   active plugin set, and drive the UI's affordances from it rather than from compile-time
   knowledge. Put plugin identity under `custom`.
6. **Switch `STATE_SNAPSHOT` to `STATE_DELTA`** for incremental updates, and add
   `event-throttle-middleware` at `intervalMs: 16` before worrying about plugin overhead.
7. **Move `agents:` to the per-request factory form** so runtime-activated plugins are
   resolvable without a restart.
8. **Put provenance in `metadata`, not in undeclared fields.** Undeclared-property passthrough
   is being removed in 1.0. Do not write to the reserved `ag-ui` key.
9. **Set `parentRunId`** so the thread is a branchable append-only log — it costs one field and
   it is the same shape as the kernel's revertible effects.
10. **Treat `@ag-ui/*` as the dependency and `@copilotkit/*` as replaceable.** Keep the Node
    bridge thin enough to delete; the upstream `a2ui_tool.py` shows the A2UI injection can move
    to Python.

## What Aleph should avoid

1. **Do not trust the AG-UI prose docs on scope.** They still say 16 event types; there are 34.
   Read `@ag-ui/core`'s type definitions or the Python `events.py`.
2. **Do not build on `THINKING_*`.** Deprecated, removal scheduled for 1.0. Use `REASONING_*`.
3. **Do not design around WebSocket, `pushNotifications` or `resumable` transports.** They are
   declared capabilities with no open implementation; the durable-thread story is CopilotKit's
   commercial Intelligence platform (Redis locks, channels, `identifyUser`). Aleph is docker
   compose and self-hosted — stay on `HttpAgent` over SSE, and use binary protobuf if the
   stream volume ever justifies it.
4. **Do not adopt CopilotKit Intelligence mode.** It requires the hosted platform and pulls
   thread durability, user identity and realtime channels behind a licence.
5. **Do not put an `activity` or `reasoning` message where an `assistant` message belongs, or
   vice versa.** Activity messages are stripped from `RunAgentInput` by design; anything the
   model must see cannot live there.
6. **Do not confuse AG-UI with A2UI.** AG-UI is the connection; A2UI is the widget description.
   Aleph needs both and they are not substitutes. Equally, do not bet the workbench on A2UI
   winning the generative-UI race — AG-UI carries Open-JSON-UI and MCP-UI too, so keep the
   catalog behind the boundary it already has.
7. **Do not implement partial resumes.** The spec requires a single `resume` array addressing
   *every* open interrupt; agents must `RunError` otherwise. Do not invent a per-interrupt
   resume endpoint.
8. **Do not leave `emit_interrupt_outcome` at its default `False`** once the client can read
   `RunFinished.outcome` — and do not turn it on before then, or resumption silently breaks.
9. **Do not assume the protocol is stable because the implementations are broad.** There is no
   spec version, packages are `0.x`, interrupts are still labelled draft, and a 1.0 with
   removals is coming. Pin exact versions and re-verify on upgrade.
10. **Do not run two event transports.** The parallel `EventSource` stream is where Aleph's
    auth hole, its reconnect logic and its duplicated state actually live. One stream.
11. **Do not widen header forwarding beyond what is needed.** The default forwards
    `authorization` and all custom `x-*`; issue #5712 was filed because that was too much. Use
    `forwardHeaders: { allow: [...] }` if the bridge ever fronts anything untrusted.
