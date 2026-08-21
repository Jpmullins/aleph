# Why are we deleting the copilotkit-runtime? — the straight answer

**Date:** 2026-08-20 · **Question:** should `apps/copilot-runtime` be deleted?
**Answer:** **Not now, and not for the reason originally given — but yes eventually, on a condition
you write down.** Confidence: high on the first half, moderate on the second.

This re-assessment reads the four prior area passes
(`copilotkit-runtime-surface.md`, `generative-ui-spectrum.md`, `copilotkit-intelligence.md`,
`copilotkit-integration-fit.md`), and then goes back to Aleph's own source and the installed
packages, because all five documents so far — the original recommendation and the four rebuttals —
share a blind spot.

---

## In one paragraph

Aleph runs a small Node program called `apps/copilot-runtime`. It sits between the web app's **chat
box** and the Python API, and its job is to add one tool to the agent (`render_a2ui`, "draw a card")
and to turn what the agent draws into something the browser can paint. Someone recommended deleting
it because that job now also exists in Python. Someone else pushed back that the program does far
more than that and deleting it would go dark. **Both are partly right and both missed the same
thing: Aleph already has a second, home-grown version of this machinery in Python, and it drives the
main part of the app.** The panels you actually do research in — the reading region, the claim views,
the notes — get their cards from Aleph's own Python code over Aleph's own connection and have never
touched the Node program. Only the chat transcript depends on it. So deleting it does not turn off
"every card in Aleph"; it turns off cards **in the chat window**. That reframes the decision from a
big architectural bet into a small, bounded piece of work. My recommendation: keep the Node program
for now — deleting it today breaks the chat lane with nothing ready to replace it — but stop calling
it "the front door," stop planning to build Aleph's security and record-keeping inside it, and write
down the one condition that would let it go. That condition is small and testable, and you should
plan to meet it.

---

## Terms, defined once

| Term | Plain meaning |
|---|---|
| **AG-UI** | The wire protocol between an agent and a user interface. Think of it as the truck. |
| **A2UI** | A format for describing a screen as data ("a card, with this title, bound to this value"). The cargo the truck carries. Not a competing protocol. |
| **Catalog** | The list of screen pieces the agent is allowed to name. An id plus a flat list of components. |
| **Surface** | One drawn thing — a card, a panel — built from catalog pieces plus data. |
| **Activity event** | An AG-UI message that says "render this non-chat thing." Cards ride in these. |
| **Middleware** | A wrapper around an agent run that can inspect and change what goes in and comes out. |
| **The runtime** | `apps/copilot-runtime` — the Node program. CopilotKit's server half. |
| **Intelligence** | CopilotKit's paid hosted platform (durable threads, memory, Slack, learning). |

---

## 1. Was the prior recommendation wrong?

**Partly. It reached a defensible destination by an indefensible route, and the four rebuttals then
overcorrected.**

### 1.1 What the prior pass got right — and it is more than the rebuttals allow

| Claim | Verdict |
|---|---|
| The Node service uses ~1 of 11 constructor options and contains no Aleph logic | **Right.** 80 lines, one `HttpAgent`, one `a2ui` block. |
| A2UI tool generation now exists in Python (`get_a2ui_tools`, `ag-ui-a2ui-toolkit`) | **Right.** Both real, both current. |
| Versions are stale and disagree with each other | **Right, and worse than stated.** react-core 1.58.0 / AG-UI 0.0.53 in the browser vs runtime 1.63.2 / AG-UI 0.0.57 in Node, both against 1.68.2. Confirmed today via `npm view`. |
| The browser-facing surface Aleph exercises is small | **Right in substance.** Nineteen route methods exist; Aleph's own e2e spec touches `/info` and the chat run path. Breadth of an unused API is not value. |
| Nothing in Intelligence rescues the service | **Right.** Everything there 422s or throws without a paid licence. |

### 1.2 What the prior pass got wrong

1. **It priced the option at zero.** It asked "what job is this doing?" and never asked "what could it
   do?" That is the owner's criticism and it is correct.
2. **It said "FastAPI can serve five routes" as though the routes were the work.** `POST /agent/:id/run`
   resolves and clones the agent, validates the input, attaches middleware, merges headers under a
   security denylist, and drives a runner. `/info` is not a route, it is a capability negotiation
   that gates six client features.
3. **It would have shipped a break.** `render_a2ui` is named in `copilot_agent.py`'s system prompt
   (lines 95, 103) and defined **nowhere in Aleph's Python**. Delete the service today and the agent
   is instructed to call a tool that does not exist. That is a real, immediate, verifiable break.

