# Aleph's agent + interface stack — keep, replace, drop, and how the workbench grows

**Written 19 August 2026.** Every version number and API claim below was checked live against
PyPI, the npm registry, the GitHub API, or the actual bytes in Aleph's `node_modules` on that
date. Where I could not verify something I say so.

---

## In one paragraph

Aleph currently depends on four outside things to get an agent's work onto a screen: **A2UI**
(a JSON format the agent uses to describe a card or a panel), **AG-UI** (the event stream that
carries the agent's work to the browser), **CopilotKit** (a React library plus a small Node
server that sits between the two), and **LangGraph/Deep Agents** (the library that runs the
agent loop itself). My recommendation is: **keep A2UI as a format, keep AG-UI as the wire, keep
Deep Agents as one swappable piece behind Aleph's own types, and drop CopilotKit's Node server
entirely** — because the one job it exists to do now runs in Python, and the browser-facing
protocol it speaks is five documented HTTP routes that FastAPI can serve directly. Doing that
deletes a service, a duplicated catalog, an unbounded-memory bug and two of the three
"Known broken" auth entries in `CLAUDE.md`. The larger point is separate from any dependency:
Aleph's interface is **hardcoded in four specific places** — the rail's five-item list, the
per-card React implementations, the closed action-name union, and the single hand-written
approval card — and those four places are exactly what a plugin system has to make into data.
Everything else in the shell (the panes, the surface stream, the server-composed surfaces) is
already runtime-driven and closer to right than it looks.

---

## Vocabulary, defined once

Terms are used precisely throughout. If you already know these, skip to §1.

- **A2UI** — a JSON dialect an agent emits to describe a user interface. It names components
  from a **catalog** the host published in advance, so it is as safe as data and as expressive
  as code. It is a *content* format. Current stable spec **v0.9**; **v1.0** is a release
  candidate with no shipping renderer yet.
- **Catalog** — the list of components an agent is permitted to name, plus their prop schemas.
  Aleph's is `packages/aleph-a2ui/src/aleph_a2ui/catalog.json`: 21 components, 9 primitives,
  20 actions.
- **AG-UI** — a *transport* protocol: about 35 typed JSON events (text chunks, tool calls, state
  patches, run lifecycle, interrupts) streamed over one HTTP POST. It is what carries A2UI (and
  everything else) from an agent to a front end. **A2UI and AG-UI are not alternatives.** One is
  what you say; the other is how it travels.
- **CopilotKit** — a company and a product built on AG-UI. Two halves matter here: a React
  package (`@copilotkit/react-core`) and a Node server (`@copilotkit/runtime`). Aleph runs both.
- **LangGraph** — the library that executes an agent as a graph of steps, with checkpointing.
  **Deep Agents** — a library on top of LangGraph giving planning, subagents, a virtual
  filesystem and skills. Aleph's assistant is a Deep Agent.
- **Surface** — in A2UI, one addressable UI region with an id. Aleph's panes *are* A2UI surfaces:
  the pane id is the wire `surfaceId`.
- **Interrupt** — an AG-UI concept: the agent's run *ends*, carrying a request for a human
  decision; the client starts a *new* run carrying the answers. This is how approvals work.
- **Capability document** — a typed JSON blob an agent publishes saying what it can do (tools,
  approvals, multimodal input, sub-agents). Fetched by the browser at connect time.
- **Kernel / capability / plugin** — Aleph's own layer (`packages/aleph-kernel`). A capability
  declares what it provides, what it requires, how to set itself up, and — mandatorily — a
  **probe** proving it works. Dynamically registered capabilities get a `PluginId`; boot-manifest
  ones never do, which is why an agent cannot name core capability to remove it.

---

## 0. What is actually installed today

Verified from `package.json`, `uv.lock` and `node_modules` on 19 Aug 2026.

| Thing | Aleph pins | Current | Gap |
|---|---|---|---|
| `@a2ui/react` | 0.10.0 (`^0.10`) | 0.10.2 (17 Jul 2026) | 2 patches |
| `@a2ui/web_core` | 0.10.0 (`^0.10`) | 0.10.6 (3 Aug 2026) | 6 patches |
| `@copilotkit/react-core` | **1.58.0** | 1.68.1 (14 Aug 2026) | **10 minors** |
| `@copilotkit/runtime` | **1.63.2** | 1.68.1 | **5 minors** |
| `@ag-ui/client` | 0.0.57 | 0.0.58 (14 Aug 2026) | 1 patch |
| `ag-ui-protocol` (py) | **0.1.18** (21 Apr 2026) | 0.1.20 (14 Aug 2026) | **predates interrupts by 9 days** |
| `ag-ui-langgraph` (py) | **0.0.36** (22 May 2026) | 0.0.43 (16 Aug 2026) | **3 months** |
| `copilotkit` (py) | 0.1.91 | 0.1.95 (16 Aug 2026) | 4 patches |
| `deepagents` | 0.6.x (`>=0.6,<0.7`) | 0.7.7 (18 Aug 2026) | one breaking minor |

Two of these gaps are load-bearing, not cosmetic:

- **`ag-ui-protocol 0.1.18` predates the interrupt spec.** Interrupts landed 30 Apr 2026; Aleph's
  pin is from 21 Apr. Approvals, `responseSchema`-generated forms and durable resume are all on
  the wrong side of that line.
- **`@copilotkit/runtime 1.63.2` predates the 1.66.3/1.66.4 memory fix.** In 1.63.2,
  `InMemoryAgentRunner` keeps every thread, every run, and a `ReplaySubject(Infinity)` per run in
  a process-global `Map`. This is a live leak in a service Aleph runs, not a theoretical one.

### The shell as it stands

```
 rail        reading region (up to 3 panes)          assistant dock
┌────┬────────────────────────────────────────────┬──────────────────┐
│ ℵ  │ ┌────────────┐┌────────────┐┌────────────┐ │  CopilotChat     │
│ 📖 │ │ Wiki pane  ││ Notes pane ││ Briefs pane│ │  (CopilotKit)    │
│ 📚 │ │ A2UI       ││ A2UI       ││ A2UI       │ │                  │
│ 📝 │ │ surface    ││ surface    ││ surface    │ │  useAgentContext │
│ 🧪 │ └────────────┘└────────────┘└────────────┘ │  useFrontendTool │
│ 📋 │        ▲ one multiplexed SSE stream         │        ▲         │
└────┴────────────────────────────────────────────┴────────┼─────────┘
   ▲                     │                                 │
   │ SURFACE_TABS        │ EventSource                     │ fetch POST
   │ (5 hardcoded)       ▼                                 ▼
                 aleph-api :8000                    copilot-runtime :4000 (Node)
                                                            │
                                                            ▼
                                                     aleph-api :8000 /copilotkit
```

The pane model is genuinely good and better than upstream: one multiplexed SSE connection with a
server-stamped monotonic `seq` so every pane shares one total order, and a pane that owns no
transport of its own. Keep all of that.

But there are **four live transports and six pollers**:

| Transport | Mechanism | Ordering | Auth |
|---|---|---|---|
| Surfaces | `EventSource` → `/surfaces/stream` | monotonic `seq` | none possible |
| Agent phase events | `EventSource` → `/agent-events/stream` | none | none possible |
| Wiki change signals | `EventSource` → `/changes` | none | none possible |
| Agent run | `fetch` POST → Node → API | AG-UI event order | possible, not wired |
| Pipeline / runs / actions / drawers | `react-query` polls at 1.5s–30s | none | header, works |

Four independent orderings means the pipeline strip and a pane can display mutually inconsistent
states with nothing detecting it — which is precisely the argument `SurfaceStreamProvider`'s own
file header makes for multiplexing, applied at only one of the four places it applies.

---

## 1. Keep, replace, or drop — one verdict each