### 1.3 What the four rebuttals got wrong

They corrected the prior pass's error and then made the mirror-image one. Three claims recur across
them and do not survive contact with Aleph's own tree:

> "Deleting turns off **every** generative-UI card in Aleph."

**False.** See §2.1. It turns off chat-transcript cards. The main workspace is unaffected.

> "The card-click return path has **no Python counterpart**."

**False, and backwards.** Aleph built a better one. See §2.3.

> "Reimplementing the A2UI paint contract in Python is a **substantial project** with no reference
> implementation anywhere."

**Overstated by a wide margin.** The contract is four fields and the Python type for it is already
installed. See §2.2.

---

## 2. Five things all five prior documents missed

Verified directly against this tree and the installed packages today.

### 2.1 Aleph has **two** A2UI rendering paths, and the important one does not touch CopilotKit

| | Workspace panes (reading region, claim views, notes) | Chat transcript |
|---|---|---|
| Who emits the surface | **Aleph's own Python** — `aleph_a2ui.messages` builders | The agent's `render_a2ui` tool call |
| How it reaches the browser | **Aleph's own SSE** — `GET /v1/projects/{id}/surfaces/stream` | AG-UI activity events via the Node runtime |
| Who renders it | `SurfaceStreamProvider` → `MessageProcessor([catalog])` from `@a2ui/web_core` | `createA2UIMessageRenderer` from `@copilotkit/react-core` |
| Depends on `apps/copilot-runtime`? | **No. Not at all.** | Yes |
| Depends on CopilotKit at all? | **No.** | Yes |

`packages/aleph-a2ui` is **1,565 lines of Python** that already does the host-side job for the panel
path: v0.9 operation builders (`messages.py`), a JSON-pointer delta engine for incremental updates
(`surface_streamer.py`), a catalog and validator (`catalog.py`), a ledger-writing action dispatcher
(`action_router.py`), and an MCP server that exposes Aleph's catalog to outside clients
(`mcp_server.py`).

This single fact rewrites the blast radius. The rebuttals' strongest sentence — "every generative-UI
card goes dark" — describes a system Aleph is not. The reading region, which is where the research
actually happens, keeps working.

### 2.2 The chat paint contract is four fields, and Python already has the type

The A2UI middleware's entire output, extracted from the installed
`@ag-ui/a2ui-middleware@0.0.10` bundle, is this event shape:

```
ACTIVITY_SNAPSHOT
  messageId    = "a2ui-surface-<key>"
  activityType = "a2ui-surface"
  content      = { "a2ui_operations": [ ...v0.9 messages... ] }
  replace      = true
```

`ActivitySnapshotEvent` — with exactly the fields `message_id`, `activity_type`, `content`,
`replace` — is in **`ag-ui-protocol` 0.1.18, which is installed in Aleph's `.venv` right now.**
`ActivityDeltaEvent` (JSON-Patch onto an activity message) is there too, which is the progressive-
update primitive. And Aleph already has the v0.9 operation builders in `aleph_a2ui.messages`.

So "no Python implementation exists anywhere" is true of *CopilotKit's packages* and false of *the
contract*. Emitting a chat card from Python is assembling a dict Aleph already knows how to build and
wrapping it in a Pydantic model that is already on disk.

**Being honest about what is genuinely harder.** Three of the middleware's eight jobs are not trivial:
painting a surface *while* the model is still streaming its tool-call arguments (a partial-JSON
extractor), the `building / retrying / failed` skeleton with a live token estimate, and the
validate-before-paint gate with a re-prompt loop. Those are real engineering. But note what they are:
they are quality-of-experience for the case where **a second LLM is inventing the layout at request
time**. Aleph's panel path does not need any of them, because Python builds the surface
deterministically — and CopilotKit's own docs say the fixed-schema mode is "faster, cheaper and
deterministic." Aleph currently pays for the expensive mode (`injectA2UITool: true`) on every chat
surface, including ones whose shape is perfectly well known.

### 2.3 Aleph's click round-trip is not missing — it is better, and CopilotKit's would be a regression

Three rebuttals list "card button clicks reach the agent" as an unrecoverable loss. CopilotKit does it
by appending a synthetic `log_a2ui_event` tool call and result into the conversation.

Aleph does it through `POST /v1/projects/{id}/cards/actions` → `aleph_a2ui.action_router.ActionRouter`,
which validates the action against the catalog schema, dispatches to a registered handler, and writes
a `CardAction` row **plus the ledger event, in one transaction**.

CLAUDE.md's own rule: *"Every state mutation writes an `ActionLedgerEvent` in the same transaction."*
CopilotKit's bridge writes to the conversation and nothing else. It structurally cannot satisfy that
rule. **Adopting it would be a downgrade.** This is a non-loss listed three times as a loss.

### 2.4 The MCP Apps and open-ended-UI *renderers* are already in the browser bundle

`@copilotkit/react-core@1.58.0` — the version installed — already contains:

- `MCPAppsActivityRenderer`, keyed on `activityType === "mcp-apps"`, with a zod content schema
  (`{ result, resourceUri, serverHash, serverId?, toolInput? }`) and a full sandboxed-iframe proxy
  with an explicit CSP.
- `OpenGenerativeUIRenderer`, keyed on `activityType === "open-generative-ui"`, over
  `@jetbrains/websandbox` (already a declared dependency).

These are **plain activity renderers**. They will render for any server that emits the right event.
The Node runtime is the shipped *emitter*, not a gatekeeper on the *rendering*.

This narrows the MCP Apps argument — the strongest pro-keep argument in the rebuttals — without
eliminating it. What is genuinely Node-only is the **server-side half**: connecting to MCP servers,
discovering UI-bearing tools, fetching the UI resource, and proxying the iframe's JSON-RPC back to the
MCP server. Aleph would have to build that. Worth noting that Aleph is already an MCP *server*
(`aleph_a2ui/mcp_server.py`), and being the provider of research MCP Apps may matter more to this
product than being a consumer of third-party ones.

### 2.5 CopilotKit's React client is license-gated, and the gate tightened between 1.58 and 1.68

Nobody raised this. It is in the code.

`createLicenseContextValue` in `@copilotkit/shared`:

- **1.58.0 (installed):** `checkFeature: () => true` — hardcoded permissive, ignores status entirely.
- **1.68.2 (current, downloaded and read today):** `checkFeature: () => status !== "expired" && status !== "invalid"`.

Feature ids already in the client: `chat`, `sidebar`, `popup`. The client ships `LicenseWarningBanner`
("Powered by CopilotKit" / "requires a CopilotKit license") and `InlineFeatureWarning`, which
`CopilotSidebar` and `CopilotPopup` render inline. `@copilotkit/license-verifier` (Ed25519) is in the
tree. `@scarf/scarf` and a `TelemetryClient` are dependencies.

**Nothing is blocked today** — unlicensed status is not `expired` or `invalid`, so features stay on,
and Aleph uses `CopilotChat`, which only logs a console warning. I am not predicting a rug-pull. But
two facts are worth stating plainly:

1. The gate went from *ignored* to *status-driven* in ten minor versions. That is a direction.
2. **`licenseStatus` is reported by whatever serves `/info`.** Keep the Node runtime and CopilotKit's
   code decides that field. Serve `/info` from FastAPI and Aleph decides it.

That is an argument for owning `/info` that cuts the opposite way from the rebuttals' "`/info` is a
burden you'd inherit." It is a burden *and* a control point.

---

## 3. The honest cost/benefit of keeping it

Written as what you actually get and actually pay, not as feature lists.

| Keeping `apps/copilot-runtime` buys you | Keeping it costs you |
|---|---|
| **The chat lane keeps working.** `render_a2ui` exists because the middleware injects it. Today this is the only thing standing between the system prompt and a tool that does not exist. Immediate, real, and the whole reason not to delete this week. | **A second language at the front door, permanently.** A Node 22 container, 512 MB, one more compose service, one more restart surface, one more thing to upgrade. |
| **Progressive paint and the building/retrying/failed skeleton** for chat cards. Genuinely nice, genuinely not reimplemented in a day — but only needed for the request-time-layout-invention mode Aleph should mostly stop using anyway. | **A second dependency universe.** `apps/copilot-runtime` is npm with its own `package-lock.json` and is **not a pnpm workspace member**, so the Dependabot security `overrides` in `pnpm-workspace.yaml` do not reach it. CI installs it separately. This already caused one incident: the manifest floated `^1.58`, resolved 1.63.2, and the container "only worked because of that accidental float." |
| **MCP Apps as configuration** rather than code — the one band where a third party ships an ability *and* its interface. Closest existing thing to the plugin thesis. Unused. | **A governance hole.** Pyright-strict does not cover it. Ruff does not cover it. `scripts/acceptance.sh` does not run it. It is the least-checked code in a repo whose entire CLAUDE.md is about not trusting unchecked claims. |
| **`FilterToolCallsMiddleware`, `hooks.onRequest`, per-request agent factory, `PostgresAgentRunner`** — real, well-specified extension points. | **The extension points are in the wrong process** (see below). This is the decisive cost and it is not obvious. |
| **Someone else maintains `/info`,** event translation, tool-call argument accumulation, reconnect. | **Someone else controls `/info`,** including `licenseStatus` (§2.5), and versions it on their cadence. |
| **A place to stand** if Channels (Slack/Teams) or Intelligence are ever wanted. Channels is Node-only in both the managed and MIT paths. | **Version-skew maintenance,** already live and already producing `VERSION_MISMATCH`-class risk between the browser and the server in the same repo. |
| Suggestions, transcription, live event tracing, thread inspection — four shipped features the installed client already knows how to call. | **An extra hop where Aleph's `Principal`, ledger, and OTEL context are not automatic.** |