The first thing to get right is that these four are not competitors. They stack:

```
  WHAT THE AGENT SAYS        A2UI          "draw a ClaimCard with these props"
  ─────────────────────────────────────────────────────────────────────────
  HOW IT TRAVELS             AG-UI         ~35 typed events over one HTTP POST
  ─────────────────────────────────────────────────────────────────────────
  WHO SPEAKS IT              CopilotKit    a Node proxy + React hooks
  (replaceable glue)         @ag-ui/client the MIT client the React hooks use
  ─────────────────────────────────────────────────────────────────────────
  WHAT PRODUCES THE WORK     Deep Agents   plan, delegate, call tools
                             LangGraph     execute as a checkpointed graph
  ─────────────────────────────────────────────────────────────────────────
  WHAT OWNS THE SYSTEM       aleph-kernel  mount / probe / revert capability
```

Only the third row is glue. That is the row to cut.

### A2UI — **KEEP the format. Treat `@a2ui/*` as replaceable.**

**Why keep.** It is the only serious contender for "portable declarative UI in an app you own."
The repo is healthy on code (16,155 stars, Apache-2.0, pushed the day I checked). Aleph is on the
current stable spec (v0.9) and its integration is in places better than upstream's own — the SSE
multiplexer and the single-editable-catalog rule with a generator and a drift check are both
right. The competing standard, **MCP Apps** (Final since Jan 2026, OpenAI + Anthropic behind it),
solves a *different* problem: rendering UI inside a chat client you don't control. Aleph owns its
frontend. Different slot.

**Why not a stronger commitment.** A2UI has **no foundation, no governance document, no releases
and no tags**, and its contributor list is dominated by Google — while the sibling protocol A2A
joined the Linux Foundation's AAIF in August 2026 and A2UI did not. That is the single biggest
governance risk in this stack.

**What makes the exit cheap, and must stay true.** Aleph's 21 card components import nothing from
`@a2ui/*`. The A2UI coupling lives in exactly four files: `aleph-catalog-v09.tsx`,
`A2UISurfaceView.tsx`, `SurfaceStreamProvider.tsx`, `A2UIRightPanel.tsx`. On the server side
Aleph **already owns its wire producers** — `messages.py` hand-rolls `createSurface` /
`updateComponents` / `updateDataModel` in 82 lines. So the only genuinely expensive thing Aleph
would have to rebuild is the *renderer*: the JSON-Pointer data binder and the incremental
surface model in `@a2ui/web_core`. Keep that boundary; add a lint or a test that fails if a card
component imports `@a2ui/*`.

**Three defects to fix regardless of any other decision.**

1. **Two catalogs share one id and disagree about functions.** Verified in tree:

   ```
   apps/web/src/a2ui/A2UISurfaceView.tsx:57       new Catalog("aleph://v1", [...impls], [])
   apps/web/src/a2ui/SurfaceStreamProvider.tsx:35 new Catalog("aleph://v1", [...impls],
                                                    [...basicCatalog.functions.values()])
   ```

   `lib/copilot.tsx:37` builds the **chat** renderer from the function-less one. So a surface
   using `formatString`, `required` or `email` renders in a pane and fails in chat, under one
   shared identifier. `ALEPH_V09_CATALOG_ID` is also declared twice, once per file. This is the
   same defect class the repo already burned a work package on with the three hand-maintained
   catalogs.

2. **The catalog has two identities.** `catalog.json` carries `catalogId: "aleph-v1"` *and*
   `agentCatalogId: "aleph://v1"`, and neither is a URL, which is what A2UI catalog ids are meant
   to be. Two names for one thing in a codebase whose stated rule is that things which can
   disagree eventually do.

3. **A fully built surface is unreachable.** `GroundingSurface` has a React implementation, a
   catalog entry, a registered component api, a server builder, and a `grounding` entry in the
   server's `_PANE_KINDS`. Nothing in the UI can open it, because the only source of pane kinds
   on the client is `SURFACE_TABS`, a five-element const that does not contain it. The Rail's own
   docstring says a rail was chosen because *"Aleph needs more surfaces than that — a grounding
   inspector, a dispute queue, a claim view — and none of them could have been added"* — and then
   the code reintroduced the ceiling one file over. This is the plugin problem in miniature, and
   it is already shipping.

### AG-UI — **KEEP, and use roughly four times as much of it.**

**Why keep.** It is the closest thing to a standard for this job, with 21 first-party
integrations spanning Microsoft, Google, AWS, IBM, Anthropic and Vercel. `@ag-ui/client` is MIT,
630 KB unpacked, and depends on zod ^3 — the same major Aleph already carries an alias for
because of `@a2ui/web_core`. Governance is single-vendor (CopilotKit, VC-funded) and there is no
spec version number, but the licence and the breadth of adoption bound that risk.