### The cost that is not on anyone's list: the extension points are in the wrong process

This is the argument I think decides the strategic half of the question, and no prior document makes it.

Every extension point the rebuttals are excited about is a place to put **Aleph's most
security-and-provenance-critical code**:

- A `PostgresAgentRunner` would write run state into Aleph's Postgres — which means writing rows that
  must carry `project_id` and `access_scope`, and appending to a **hash-chained** ledger. In
  TypeScript. Outside the ORM. Outside the repositories. Outside pyright.
- `hooks.onRequest` would verify Aleph's `Principal`, its agent tokens, and its project scoping — a
  second implementation of `packages/aleph-security` in another language.
- `FilterToolCallsMiddleware` would enforce the three-tier plugin trust model — Aleph's kernel
  problem, sited in the one process the kernel does not run in.

CLAUDE.md exists because *three hand-maintained copies of one catalog disagreed and no test noticed*,
and because *four features were inert while every step reported success*. Putting auth, the ledger and
the trust model in a second language, in the least-governed process in the repo, is that failure mode
with a bigger blast radius. The runtime's extension points are excellent — for an application whose
backend is already TypeScript. For Aleph they are an invitation to duplicate the crown jewels.

**Corollary: even if you keep the service, do not invest it with any of that.** Auth, the ledger, run
durability and plugin trust belong in FastAPI whichever way this decision goes. That removes four of
the six pro-keep items from the ledger, because they were never really available to Aleph.

### Correction to a CLAUDE.md "Known broken" entry

CLAUDE.md says *"The runtime bridge does not forward the caller's credential… constructs
`new HttpAgent({ url: AGENT_URL })` with no headers."* I read `handlers/header-utils.mjs` in the
installed 1.63.2: `mergeForwardableHeaders` forwards inbound `authorization` and `x-*` by default,
under a denylist that strips `x-forwarded-*`, `x-real-ip`, `x-request-id`, `x-vercel-*`, `x-amz-*`,
`x-copilotcloud-*`, with server-set headers winning case-insensitively. Constructing `HttpAgent`
without headers is exactly the case that code handles.

**But do not simply delete the entry.** The gap is real and mislocated: `server.ts` sets `cors: true`,
which is allow-all **without** credentials, and the browser client sets no `Authorization` header at
all. So nothing arrives to forward. The entry should be rewritten as *browser → runtime*, not
*runtime → API*, and re-tested before it is quoted again.

---

## 4. Recommendation, confidence, and what would change it

### Recommendation

**KEEP `apps/copilot-runtime` now. Reclassify it. Set a written exit condition. Do not build on it.**

Concretely, three commitments:

1. **Keep it** — deleting today breaks the chat lane against a system prompt that demands a tool that
   would no longer exist. There is no version of "delete this sprint" that is responsible.
2. **Reclassify it** in the docs from *the front door* to *a removable adapter for the chat lane*.
   It is not the front door: FastAPI already serves four SSE streams of its own and the entire
   workspace surface path. The Node program is a sidecar on **one lane**.
3. **Write the exit condition** into `docs/decisions.md`, in the style `docs/acceptance.md` §E uses
   for the wiki deletion: *"`apps/copilot-runtime` is deleted when the FastAPI agent endpoint emits
   `a2ui-surface` ACTIVITY_SNAPSHOT events for chat surfaces, with an e2e test asserting a card
   painted in the chat transcript with the Node service stopped."* That is the test. It is small.

### Why not "keep and invest"?

Because the investments named — the runner, the auth hooks, the tool-call filter — are Aleph's
security and provenance logic, and §3 says why they must not live there. Strip those out and what
remains that is genuinely runtime-only is: MCP Apps, chat-card progressive paint, and a future Slack
host. One of those is unproven for Aleph, one matters only in the mode Aleph should use less, and one
is a separate decision. That is not enough to justify building a platform on it.

### Why not "delete on the merits"?

Because the merits changed under the prior pass's feet: it recommended deletion on the theory the job
had *already* moved, and it hadn't — `render_a2ui` is prompt-referenced and undefined. Deletion
becomes correct the moment Aleph *moves* it deliberately, with a test. Same destination, honest route.

### Confidence

| Claim | Confidence | Why |
|---|---|---|
| Do not delete in the next month | **High** | Verified break: `render_a2ui` undefined in Python, prompt-referenced in two places. |
| The blast radius is the chat lane, not the whole UI | **High** | Read both render paths in this tree. The pane path imports nothing from CopilotKit. |
| Do not put auth, the ledger, or plugin trust in the Node process | **High** | Follows from Aleph's own stated rules and its own documented failure history. |
| Deletion is the right endpoint within ~2 quarters | **Moderate** | Depends on the MCP Apps experiment and on whether Channels is ever wanted. Both are open. |
| The Python paint path is days, not a project | **Moderate-high** | The contract and the Pydantic type are verified. The *progressive* variant is genuinely more work and I have not built it. |

### What would change my mind, in either direction

**Toward keeping it permanently:**
- The MCP Apps experiment (§5, week one) works and turns out to be the natural shape for
  third-party plugins. That is a strategic capability with no Python path and it would justify the
  second language on its own.
- Slack/Teams becomes a real requirement. Channels is Node-only in both the managed and the MIT
  direct-adapter paths, and needs a process that outlives a request.
- `@ag-ui/a2ui-middleware` stops being the sole emitter and CopilotKit ships an official Python host —
  in which case keeping is free and the argument dissolves.

**Toward deleting sooner:**
- The Python `ActivitySnapshotEvent` chat-paint spike (§5, month one) lands cleanly. Then the exit
  condition is met and the only remaining argument is MCP Apps.
- The MCP Apps experiment underwhelms, or the trust review says arbitrary third-party iframes are not
  something a research workbench should host.
- The client license gate tightens further at 1.7x — e.g. `chat` gated on a real status rather than a
  permissive default. That would make owning `/info` urgent rather than merely preferable.
- Aleph adds a second API replica. `InMemoryAgentRunner`'s store is a process-global singleton; the
  bridge becomes actively wrong under horizontal scaling, and the fix (a durable runner) is code that
  §3 says must not live there.

---

## 5. A staged path

Each stage stands alone. No stage requires the one after it. **No stage deletes anything.**

### Next week — make the thing you have honest (~1–2 days)

Nothing here needs a version upgrade, and nothing here is wasted whichever way the decision goes.

1. **Fix the two CLAUDE.md entries** this pass falsified or mislocated: the credential-forwarding
   entry (§3), and the implied claim that generative UI as a whole depends on the Node service (§2.1).
   CLAUDE.md's own preamble says a false invariant is how a broken path survived seven work packages.
2. **Move `apps/copilot-runtime` into the pnpm workspace.** One line in `pnpm-workspace.yaml`, delete
   `package-lock.json`. The repo's security `overrides` start reaching it. Pure win, zero risk.
3. **Pin `@copilotkit/*` to one version across `apps/web` and `apps/copilot-runtime`,** and add a CI
   check that the two trees agree — same shape and same reason as `check-catalog-generated.sh`.
4. **Add `onError` to the provider** in `apps/web/src/lib/copilot.tsx`. Without it a bad runtime URL
   or a CORS failure shows "connecting…" forever with nothing surfaced.
5. **Run the MCP Apps experiment.** Point `mcpApps: { servers: [...] }` at a public MCP server and see
   whether a third-party UI appears with zero Aleph frontend code. Fifteen minutes to an hour. This
   answers the single biggest open strategic question with a demo instead of an argument, and it is
   the one experiment whose result could flip the recommendation.

*Independently useful?* Yes — every item is a correctness or hygiene fix that survives deletion.

### Next month — buy the option, in Python (~1 week)