**Why "four times as much."** Aleph uses the 2025 subset: stream tokens, stream tool calls,
render cards. The protocol shipped 34 event types; the docs still say 16. Unused and directly
relevant: **interrupts with schema-generated approval forms**, **capability negotiation**,
**activity messages** (structured progress that is stripped from the model's context by design),
**`STATE_DELTA`** (Aleph's pinned adapter emits only whole-object snapshots), **typed metadata on
every event**, **`parentRunId` branching**, and **event throttling**. §3–§6 build on these.

**Correct two entries in `CLAUDE.md`.** Both are misdiagnosed:

- *"The runtime bridge does not forward the caller's credential."* It does. In the installed
  1.63.2, `dist/v2/runtime/handlers/shared/agent-utils.mjs:65` merges forwardable headers per
  request and forwards `authorization` by default. The gap is that
  `<CopilotKitProvider runtimeUrl={...}>` in `apps/web/src/lib/copilot.tsx` passes no `headers`
  and no `credentials`, so **the browser never sends a token** and there is nothing to forward.
  Three lines on the front end.
- *"SSE cannot carry a bearer token."* True of the browser `EventSource` API — which is what
  Aleph's own three hand-rolled streams use. **Not true of AG-UI**, whose `HttpAgent` is a `fetch`
  POST with a streaming body and can set any header. So moving Aleph's surface stream onto AG-UI's
  transport closes the OIDC SSE gap as a side effect rather than as a project.

### CopilotKit — **DROP the Node service. Demote the React package to a thin, deletable client.**

This is the strongest verdict in the document, and it rests on facts I verified in Aleph's own
`node_modules`, not on the docs.

**The Node service's only stated job now runs in Python.** `apps/copilot-runtime/src/server.ts`
is 75 lines. Its file comment says plainly: *"This is where A2UI tool injection lives."* That is
the whole justification. But `ag-ui-langgraph 0.0.43` ships `get_a2ui_tools(A2UIToolParams)` —
verified by downloading the wheel — with `catalog`, `default_catalog_id`, `default_surface_id`,
`guidelines`, `tool_name` and `recovery` knobs, and its own docstring explains that progressive
surface painting is *free* on LangGraph because the subagent's `astream` already surfaces the
nested tool-call argument deltas as AG-UI events.

**The browser-facing protocol is five routes.** I read the route matcher in the exact version
Aleph runs (`dist/v2/runtime/core/fetch-router.mjs`, 1.63.2):

```
GET   {base}/info                              handshake
POST  {base}/agent/{agentId}/run               the run — SSE of AG-UI events
POST  {base}/agent/{agentId}/connect           reattach / replay
POST  {base}/agent/{agentId}/stop/{threadId}   cancel
POST  {base}/agent/{agentId}/suggest           optional, stateless suggestion run
```

Everything else the router matches — `threads/*`, `transcribe`, `cpk-debug-events` — is optional
or Intelligence-only. FastAPI can serve those five. That is not reimplementing CopilotKit; it is
implementing a five-route protocol the MIT client already speaks.

**A finding that changes the plugin design.** I traced how A2UI is enabled today, and it runs
**backwards**. `copilotkit/copilotkit_lg_middleware.py` decides whether to inject the A2UI tool
by reading `state["ag-ui"]["inject_a2ui_tool"]`, which `ag_ui_langgraph/agent.py:1005` copies
from `forwardedProps.injectA2UITool` — a flag the **Node bridge** sets. And it resolves *which
catalog* from `state["ag-ui"]["a2ui_schema"]`, which `agent.py:987` extracts from an AG-UI
`context` entry whose `description` matches a magic constant stamped by the Node middleware.

So today: **the client tells the server whether A2UI is on, and ships it the catalog on every
run.** For a workbench whose plugin registry lives on the server, that is exactly inverted. The
server owns the mounted plugin set; it must own the catalog. Binding the catalog server-side is
not just a step toward deleting the bridge — it is a precondition for plugins working at all.

**What CopilotKit gives back, and whether it is worth it.** `/info` already calls
`agent.getCapabilities()` per agent and returns the result under `agents[name].capabilities` —
verified in 1.63.2. And `resolveAgents(runtime.agents, request)` supports a **per-request agents
factory**, so a newly activated plugin can be a resolvable agent with no restart. Both of these
are exactly what Aleph needs. But both are AG-UI features that CopilotKit merely *plumbs*; Aleph
can serve the same `/info` document from FastAPI, computed from `AgentPluginAPI.inspect()`.

**On the React package.** `@copilotkit/react-core@1.58.0`'s `dist/` is 5.4 MB on disk (~2.96 MB
gzip for the `/v2` entry point) because it drags in `streamdown`, `katex`, `react-markdown`,
`lit`, `@jetbrains/websandbox`, `@copilotkit/web-inspector`, `@copilotkit/runtime-client-gql` and
a virtualiser. Aleph renders its own shell, its own cards, its own everything except the chat
bubble list. **The `./v2/headless` export already exists in the installed 1.58.0** — I checked
the exports map — so the bundle cut is available today, without an upgrade. Switching to it means
drawing Aleph's own chat list and composer against the `@ag-ui/client` subscriber API: realistically
500–800 lines. Given Aleph already ships CSS variable overrides to fight `CopilotChat`'s styling,
that is a trade worth making, but it is a Stage-3 item, not a Stage-0 one.

**Do not build on `selfManagedAgents`.** Sources contradict each other: the docs site calls it
part of the Enterprise Intelligence offering and says to talk to sales; the repo's own in-tree
skill files call it and `agents__unsafe_dev_only` *"dev-only aliases of each other. Not
production-safe."* I trust the in-tree skills — they ship with the code and name their sources.
Either way it is not a foundation. If you want to skip the runtime, own the five routes.

### LangGraph / Deep Agents — **KEEP, DEMOTE, do not build the kernel on it.**

**Keep.** Aleph's usage — `create_deep_agent`, six subagents, `CompositeBackend` routing
`/memories/` to a per-project Postgres `StoreBackend` and `/skills/` to a read-only filesystem, a
Postgres checkpointer — is current and well-shaped, not dated. The parts Deep Agents provides
that are genuinely hard to write are compaction policy, offload policy and interrupt resumption
across a checkpointer. (For calibration: Nous Research's hermes-agent spent roughly 12,000 lines
on compaction alone.)

**Demote.** Aleph should own the types the rest of the system sees — a run, a step, a tool call,
an approval request, a streamed event — and adapt Deep Agents to them. That is what makes the exit
path real rather than theoretical.

**Do not build the kernel on it.** Deep Agents assembles middleware at `create_deep_agent` time.
There is no mount, unmount or revert. Aleph's premise needs exactly those, and `aleph-kernel`
already has them. The precedent is decisive: **none of the five production systems in
`~/Documents/code/inspiration/` uses langchain, langgraph or deepagents** — and the most
architecturally similar one, deepseek-harness, makes its 1,643-line agent loop **a plugin on a
composability kernel**. Loop inside kernel, not kernel inside loop.

**One bug-in-waiting to fix now.** `_gateway_chat_model` builds `ChatOpenAI(model=..., ...)` and
never passes `profile=`. `langchain_openai` resolves model metadata from a static table keyed on
model *name*; behind a gateway with operator-chosen names it finds nothing, and Deep Agents'
summariser falls back to a hardcoded 170,000-token trigger. Point a gateway at a 32k model and
the agent overflows the context before the framework ever tries to compact. Aleph's own
`aleph_models.discovery` already reads exactly the fields `ModelProfile` wants
(`max_input_tokens`, `max_output_tokens`, tool calling, structured output). Hand them over.

### The honest "use none of them" option

I took this seriously because three of the four are already thin enough to leave.

| Layer | Cost to own | Verdict |
|---|---|---|
| Wire (AG-UI) | ~20 event types is a week. The *contract rules* — eight numbered interrupt rules, snapshots-before-interrupt, idempotent resumes, no partial resumes — are what took three SDKs a year. | **Don't.** Interop with LangGraph, CrewAI, Mastra and Pydantic AI is free today and is not worth re-earning. |
| A2UI producers | Already owned — 82 lines in `messages.py`. | **Already none.** |
| A2UI renderer | JSON-Pointer binder + incremental surface model + StrictMode-safe processing. Weeks, and it is subtle. | **Keep the dependency**, keep the boundary at four files. |
| CopilotKit Node | 75 lines of glue over five routes. | **Own it.** Delete the service. |
| CopilotKit React | Chat list, composer, streaming text renderer. 500–800 lines. | **Own it eventually**, headless in the meantime. |
| Agent loop | ~1,600 lines for the loop; compaction is the expensive part. | **Keep Deep Agents**, behind Aleph's types. |

Net: the right answer is not "none of them," it is **"none of the glue."** Own the wire's
producers and the five routes; keep the two libraries whose replacement cost is genuinely high
(the A2UI renderer, the agent loop's context management); make sure both can be swapped.

---

## 2. Is the Node bridge justified?

**No. It was justified when it was written and no longer is.**

```
 TODAY                                         TARGET
 ─────                                         ──────
 browser                                       browser
   │ fetch POST /api/copilotkit/agent/…/run      │ fetch POST /copilotkit/agent/…/run
   ▼                                             │  (Authorization: Bearer …)
 copilot-runtime :4000  (Node, 75 LOC)           │
   • injects render_a2ui tool                    │
   • forwards the catalog it was given           │
   • decodes + re-encodes EVERY SSE event        │
   • InMemoryAgentRunner leaks (pre-1.66.3)      │
   │ HttpAgent → no browser credential           │
   ▼                                             ▼
 aleph-api :8000 /copilotkit/agent/assistant   aleph-api :8000
   • ag_ui_langgraph endpoint                     • serves /info, /run, /connect, /stop
   • CopilotKitMiddleware reads the flag          • binds the catalog from the KERNEL
     and the catalog the CLIENT sent              • get_a2ui_tools(...) server-side
```

The case for deletion, in the order the facts matter:

1. **Its job moved to Python.** `get_a2ui_tools` (verified in `ag-ui-langgraph 0.0.43`) plus
   `CopilotKitMiddleware(a2ui_params=...)` (verified in `copilotkit 0.1.95`) do the injection.
2. **Its protocol is five routes**, read from the matcher in the version Aleph runs.
3. **It runs the catalog the wrong direction.** The client currently supplies both the
   `injectA2UITool` flag and the catalog schema on every run. The server owns the plugin set;
   the server must own the catalog. Removing the bridge is the natural moment to invert this.
4. **It costs a full decode/re-encode of every SSE event in a JS event loop** — a per-token cost
   on the hot path — plus a second process that can restart, OOM or drop a connection.
5. **It is the reason two `CLAUDE.md` "Known broken" entries exist**, and both dissolve when the
   browser talks to the API directly: there is no hop that needs to forward a credential.
6. **It ships four model-provider SDKs into the image.** `@copilotkit/runtime` depends directly
   on the OpenAI, Anthropic, Google and Vertex SDKs to support `BuiltInAgent`, which Aleph must
   never use. Aleph's "everything goes through the gateway" rule is review-enforced only; this is
   how such a rule quietly dies.
7. **The version it is pinned at leaks memory.** 1.63.2 predates the 1.66.3/1.66.4 bound on
   `InMemoryAgentRunner`.

**The one thing to be careful about.** `handleGetRuntimeInfo` is where `getCapabilities()` is
called and where `a2uiEnabled` is advertised. Whatever serves `/info` from FastAPI must produce
the same shape, because the browser client feature-gates off it. That document is not overhead —
it is the plugin manifest (§4).

**If the bridge somehow stays**, upgrade `@copilotkit/runtime` to ≥1.66.4 today, and do **not**
upgrade `@copilotkit/react-core` past 1.58 while `apps/web/src/lib/copilot.tsx` still passes
`renderActivityMessages` by hand — current versions auto-inject the A2UI renderer off `/info`,
and the repo flags manual wiring as a HIGH-severity double-render race.

---

## 3. What the interface should be able to do, and cannot

Every item below is grounded in a capability I verified exists, in a version that is published.

**1. Show what it can do *right now*, not what it could do when it was compiled.**
AG-UI's capability document reaches the browser at connect time and `handleGetRuntimeInfo`
already calls `getCapabilities()` per agent per request. Aleph's Python agent implements no such
method, and the rail is `SURFACE_TABS`, a five-element const. *Today:* five fixed icons; a
built, server-buildable `grounding` surface that nothing can open.

**2. Generate an approval form from a schema it has never seen.**
An AG-UI `Interrupt` carries `responseSchema` — a JSON Schema for the expected answer — and
`ag_ui_langgraph/interrupts.py::lg_interrupt_to_agui` maps it straight through from a plain
LangGraph `interrupt({"reason": ..., "responseSchema": {...}})`. One generic schema-driven form
renderer then covers every approval type any future plugin invents, with **no frontend change**.
*Today:* one hand-written `ApprovalCard` with a fixed prop set and a fixed approve/reject pair.
This is the single highest-leverage unused feature in the whole stack for a plugin workbench.

**3. Let a human amend before approving.** `approveWithEdits` is first-class in the interrupt
spec: the response schema can carry `editedArgs` and the agent executes the edited arguments.
*Today:* approve or reject, nothing between. For an agent proposing to install code it wrote
itself, "approve, but change the manifest first" is the interaction that matters most.

**4. Survive a reload in the middle of an approval.** A LangGraph `interrupt()` lives in the
checkpointer; the pause survives the page. *Today:* `useFrontendTool` handlers die with the
component, and a missing `respond()` on a reject path hangs the run forever.

**5. Show live structured progress without polluting the model's context.** The AG-UI `activity`
message role is **stripped from `RunAgentInput` by design**, with an `activityType` discriminator
telling the UI which renderer to use, and snapshot/delta events addressed by `messageId`.
*Today:* a parallel `EventSource` for phase events, a 1.5-second poll for run rollups, a third
`EventSource` for change signals, and a 15-second poll for card actions that is then **fed back
into `useAgentContext`** — meaning it is serialised into the prompt on every single turn.

**6. Have one ordering for everything on screen.** *Today:* four transports, one of which
(`surfaces`) has a monotonic sequence and three of which have none.

**7. Watch a long document assemble as it is written.** `StateStreamingMiddleware(StateItem(
state_key, tool, tool_argument))` — verified in `ag-ui-langgraph 0.0.43` — projects partially
streamed tool arguments into shared state. A report body or a claim set fills the reading region
live. *Today:* a report appears whole when the tool returns.

**8. Branch and rewind a thread.** `RUN_STARTED.parentRunId` turns the thread into an
append-only log with branches and deterministic time travel — the same shape as the kernel's
revertible effects, for the cost of one optional field. *Today:* no UI expression of the kernel's
central idea.

**9. Carry provenance all the way to the pixel.** Typed `metadata` landed on every event,
message, tool call and resume entry between 10 and 18 Aug 2026, with resume-entry metadata added
specifically as the place for "a signature proving the human decision was not tampered with."
That is the sanctioned channel for Aleph's ledger id, trace id, token usage and `pricing_source`.
*Today:* the cost drawer polls a separate endpoint; a rendered claim carries no trace of what it
cost or which run produced it.

**10. Accept a file in the conversation.** `RunAgentInput` messages take typed image / audio /
video / document parts in a discriminated union. *Today:* a separate upload modal POSTing to a
REST route; the chat cannot take a PDF.

**11. Explain why a model cannot do something.** Aleph auto-discovers models from whatever
gateway is connected and already probes before binding. The interface should say *"12 of 18
models rejected: 9 unknown capabilities, 2 context window, 1 unreachable"* with a re-probe
button. *Today:* a picker.

---

## 4. The hard one — how the interface grows when a plugin is mounted

This is the question the whole architecture is for. A hardcoded front end defeats a plugin
kernel, so the chain from *"the kernel mounted a capability"* to *"there are pixels for it"* must
be **data at run time at every link**. There are five links, and Aleph has already solved two.

```
  kernel mounts capability
  (PluginId minted, probe passed)
            │
    ┌───────┴─────────────────────────────────────────────────────┐
    │                                                             │
 ①  DISCOVERY      the UI learns it exists          ← capability document at /info
 ②  VOCABULARY     the agent learns it can draw it  ← catalog assembled at run time
 ③  DELIVERY       the renderer can draw it         ← THE HARD LINK
 ④  ACTIONS        a click reaches its handler      ← namespaced actions via the kernel
 ⑤  APPROVAL       a human can gate it              ← interrupt + responseSchema
    │                                                             │
    └───────┬─────────────────────────────────────────────────────┘
            ▼
  and, symmetrically: deactivate must REMOVE the interface, visibly
```

### ① Discovery — solved by a document Aleph is not yet writing

The browser fetches `/info` once per connection and gets, per agent, whatever
`getCapabilities()` returned. The document is typed and has categories for `identity`,
`transport`, `tools`, `output`, `state`, `multiAgent`, `reasoning`, `multimodal`, `execution`,
`humanInTheLoop`, and a **`custom` escape hatch** — which is where Aleph's plugin identifiers
belong. Omitted means *undeclared*, not *unsupported*.

Nothing says this document must be static. Aleph's `AgentPluginAPI.inspect()` already returns,
for every capability: name, state, protected, provides, requires, `plugin_id` (None for anything
unaddressable), `removable`, and `would_also_stop` — the precomputed blast radius. **That is
already a capability document.** It just needs a projection into the AG-UI shape, recomputed per
request. `resolveAgents(agents, request)` (verified in 1.63.2) exists precisely so the agent set
can be computed per request rather than registered at boot.

I found no published example driving this dynamically. Call it a **plausible but unproven
pattern** — the schema was clearly designed for it ("Helps clients build agent selection UIs"),
and Aleph would be early. That is a reason to ship it behind a version field, not a reason not to.

**The shell then reads from the document instead of from a const:**

```
  SURFACE_TABS = ["Wiki","Library","Notes","Hypotheses","Briefs"]   ← delete this
        ↓
  rail entries    = capabilities.custom.aleph.surfaces[]   (id, label, icon, plugin_id)
  approval UI     = capabilities.humanInTheLoop.interrupts
  upload button   = capabilities.multimodal.input.pdf
  agent picker    = capabilities.multiAgent.subAgents[]
  pane kinds      = the same list the server's _PANE_KINDS is built from
```

Note the last line. `_PANE_KINDS` is a `frozenset` of seven strings on the server and
`SURFACE_TABS` is five on the client, and **they already disagree** — which is how the grounding
surface became unreachable. One list, derived from the mounted plugin set, published once.

### ② Vocabulary — the catalog must be assembled, not imported

Today the catalog is a committed JSON file, generated into a TypeScript module, compiled into
the bundle, and — for the chat path — shipped from the browser to the server on every run. Four
copies of one truth, one of which travels the wrong way.

Target: **one catalog per plugin, assembled server-side from the mounted set.**

```
kernel mounted set ──► catalog assembler ──► one merged catalog document
                              │                    │
                              │                    ├─► agent system prompt
                              │                    │   (A2UI generate_system_prompt(
                              │                    │    allowed_components=[...]) —
                              │                    │    the guidance ships INSIDE the
                              │                    │    catalog's `instructions` field
                              │                    │    so it cannot drift from it)
                              │                    │
                              │                    └─► browser, at connect time
                              │
                              └─► validator cache, one compiled validator per catalogId
```

Two things this buys immediately: the "here are your components" prompt section stops being
hand-written (it is currently ~40 lines inside `SYSTEM_PROMPT` naming TableCard, ChartCard,
HypothesisCard by hand — a drift source), and the catalog stops being something the client can
influence.

Restructuring `catalog.json` into a real A2UI `catalog_definition` is the prerequisite. Aleph's
current shape describes a `{type, id, props}` wrapper; A2UI's is a components map of JSON Schema
definitions with a `component` const, plus a `functions` map, `instructions`,
`allowedParents`/`allowedChildren`, `protocolVersion`, and a **URL** `catalogId`. They are not
the same document, which is why Aleph cannot today use the conformance suite, the validator, the
SDK prompt generator, or inline catalog delivery.

### ③ Delivery — the hard link, and the honest answer is three mechanisms

**The constraint:** on A2UI v0.9, a component the renderer did not compile in cannot be drawn.
Adding one component today requires editing `catalog.json`, running `gen_catalog.py`,
hand-writing a `createComponentImplementation` with a zod v3 schema, writing the React view, and
**rebuilding and redeploying the frontend**. An agent cannot do that at runtime. This is not a
criticism of Aleph's code — it is the only thing the v0.9 renderer supports.

There are exactly three ways out, and the design should use all three, deliberately:

```
   ┌──────────────────────────────────────────────────────────────────┐
   │ (a) PARAMETERISE — default, works today, covers ~80%             │
   │     A plugin does NOT add a component type. It configures        │
   │     existing generic ones: TableCard, ChartCard, FormCard,       │
   │     HtmlDocCard, and the basic primitives (Column/Row/Card/…).   │
   │     Cost at render: zero. Cost to add a plugin: zero frontend.   │
   ├──────────────────────────────────────────────────────────────────┤
   │ (c) SANDBOX — escape hatch, works today, for genuinely novel UI  │
   │     The plugin ships an HTML/JS artifact rendered in a           │
   │     double-iframe: a same-origin proxy frame, then an inner      │
   │     `srcdoc` frame that NEVER gets allow-same-origin (because    │
   │     allow-scripts + allow-same-origin lets a frame delete its    │
   │     own sandbox attribute). Aleph has HtmlFrameCard and          │
   │     code-runner already; this is a hardening of what exists.     │
   ├──────────────────────────────────────────────────────────────────┤
   │ (b) MIXABLE CATALOGS — the right long-term answer, not yet       │
   │     A2UI v1.0: multiple catalogs combined within ONE surface,    │
   │     `catalogId` optional on individual components, resolution    │
   │     order component → surface default → ERROR WITH NO FALLBACK.  │
   │     Plus `inlineCatalogs`: catalogs negotiated and delivered at  │
   │     runtime. This IS a runtime component-registration mechanism  │
   │     and it is the single most relevant A2UI feature to Aleph.    │
   │     STATUS: spec complete; renderer PR #2257 OPEN, not merged,   │
   │     as of 19 Aug 2026. Weeks-to-months, not vapourware.          │
   └──────────────────────────────────────────────────────────────────┘
```

**The design rule: (a) is the default, (c) is the escape hatch, and (b) replaces (c) for
declarative cases when the renderer lands.** Write the v0.9 message builders so that the v1.0
migration is a swap and not a rewrite — which mostly means: never assume one catalog per surface,
and put `catalogId` on components from the start even though v0.9 ignores it.

**The isolation property that makes this a plugin system rather than a pile.** v1.0's resolution
is *strict, with no fallback* — a component naming an unknown catalog is an error, not a
best-effort render. That is the whole point, and it means: **one `catalogId` per plugin, minted
as a URL from the `PluginId`, never shared.** A single shared id across plugins throws the
isolation away and reintroduces the drift problem at plugin scale.

**Performance, since the owner asked.** Catalog resolution in v1.0 is a map lookup on
`catalogId`. Ten plugin catalogs are ten map entries. The one thing that *would* be slow is
validating every message against JSON Schema on the hot path — which is exactly why upstream
cached the compiled validator on the catalog object (PR #1972, 27 Jul 2026). Cache one validator
per catalog and this disappears. The real cost in this layer is not plugin dispatch; it is model
output tokens and time-to-first-render, which is what A2UI's flat adjacency-list design and the
**Express** compact DSL target (claimed 55–70% fewer output tokens, line-by-line streaming —
still under `proposals/`, so measure it rather than believe it).

### ④ Actions — the closed union has to open, carefully

Today an action is one of 20 names in a generated TypeScript union, dispatched by a fixed
`ActionRouter` registry, validated against the catalog, and written to a `CardAction` row plus a
ledger event in one transaction. The *transaction discipline is right and must survive*. The
*closed union* cannot.

```
  action name:  "plugin:<plugin_id>/<verb>"        e.g. "plugin:018f…/retract_claim"
                 │         │           │
                 │         │           └─ declared in the plugin's catalog
                 │         └───────────── minted by register_dynamic, unforgeable
                 └─────────────────────── reserved prefix; core actions keep bare names

  dispatch:  ActionRouter
               ├─ bare name  → the existing fixed registry (unchanged)
               └─ plugin:…   → resolve plugin_id in the kernel
                                 ├─ not mounted / not active → REFUSE, 409, ledger row
                                 └─ mounted → the plugin's declared handler
                                       (still inside the same transaction,
                                        still writes the ActionLedgerEvent)
```

Three properties this preserves that are worth being explicit about: an action name an agent
invents cannot address a plugin that is not mounted (the `PluginId` is minted by
`register_dynamic` and nothing else); every plugin action is ledgered exactly like an analyst's
click; and a *refusal* is itself a ledger row, so "the plugin was unmounted mid-flight" is
visible rather than silent.

### ⑤ Approval — one generic form renderer, forever

Covered in §3.2 and §6. The point for this section: this is the link that makes plugins
*cheap*. If approval forms are hand-written per approval type, every plugin needs frontend work.
If they are generated from `responseSchema`, no plugin ever does.

### The negative case: deactivation must be visible

A plugin system that can only add is not a plugin system.

- Every pane, card and rail entry must carry the `plugin_id` / `catalogId` it came from.
- When a plugin is deactivated, its panes render a **tombstone** — "this view came from
  `<plugin>`, which is no longer active" with a re-enable affordance — not a blank pane and not a
  crash. A blank pane is indistinguishable from a loading pane, which is how the grounding
  surface managed to be invisible rather than broken.
- Before deactivating, the UI shows the blast radius. `AgentPluginAPI.inspect()` already returns
  `would_also_stop` as a pure function over the declaration graph, so this costs nothing.
- Rendering must **refuse** a component whose `catalogId` is not in the active set. No fallback
  to the basic catalog. (Note the current bridge sets `defaultCatalogId` explicitly *because* the
  fallback path silently produced "Catalog not found" — the failure was already met once.)

### What has to be true, summarised

1. The pane-kind list has exactly one source, derived from the mounted set, published to both
   sides. (Two sources exist today and already disagree.)
2. The catalog is assembled server-side per request from mounted plugins, never shipped from the
   client, and carries its own agent instructions.
3. One `catalogId` per plugin, a URL, minted from the `PluginId`, never shared, never falling back.
4. Delivery uses parameterised components by default and a sandboxed frame for novel UI, with
   mixable catalogs adopted when the v1.0 renderer ships.
5. Action names are namespaced by `PluginId` and resolved through the kernel, inside the existing
   ledger transaction.
6. Approvals are schema-driven so no plugin needs frontend work to ask a question.
7. Deactivation is as visible as activation: tombstones, blast radius, strict refusal.

---

## 5. How agent-written plugins surface safely

Aleph's kernel already has the hard parts, and they are unusually well-judged: the AST gate
(loading is not running), the mandatory probe (a capability that cannot answer a live query does
not come up), `PluginId` as the *only* addressable handle (core capability is not
protected-by-policy, it is **unnameable**), and the honest boundary comment in `skills.py` that
once a skill's function is *called* it runs with this process's authority.

The interface's job is to make that trustworthy at the moment a human is asked to say yes.

```
  agent authors a capability
        │
   ┌────▼────┐   AST gate: top level is definition-only, or rejected outright.
   │ ADMIT   │   The module has not had a turn. Nothing has executed.
   └────┬────┘
        │
   ┌────▼────┐   Kernel registers it dynamically, runs the probe against the live
   │ PROVE   │   system. Probe fails → rollback. The probe result is a fact the
   └────┬────┘   UI can show, not a promise the agent made.
        │
   ┌────▼────┐   AG-UI interrupt, reason "aleph:plugin_install", carrying a
   │ ASK     │   responseSchema. The UI GENERATES the form. approveWithEdits
   └────┬────┘   lets the human amend the manifest before saying yes. expiresAt
        │        bounds it. Resume metadata carries the ledger id + signature.
   ┌────▼────┐
   │ ACTIVATE│   Ledgered. The capability document changes. The rail changes.
   └────┬────┘   No redeploy.
        │
   ┌────▼────┐   Its UI is parameterised components, or a sandboxed artifact
   │ RENDER  │   frame. Never model-authored code in the app context.
   └─────────┘
```

**What the approval screen must actually show**, because "approve this plugin?" with a name is
worthless:

- The **probe result**, verbatim — what was observed, from the live system. Aleph's `problem()`
  already refuses a failing probe with no detail; surface that detail.
- **What it provides and requires**, from the spec — and therefore what will depend on it.
- The **AST gate verdict** and the source, rendered read-only in the reading region beside the
  approval. This is a comparison task, and the reading region tiles for exactly this.
- **What removing it later would cost** — `would_also_stop`, precomputed.
- Which **catalogId** it will own, and which components it adds. A plugin adding
  `HtmlFrameCard`-style escape-hatch UI is a materially different risk from one adding a
  `TableCard` configuration, and the screen should say which.

**Four rules that must not bend:**

1. **No agent-emitted code runs in the app context.** Plugin UI is parameterised catalog
   components or a sandboxed artifact — inner `srcdoc` frame, `allow-scripts allow-forms
   allow-popups allow-modals`, and **never `allow-same-origin`**, because that combination lets a
   frame remove its own sandbox attribute. Check Aleph's current `HtmlFrameCard` attributes
   against this.
2. **The interrupt id is the ledger correlation key.** The AG-UI spec names it "the correlation
   key across interrupt, resume, idempotency, and audit." That is the same key Aleph's
   hash-chained `ActionLedgerEvent` wants. Use one.
3. **Approve per *argument*, not per tool.** Interrupt on destructive or out-of-scope arguments,
   not on every call. Blanket per-tool approval trains people to click through, which destroys
   the value of approval entirely.
4. **Test the cheap hypothesis first.** Deep Agents' skills plus a *writable* `/skills/personal/`
   route on the existing `StoreBackend` would already be runtime self-extension — an agent writing
   `SKILL.md` files for itself. If most self-authored plugins turn out to be instructions plus a
   tool function rather than kernel capabilities, a large slice of the thesis is already shipping
   and the kernel can stay smaller. Worth an afternoon, before the kernel is finished rather than
   after.

---

## 6. Streaming, progress, approvals, long-running work — what actually works

### One transport, four channels

```
  TODAY                              TARGET
  ─────                              ──────
  EventSource /surfaces/stream       one authenticated AG-UI stream, carrying:
  EventSource /agent-events/stream     • TEXT_MESSAGE_*     the reply
  EventSource /changes                 • TOOL_CALL_*        what it is doing
  fetch POST  → Node → API             • ACTIVITY_*         structured progress
  + 6 react-query pollers              • STATE_DELTA        the workspace's shared state
                                       • RUN_FINISHED       incl. interrupt outcomes
  4 orderings, 3 unauthenticated     1 ordering, 1 credential
```

**Activity messages are the right home for progress.** The `activity` role is
*frontend-only* — explicitly stripped from `RunAgentInput` so it never reaches the model —
carries an `activityType` discriminator telling the UI which renderer to use, and uses the same
snapshot+delta pattern as state, with a `replace` flag so a late snapshot cannot clobber a live
one. That is exactly what Aleph's phase-event stream, pipeline strip and ingest progress are.
`ActivityMessage` is present in the Python `ag_ui.core.types`, so Aleph can emit these today
after the version bump.

**Use `STATE_DELTA`, not repeated snapshots.** Aleph's pinned `ag-ui-langgraph 0.0.36` emits
`STATE_SNAPSHOT` and never `STATE_DELTA`, so every state change resends the whole object.

**Progressive composition.** `StateStreamingMiddleware(StateItem(state_key, tool,
tool_argument))` projects partially streamed tool arguments into shared state — a report body
fills the reading region as it is written. Note the middleware deliberately suppresses itself
when the last message is a `ToolMessage`, to avoid a duplicate stream if the same tool fires
twice; that subtlety is worth not reimplementing.

**Throttle before you optimise anything else.** `event-throttle-middleware` at `intervalMs: 16`
(≈60fps) coalesces high-frequency events on an allowlist so lifecycle events are never buffered.
Combined with `STATE_DELTA` and, if volume ever justifies it, content-negotiated binary protobuf
(`@ag-ui/proto` + `@ag-ui/encoder`, `Accept`-header negotiated, falls back to SSE), that is three
independent mitigations for stream cost. None of them is where the current cost is: the current
cost is a Node hop decoding and re-encoding every event, three redundant connections, and a
2.96 MB bundle.

### Approvals — the pattern that works

```
  agent reaches a gate
     │
     ├─ LangGraph interrupt({"reason": "aleph:plugin_install",
     │                       "responseSchema": {...JSON Schema...},
     │                       "expiresAt": ...,  "message": ...})
     │
     ├─ ag_ui_langgraph maps it to an AG-UI Interrupt   [verified: interrupts.py]
     │  and the RUN ENDS — RunFinished{outcome:{type:"interrupt", interrupts:[…]}}
     │
     ├─ the UI renders a form FROM THE SCHEMA. No per-approval frontend code.
     │  approveWithEdits lets the human amend the arguments first.
     │
     └─ a NEW run starts, carrying resume:[{interruptId, status, payload, metadata}]
        • the resume must address EVERY open interrupt — no partial resumes
        • resumes are idempotent on (threadId, interruptId, status, payload)
        • state + message snapshots MUST be emitted BEFORE the interrupting
          RunFinished, so resumption is identical whether the backend replays
          context or restores a checkpoint
```

Because the pause lives in the checkpointer, it survives a page reload. Because the interrupt id
is the audit correlation key, the ledger row and the UI agree. Because resume entries carry
`metadata` — added 19 Aug 2026 explicitly for "a signature proving the human decision was not
tampered with" — the hash chain can reach the human decision.

**The migration hazard, verified in the wheel:** `LangGraphAGUIAgent.__init__` defaults
`emit_interrupt_outcome=False` for back-compat, keeping the legacy
`CustomEvent(name="on_interrupt")` path. Turn it on **only after** the client can read
`RunFinished.outcome`, or resumption silently breaks. Also verified: disabling the legacy event
forces the structured outcome on, "to avoid silently stranding the run" — a good default, worth
knowing before you set either flag.

### Long-running work

- **Set `durability` per graph.** `"exit"` for cheap chat turns; `"async"` or `"sync"` for the
  research loop where resumption matters. Framework overhead is ~60 µs per graph node and ~144 µs
  with an in-memory checkpointer, against LLM calls three to five orders of magnitude larger. The
  real cost is the Postgres write per super-step, and this is the dial that controls it. Evaluate
  `DeltaChannel` (LangGraph 1.2, beta) for the long graphs.
- **`parentRunId` for branching**, so a long run can be forked and compared in two panes — which
  is what the reading region was built for.
- **`compactEvents()`** to fold verbose history into snapshots before storing it.
- **Do not design around WebSocket, `pushNotifications` or `resumable` transports.** They are
  declared capabilities in the type system with **no open implementation**; `@ag-ui/client` ships
  exactly one agent class, `HttpAgent`. Durable reconnecting threads live in CopilotKit's
  commercial Intelligence runtime, which is explicitly not self-hostable. Aleph is docker compose.
  Stay on SSE, keep `/connect` for reattach.

---

## 7. Migration path — seven stages, each independently shippable

Each stage lands on its own, leaves the system working, and has a check that can fail.

### Stage 0 — fix what is already broken (days, no dependencies)

- Delete one of the two `buildCatalog` functions; export one `ALEPH_V09_CATALOG_ID` from one
  module; register the 14 basic-catalog **functions** in the survivor. Add a test asserting the
  chat catalog and the pane catalog have identical component *and* function sets.
- Add a `headers` function to `<CopilotKitProvider>` returning
  `{ Authorization: "Bearer " + token }` (the prop accepts a *function* so it re-evaluates on
  token refresh), or use `credentials="include"` for HTTP-only cookies. Verify the bridge's
  `cors: true` echoes `Authorization` in `Access-Control-Allow-Headers`. Correct the two
  misdiagnosed `CLAUDE.md` entries.
- Upgrade `@copilotkit/runtime` to ≥1.66.4 (the memory fix) — while the service still exists.
- Give the `grounding` surface a launcher, or delete it. A producer with no consumer.
- Pin exact `@a2ui/*` versions instead of `^0.10`.

**Done when:** the catalog-parity test passes, and a request from the browser arrives at the
Python endpoint carrying an `Authorization` header.

### Stage 1 — Python version floor (days)

`ag-ui-protocol` 0.1.18 → 0.1.20, `ag-ui-langgraph` 0.0.36 → 0.0.43, `copilotkit` 0.1.91 →
0.1.95. Behaviour unchanged; `emit_interrupt_outcome` stays `False`. This is the gate for
everything after it.

**Done when:** the existing Playwright suite is green and `get_a2ui_tools` is importable.

### Stage 2 — move A2UI into Python and bind the catalog server-side (1–2 weeks)

Call `get_a2ui_tools(A2UIToolParams(model=..., catalog=<from catalog.json>,
default_catalog_id=...))` in the graph builder, or pass `a2ui_params` to `CopilotKitMiddleware`.
Stop depending on the client-forwarded `injectA2UITool` flag and the client-shipped
`a2ui_schema` context entry. Delete the `a2ui:` block and `catalog.generated.ts` from the Node
service — it becomes a pure proxy.

**Done when:** an agent-composed surface renders in chat with the Node service's A2UI config
removed, and `apps/copilot-runtime/src/catalog.generated.ts` is deleted (so
`check-catalog-generated.sh` has one fewer generated copy to police).

### Stage 3 — serve the five routes from FastAPI, delete the Node service (2–3 weeks)

Implement `GET /info`, `POST /agent/{id}/run`, `/connect`, `/stop/{threadId}`, and optionally
`/suggest`. Point `VITE_COPILOT_RUNTIME_URL` at aleph-api behind a flag; cut over with Playwright
green; delete `apps/copilot-runtime/`, its Dockerfile, its lockfile and its compose service.
Switch the browser to `@copilotkit/react-core/v2/headless` (the export exists in the installed
1.58.0) and draw Aleph's own chat list and composer against `@ag-ui/client`.

**Done when:** no Node service in the compose file, the browser bundle drops by roughly 2.9 MB
gzip, and the two credential-forwarding entries can be deleted from "Known broken."

### Stage 4 — one transport (2–3 weeks)

Emit `ACTIVITY_SNAPSHOT` / `ACTIVITY_DELTA` with `activityType` discriminators for phases,
ingest, reviewer passes and the research loop. Retire the three `EventSource` streams and the
1.5s/15s pollers. Switch state to `STATE_DELTA`. Add `event-throttle-middleware` at
`intervalMs: 16`. Move the card-action history *out* of `useAgentContext` and into a tool the
agent calls when it needs it — right now it is serialised into every prompt on a 15-second poll.

**Done when:** the browser opens one streaming connection per workspace, it carries a bearer
token, and every visible surface shares one sequence.

### Stage 5 — approvals become interrupts, forms become generated (2–3 weeks)

Turn on `emit_interrupt_outcome=True` *after* the client can read `RunFinished.outcome`. Build
one schema-driven form renderer. Move connector toggles, artifact builds, hypothesis creation and
every other approval-gated path onto `interrupt()` with a `responseSchema`. Keep the
`ActionLedgerEvent` write; use the interrupt id as the correlation key. Namespace custom reasons
`aleph:<name>`. Add `approveWithEdits`.

**Done when:** an approval survives a page reload, and adding a new approval type requires no
frontend change — proven by adding one and shipping only server code.

### Stage 6 — the capability document drives the shell (2–3 weeks)

Implement `get_capabilities()` on the Python agent, computed per request from
`AgentPluginAPI.inspect()`. Move the agent registry to the per-request factory form. Delete
`SURFACE_TABS`; derive the rail, the pane-kind vocabulary and the UI's affordances from the
document. Unify with the server's `_PANE_KINDS`. Show probe results and blast radius in the UI.

**Done when:** mounting a capability changes the rail with no rebuild, and unmounting one leaves
a tombstone rather than a blank pane.

### Stage 7 — plugin catalogs (ongoing, partly gated upstream)

Restructure `catalog.json` into a real A2UI `catalog_definition` with a URL `catalogId`,
`functions`, `instructions` and `protocolVersion`. Generate the agent's component guidance from
it. Namespace actions `plugin:<id>/<verb>` and resolve through the kernel. Adopt the upstream
conformance vectors in CI, and the Inspect AI eval per discovered model so "can this gateway's
model produce valid Aleph UI?" becomes a number in the existing `aleph-evals` pattern. Adopt
mixable catalogs and `inlineCatalogs` when PR #2257 lands a v1.0 renderer.

**Done when:** a plugin ships its own catalog and its components render without a frontend build.

---

## What Aleph should do

1. **Delete `apps/copilot-runtime/` and serve the five routes from FastAPI.** Its only stated job
   now runs in Python (`get_a2ui_tools`, verified in `ag-ui-langgraph 0.0.43`), and its protocol
   is `info` / `agent/{id}/run` / `connect` / `stop/{threadId}` / `suggest`, read from the router
   in the version Aleph actually runs. This removes a service, a duplicated catalog, four unused
   provider SDKs, an unbounded-memory bug, and both credential gaps by construction.
2. **Bind the A2UI catalog server-side.** Today the client sends the `injectA2UITool` flag and
   the catalog schema on every run. The server owns the plugin registry; the server must own the
   catalog. This is a prerequisite for plugins, not just a cleanup.
3. **Fix the shipped catalog divergence now.** One `buildCatalog`, one `ALEPH_V09_CATALOG_ID`,
   the 14 basic functions registered in it, and a test that the chat and pane catalogs are
   identical. A surface using `formatString` currently works in a pane and fails in chat.
4. **Publish a live capability document from the kernel and drive the shell from it.**
   `AgentPluginAPI.inspect()` already returns name, state, provides, requires, `plugin_id` and
   the precomputed blast radius. Project it into AG-UI's `getCapabilities()` shape, recomputed per
   request, and delete `SURFACE_TABS`.
5. **Make approvals interrupts with `responseSchema`.** One generic schema-driven form renderer
   means no plugin ever needs frontend work to ask a question. Verified end to end:
   `lg_interrupt_to_agui` maps `responseSchema` straight through from a plain LangGraph
   `interrupt()`.
6. **Collapse four transports into one.** Activity messages replace the three `EventSource`
   streams and the pollers; `STATE_DELTA` replaces whole-object snapshots; the AG-UI stream is a
   `fetch` POST and can carry a bearer token, which closes the OIDC SSE gap as a side effect.
7. **Give plugins one URL `catalogId` each, minted from the `PluginId`, never shared, never
   falling back.** Strict resolution is A2UI v1.0's isolation mechanism and it costs a map lookup.
8. **Namespace actions `plugin:<id>/<verb>` and resolve through the kernel**, inside the existing
   ledger transaction. Keep the transaction discipline; open the union.
9. **Keep Deep Agents, mounted as one kernel capability behind Aleph-owned types**, and pass
   `profile=` to `ChatOpenAI` built from `aleph_models.discovery` — otherwise the summariser
   assumes 170,000 tokens behind a gateway and overflows any smaller model.
10. **Test the cheap hypothesis first:** can the agent usefully extend itself by writing
    `SKILL.md` files to a writable `/skills/personal/` route on the existing `StoreBackend`? If
    yes, a large slice of the plugin thesis already ships and the kernel can stay smaller.

## What Aleph should avoid

1. **Do not keep a Node runtime "just for A2UI."** That was true when the service was written and
   is no longer true. Every week it survives it drifts further from the pinned frontend and hides
   the auth gap it created.
2. **Do not adopt CopilotKit Intelligence, Channels, Memory or `useThreads`.** Explicitly not
   self-hostable from the OSS packages, and Aleph already owns its threads, memory and ledger.
   Equally, do not build on `selfManagedAgents` — the docs call it Enterprise and the repo's own
   skill files call it "not production-safe."
3. **Do not upgrade `@copilotkit/react-core` past 1.58 while `renderActivityMessages` is passed
   by hand.** Current versions auto-inject the A2UI renderer off `/info`; the manual prop
   duplicates and races it. Move the catalog onto the provider in the same change — or skip the
   problem by going headless.
4. **Do not adopt A2UI v1.0 before a renderer ships it.** The spec is complete; PR #2257 was
   still open on 19 Aug 2026. Read the evolution guide now, migrate later — and note the silent
   breaking change: `updateDataModel.value` becomes **required**, with `null` meaning delete, so
   code that deletes keys by omitting `value` starts failing validation.
5. **Do not confuse `@a2ui/*` package versions with spec versions.** `@a2ui/react@0.10.2`
   implements spec **v0.9**. Spec v1.0 was drafted as "v0.10" and renamed. Any comment treating
   `^0.10` as a spec version is wrong. And do not expect the zod v3 pin to have gone away — the
   "accept both zod 3 and 4" RFC was closed on 2026-08-04. Keep the `zod3` alias; `@ag-ui/client`
   needs zod ^3 too.
6. **Do not let plugin catalogs share one `catalogId`, and do not add a fallback when resolution
   fails.** The isolation is entirely keyed on the id with no fallback; a shared id throws it away.
7. **Do not run agent-emitted code in the app context, and do not combine `allow-scripts` with
   `allow-same-origin` in any sandbox frame** — that pair lets a frame delete its own sandbox
   attribute. Use the double-iframe pattern: same-origin proxy, inner `srcdoc`, never
   `allow-same-origin`.
8. **Do not turn on `emit_interrupt_outcome` before the client can read `RunFinished.outcome`** —
   resumption breaks silently. And do not implement partial resumes: the spec requires one
   `resume` array addressing every open interrupt.
9. **Do not design around AG-UI's WebSocket, `pushNotifications` or `resumable` transports.**
   They are declared capabilities with no open implementation; the durable-thread story is
   CopilotKit's commercial runtime. Aleph is docker compose.
10. **Do not build the plugin kernel on Deep Agents or LangGraph.** Middleware is assembled at
    graph-construction time; there is no mount, unmount or revert. deepseek-harness ran the
    experiment and put the loop *inside* a kernel rather than the reverse. Do not bump `deepagents`
    past `<0.7` without a checklist either: 0.7.0 silently drops `TodoListMiddleware` from the
    default stack (so `write_todos` and the plan vanish with CI green) and adds a recursive
    `delete` tool that existing write permissions already authorise.
11. **Do not let `useAgentContext` become a dumping ground.** Every registered value is
    serialised into every request; the 15-second card-action poll currently feeds it on every turn.
12. **Do not assume a plugin architecture is the performance problem.** Measured: ~60 µs per
    LangGraph node, ~144 µs with a checkpointer; catalog resolution is a map lookup. The real costs
    are a 2.96 MB bundle, a Node hop re-encoding every SSE event, four connections against the
    browser's ~6-per-origin cap, and model output tokens. Every one of those gets *smaller* under
    the plan above.

---

## Sources

Verified live on 19 August 2026: PyPI JSON API (`ag-ui-langgraph` 0.0.43, `ag-ui-protocol` 0.1.20,
`ag-ui-a2ui-toolkit` 0.0.4, `copilotkit` 0.1.95, `deepagents` 0.7.7, `a2ui-agent-sdk` 0.5.0);
npm registry (`@copilotkit/runtime` 1.68.1, `@copilotkit/react-core` 1.68.1, `@ag-ui/client`
0.0.58, `@ag-ui/core` 0.0.58, `@a2ui/react` 0.10.2, `@a2ui/web_core` 0.10.6); GitHub API
(`a2ui-project/a2ui`, 16,155 stars, pushed 2026-08-19, Apache-2.0; PR #2257 open). Wheels
downloaded and read: `ag_ui_langgraph/a2ui_tool.py`, `agent.py`, `interrupts.py`,
`middlewares/state_streaming.py`; `copilotkit/copilotkit_lg_middleware.py`;
`ag_ui_a2ui_toolkit/__init__.py`. Installed bytes read in Aleph's own tree:
`@copilotkit/runtime@1.63.2` `dist/v2/runtime/core/fetch-router.mjs` and
`dist/v2/runtime/handlers/get-runtime-info.mjs`; `@copilotkit/react-core@1.58.0` exports map.
Aleph source read at `bcc478a`: `apps/web/src/**`, `apps/copilot-runtime/src/**`,
`apps/api/src/aleph_api/copilot_agent.py`, `copilotkit_endpoint.py`, `routes/surfaces.py`,
`packages/aleph-a2ui/**`, `packages/aleph-kernel/**`. Sibling research files in this directory:
`a2ui.md`, `ag-ui.md`, `copilotkit.md`, `deep-agents.md`.