6. **Spike Python chat-card emission.** Emit an `ActivitySnapshotEvent(message_id="a2ui-surface-…",
   activity_type="a2ui-surface", content={"a2ui_operations": [...]}, replace=True)` from the FastAPI
   agent path, reusing `aleph_a2ui.messages`. Prove one card paints in the chat transcript with the
   Node service stopped. **This is the exit condition, and it is worth having even if you never
   delete** — it is the deterministic, cheap card path that should be the default anyway.
7. **Stop paying for request-time layout invention on known shapes.** Aleph sets
   `injectA2UITool: true` globally, which puts a second LLM in the loop to invent arrangements. Reserve
   it for genuinely novel surfaces.
8. **Derive the agent-facing schema from the render catalog** — see §6 item 1. Deletes a generator and
   a whole class of drift.
9. **Adopt `filterCatalog` + `setCatalogComponents`** — see §6 item 2. This is the product thesis,
   already implemented by the library, and it is entirely client-side.

*Independently useful?* Yes — 6 is the option itself, 7 saves money and latency today, 8 and 9 are
plugin-model work that is right regardless.

### Defer, deliberately, with reasons

| Defer | Until |
|---|---|
| **Deleting the service** | The §5.6 spike passes and the MCP Apps question is settled. |
| **A `PostgresAgentRunner` in TypeScript** | Never, as specified. If durable threads are wanted, build them in FastAPI over Aleph's Postgres, where `project_id`, `access_scope` and the hash-chained ledger already live. Copy CopilotKit's *interface* (`run`/`connect`/`isRunning`/`stop`), not its process. |
| **`hooks.onRequest` for auth** | Never as the primary defence. FastAPI already has `Principal`, agent tokens, and `middleware/agent_scope.py`. A second implementation in Node is a drift generator, not a second defence. |
| **Intelligence / Automatic Learning** | Indefinitely. Enterprise tier, early access, closed, and it cannot add a capability — it tunes prompts. Aleph's thesis is revertible, inspectable artifacts. An opaque self-mutating prompt is a contamination risk for a research tool. |
| **Channels (Slack/Teams)** | An actual requirement appears. Genuinely Node-only; genuinely re-creatable later as a small standalone host, which is CopilotKit's own advice for Python backends. Name it a deferred capability, not a blocker. |
| **Upgrading to 1.68.2** | After steps 2–3. Note the trap: 1.68 auto-mounts the A2UI renderer from the `a2ui` prop, so Aleph's `renderActivityMessages={[createA2UIMessageRenderer(...)]}` becomes a documented HIGH-severity duplicate-and-race. Correct at 1.58, wrong at 1.68. Both changes must land in one commit. |

---

## 6. What Aleph should adopt from CopilotKit that it is not using today

Ranked by **value per unit of effort**. Note that the top five are client-side or Python-side and are
**unaffected by the runtime decision** — which is itself an argument that the runtime question has
been absorbing attention out of proportion to its stakes.

| # | API | What it unlocks | Effort | Runtime needed? |
|---|---|---|---|---|
| **1** | **`extractCatalogComponentSchemas(catalog)`** + `buildCatalogContextValue(catalog)` (`@copilotkit/a2ui-renderer`, installed) | Derives what the agent is told from the catalog that actually renders. Aleph maintains **two independent descriptions** of the same 21 components — zod3 schemas in `aleph-catalog-v09.tsx` and JSON Schema in `catalog.json`. `check-catalog-generated.sh` pins the generated copies to `catalog.json`; **nothing pins the two descriptions to each other.** This is the `ClaimCard.confidence` bug class, still live. | Hours | No |
| **2** | **`filterCatalog` + `copilotkit.setCatalogComponents` + `isCatalogComponentEnabled` + `onCatalogComponentsChanged`** (react-core v2, installed at 1.58) | **This is the product thesis, already built.** Turning a component off removes it from what can paint *and* from what the model is told exists, in one operation. The library's own comment names the failure it guards against: "disabling never actually removes them from what the model sees — a silent enforcement divergence." Aleph must still add the one thing CopilotKit does not: a kernel guardrail against disabling load-bearing capability. | ~1 day | No |
| **3** | **`new MessageProcessor([catalogA, catalogB, …])`** (`@a2ui/web_core`, installed 0.10.0) | Per-plugin catalogs, namespaced by `catalogId`, routed per surface — A2UI's native design. `A2UISurfaceView.tsx:105` and `SurfaceStreamProvider.tsx:91` already call this constructor with an array of one. Making it an array of many is a **one-line change** and per-plugin catalogs become real on the pane path immediately. | ~1 hour + a plugin registry | No |
| **4** | **`ActivitySnapshotEvent` / `ActivityDeltaEvent`** (`ag_ui.core.events`, **already installed**) | Emit chat cards straight from Python. Meets the deletion exit condition; also gives the deterministic, cheap card path that should be the default. See §2.2 for the exact four-field shape. | 1–2 days | It removes the need for one |
| **5** | **`useHumanInTheLoop`** (react-core v2, exported at 1.58 — verified in the bundle) | Aleph has `ApprovalCard` and `ActionRouter` but no protocol-level gate, so approval **races** the run instead of blocking it. This hook's synthesized handler returns a Promise that resolves only on `respond()`, so the agent genuinely waits. Two hang conditions: never calling `respond` (including on reject) locks the thread; unmounting mid-execute abandons the promise. | ~1 day | No |
| **6** | **`useCapabilities` + `AgentCapabilities`** (`@ag-ui/core` 0.0.53, installed) | The "interface appears with the ability" primitive, from the other direction: the agent declares its identity, tool list with schemas, sub-agents, multimodal I/O and human-in-the-loop support. The type's own docstring says it is "for discovery UIs, **marketplaces**." Paired with item 1 you get a bidirectional capability handshake; Aleph has neither direction today. | ~1 day + populating it | Reads it from `/info` — whoever serves that |
| **7** | **The memory contract** (route shapes are public; the store is paid — implement it yourself) | Supersede-not-patch with a returned `retiredId`; non-lossy delete; `sourceThreadIds` as provenance; semantic dedupe on write (`absorbed: true`); user/project scope; **per-run permission grants** (`memory: { user: "read", project: "read-write" }`) with typed pre-flight refusal. This is the belief spine's revision model, independently rediscovered. Aleph has pgvector and a ledger; build it over Aleph's Postgres, pay nothing, and CopilotKit's OSS `useMemories` hook may drive it unchanged. | 1–2 weeks | No — FastAPI serves it |
| **8** | **`POST /annotate`** shape (`{ type, threadId, payload, clientEventId, occurredAt }`, idempotent, user resolved server-side) | Feedback capture. Aleph has **no way** for an analyst to mark an answer accepted, edited or rejected, so it discards the only signal any future learning loop could run on. Serve the route in FastAPI, write it to the ledger. | ~2 days | No |
| **9** | **`createA2UIMessageRenderer({ onAction })`** — the `A2UIActionInterceptor` (1.68) | The sanctioned seam for per-plugin action policy: see every action a surface dispatches, rewrite it, handle it client-side, or pass it through. Aleph currently intercepts by hand inside `adapt()` in `aleph-catalog-v09.tsx`. | ~half a day | No (needs 1.68) |
| **10** | **`openGenerativeUI` + `useSandboxFunctions`** (renderer present in the installed 1.58 bundle) | Tier 3 of the trust model with a real boundary: the agent emits HTML/SVG into a cross-origin `@jetbrains/websandbox` iframe whose only reach back is a **typed allow-list of host functions you declare**. That is precisely the capability-scoping primitive the sandboxed-plugin tier needs, and Aleph's code-runner sandbox posture already fits. | ~2–3 days for a scoped prototype | Client-side activation; runtime emitter for the streamed variant |

**Deliberately not on this list:** `BuiltInAgent` (its model resolver understands `openai/…`,
`anthropic/…`, `google/…`, `vertex/…` — it cannot route through Aleph's LiteLLM gateway);
`defineTool` (unreachable without `BuiltInAgent`); Intelligence, Automatic Learning, Product Analytics
(paid, closed, and Aleph's Langfuse + OTEL already cover the last one).

---

## 7. What to record in the repo

Three edits, in the style CLAUDE.md asks for — claims you can verify, or marked as planned.

1. **`docs/decisions.md`** — a new decision recording (a) that A2UI is cargo and AG-UI is the truck,
   so the per-plugin-catalog design is not bound to any implementation language; (b) that Aleph runs
   **two** A2UI host paths and only the chat lane depends on CopilotKit; (c) the deletion exit
   condition from §4.
2. **`CLAUDE.md` → Known broken** — rewrite the credential-forwarding entry per §3, and add the
   version-skew entry (browser 1.58 / Node 1.63.2 / current 1.68.2) since it is a live
   `VERSION_MISMATCH`-class risk that CI does not catch.
3. **`docs/architecture.md`** — the two-render-path table from §2.1. Its absence is why five documents
   in a row over-scoped the blast radius of deleting one Node program.

---

## Appendix — evidence index

Everything below was read in this tree or downloaded today (2026-08-20).

| Claim | Where |
|---|---|
| `render_a2ui` prompt-referenced, undefined in Python | `apps/api/src/aleph_api/copilot_agent.py:95,103`; `subagents/viz_builder.py:9`; no other hits under `--include="*.py"` |
| Pane path never touches CopilotKit | `apps/web/src/a2ui/SurfaceStreamProvider.tsx:18,91`; `A2UISurfaceView.tsx:12,105,180` — imports `@a2ui/web_core`, not `@copilotkit/*` |
| Pane surfaces are emitted by Python over Aleph's SSE | `apps/api/src/aleph_api/routes/surfaces.py:177,290,302`; `packages/aleph-a2ui/src/aleph_a2ui/{messages,surface_streamer}.py` |
| `aleph_a2ui` is 1,565 lines of Python | `find packages/aleph-a2ui/src -name "*.py" \| xargs wc -l` |
| The A2UI paint event shape | `apps/copilot-runtime/node_modules/@ag-ui/a2ui-middleware/dist/index.mjs` — `buildLifecycleActivity`, `createA2UIActivityEvents`, `A2UIActivityType = "a2ui-surface"`, `A2UI_OPERATIONS_KEY = "a2ui_operations"` |
| `ActivitySnapshotEvent` / `ActivityDeltaEvent` installed in Python | `.venv/lib/python3.13/site-packages/ag_ui/core/events.py:202-219`; `ag-ui-protocol` 0.1.18 |
| Aleph's click path writes a ledger row | `packages/aleph-a2ui/src/aleph_a2ui/action_router.py` (module docstring, steps 1–4); `apps/api/src/aleph_api/routes/cards.py:69,155` |
| MCP Apps + open-gen-UI renderers in the installed client | `@copilotkit/react-core@1.58.0/dist/copilotkit-BIn7HE8f.mjs` — `MCPAppsActivityType = "mcp-apps"`, `OpenGenerativeUIActivityType = "open-generative-ui"`, `buildSandboxHTML`, `MCPAppsRequestQueue`; `@jetbrains/websandbox` in `package.json` deps |
| MCP Apps server half is Node-only | `docs.copilotkit.ai/integrations/langgraph/generative-ui/mcp-apps` (via copilotkit-mcp); `@ag-ui/mcp-apps-middleware` installed under `apps/copilot-runtime/node_modules/@ag-ui/` |
| License gate 1.58 vs 1.68 | `@copilotkit/shared@1.58.0/dist/index.mjs` → `checkFeature: () => true`; `@copilotkit/shared@1.68.2` (npm pack, read today) → `status !== "expired" && status !== "invalid"`; `LicenseWarningBanner` / `InlineFeatureWarning` / feature ids `chat`,`sidebar`,`popup` in react-core 1.58 bundle |
| Header forwarding policy exists at 1.63.2 | `apps/copilot-runtime/node_modules/@copilotkit/runtime/dist/v2/runtime/handlers/header-utils.mjs` — `DEFAULT_DENY_HEADER_NAMES`, `DEFAULT_DENY_HEADER_PREFIXES`, `mergeForwardableHeaders` |
| Aleph sets `cors: true` and no client auth header | `apps/copilot-runtime/src/server.ts`; `apps/web/src/lib/copilot.tsx` |
| Current published versions | `npm view @copilotkit/runtime version` → 1.68.2; `@copilotkit/react-core` → 1.68.2; PyPI `ag-ui-langgraph` → 0.0.43, `ag-ui-a2ui-toolkit` → 0.0.4 |
| Aleph's installed versions | `apps/copilot-runtime/package.json` (runtime 1.63.2, `@ag-ui/client` 0.0.57); `node_modules/.pnpm/@copilotkit+react-core@1.58.0_*`; `.venv` (`ag_ui_langgraph` 0.0.36, `ag-ui-protocol` 0.1.18, `copilotkit` 0.1.91) |
| 1.68 auto-mounts the A2UI renderer | `.agents/skills/a2ui-renderer/SKILL.md` — "Auto-activates via /info — do NOT manually pass renderActivityMessages" |
| Aleph is already an MCP server | `packages/aleph-a2ui/src/aleph_a2ui/mcp_server.py` |
| Chat lane is e2e-tested against the Node runtime | `tests/playwright/specs/07-copilotkit-a2ui.spec.ts` (asserts `/info` returns `a2uiEnabled: true`) |
