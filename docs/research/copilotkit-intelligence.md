# CopilotKit Intelligence, and whether it matters to a self-improving workbench

Research pass, 20 August 2026. Area: **intelligence**.
Read-only investigation. Sources: the thirteen maintainer-written skills in `.agents/skills/`,
the installed `@copilotkit/*` packages under `apps/copilot-runtime/node_modules/`, CopilotKit's
own documentation MCP server (docs + library source), and the public marketing and pricing pages.

---

## In one paragraph

CopilotKit sells a paid backend called **Intelligence**. It does four things: it stores
conversations durably so they survive a page reload (**Rich Threads**), it gives the agent a
long-term **memory** it can read and write, it lets the same agent answer in Slack and Teams
(**Channels**), and — still in early access, on the top price tier only — it watches which
answers people accept, edit, or reject and quietly rewrites the agent's prompt to do better next
time (**Automatic Learning**). The important discovery is where CopilotKit draws its own
open-source line. Their docs state it plainly: *"the dividing line between CopilotKit open source
and CopilotKit Intelligence is durable data."* Everything that runs without a database is free;
everything that touches a database is the paid product. **Aleph already owns the database.** It
has Postgres, project scoping, an append-only hash-chained ledger, pgvector retrieval, and a
belief layer with evidence anchors and retraction — a stricter, better-governed version of exactly
the thing CopilotKit charges for. So the value here is not the product; it is the *interface
design*. CopilotKit has published, in working code, a well-thought-out contract for scoped agent
memory, per-run permission grants, and feedback capture. Aleph should read those contracts closely
and implement them over its own storage. It should not buy the platform, and — on the evidence in
this area — Automatic Learning is **not** a competitor to Aleph's thesis. It is a prompt-tuning
loop wearing the word "skill". It cannot give an agent an ability it does not already have, which
is the entire point of Aleph.

---

## Terms, defined once

| Term | What it actually means |
|---|---|
| **AG-UI** | The wire protocol between an agent and a user interface. Typed JSON events (message deltas, tool calls, state patches) over SSE, WebSocket, or plain HTTP. Aleph already speaks it. |
| **A2UI** | *Not* a protocol. A declarative description of a UI — "draw a card with these fields" — as a structured payload that rides **inside** AG-UI. Aleph already emits it. |
| **MCP** | Model Context Protocol. How an agent reaches *tools and data* (a database, an API). Client/server JSON-RPC. |
| **A2A** | Agent2Agent. How one agent delegates to *another agent* it did not write. |
| **Intelligence** | CopilotKit's paid, closed-source backend service. Not a library. |
| **Rich Thread** | A conversation stored server-side as a replayable event log, not a message list. |
| **Memory** | Durable facts about a user or project, embedded for search, that the agent can recall and write. |
| **Learning Container** | A named bucket that feedback is filed into. The scope key for "who is this learning for" — one user, one team, one org. |
| **Annotation** | One recorded human reaction to something the agent did (accepted, edited, rejected, clicked). The raw input to learning. |
| **CLHF** | Continuous Learning from Human Feedback. CopilotKit's name for the loop. |

---

## 1. Automatic Learning, mechanically

This is the part the assignment asked me to press hardest on, so here is exactly what I could
establish and exactly where the evidence stops.

### What is published

Marketing (`copilotkit.ai/copilotkit-intelligence`) says:

- *"Agents improve from human feedback with in-context learning. No fine-tuning pipeline required."*
- *"Users show the agent a task once. It learns the skill."*
- Learning containers scoped **per user, per group of users, or per organization**.
- Signals captured: **approvals, edits, retries**, ignored suggestions.
- *"New skills created from real usage"* that *"keep improving over time. You do nothing."*
- Output is **"exportable as fine-tuning datasets"** if you want to train a model later.

The engineering blog is more careful and calls it **CLHF — Continuous Learning from Human
Feedback** — improving *"from real usage, without the labeling pipelines or fine-tuning cycles"*,
via **"prompt-level adjustments at runtime, per user and per context."**

### What the code actually shows

Three concrete pieces exist in the open-source runtime, and they define the whole shape of the
feature. This is the mechanism, in order:

**Step 1 — capture a human reaction.** A React hook `useLearnFromUserAction` calls a low-level
function `recordAnnotation`, which POSTs to `{runtimeUrl}/annotate`:

```
POST /api/copilotkit/annotate
{ "type": "user_action", "threadId": "...", "payload": { ... },
  "clientEventId": "<uuid>", "occurredAt": "<iso8601>" }
```

`clientEventId` is an idempotency key — a retry returns `{ id: <original>, duplicate: true }`
rather than double-counting. The user id is **never** sent by the browser; the server resolves it
from its own auth. The runtime forwards to the platform's `PUT /connector/annotate/:clientEventId`.
The handler's own comment says the `"user_action"` type *"records a user UI interaction for the
self-learning loop."*

**Step 2 — file it into a container.** The runtime option `ɵlearning.containerId` — the `ɵ` prefix
is CopilotKit's marker for "internal, unstable, may change" — *"chooses one stable Learning
Container ID for each web or Channel run."* It is either a fixed string or a callback receiving
`{ surface: "web" | "channel", threadId, runId, agentId, userId, deliveryId }`, returning a
1–64 character kebab-case id. That callback **is** the "per user / per group / per org" feature:
you return `user-42`, or `team-oncology`, or `acme-corp`, and that is the scope the learning
accrues to. A deprecated older hook `useLearningContainers` sent a plural
`set_learning_containers` annotation and defaulted to `["project"]`.

**Step 3 — the platform adjusts the prompt.** This step is **not in any open-source package and
not documented anywhere public.** The runtime hands annotations and a container id to a closed
service and gets back a better-behaved agent. The blog's phrase "prompt-level adjustments at
runtime" is the most specific public statement that exists.

### What a "skill" is here — and what it is not

There is **no skill API**. I searched the entire CopilotKit documentation corpus, the entire
published library source, and all thirteen maintainer skills. `self_learning` appears exactly once
in shipped code — as a **licence feature flag**, listed alongside `analytics` and `msteams` in
`FEATURE_IDS` in `@copilotkit/license-verifier`. There is no skill type, no skill format, no
`recordSkill` call, no skill store, no skill list, no way to inspect, edit, version, test, disable,
or delete a skill. Nothing renders one. The word "skill" on the marketing page describes an
*outcome* — the agent behaves as if it learned something — not an artifact you can hold.

Given the mechanism (annotations → container → prompt augmentation), a "skill" is almost certainly
a summarised trajectory: the platform notices a repeated accepted pattern, distils it into text,
stores it in the container, and injects it into the system prompt for later runs in the same
container. That is in-context learning. It is a good technique. It is not a program.

### The key judgement: competitor, component, or different thing?

**A different thing wearing similar words — with one component-shaped idea worth taking.**

The test is simple: *can this loop give the agent an ability it did not previously have?*

- **CopilotKit's answer: no.** It can only bias how the model uses tools that a developer already
  registered. If the agent has no way to query Crossref, no amount of accepted feedback will
  produce one. The capability set is fixed at deploy time by a human; learning tunes the language
  in front of it.
- **Aleph's answer: yes, that is the entire product.** The agent authors a plugin — code, tools,
  an A2UI catalog, a settings card — and the kernel activates it, with guardrails preventing the
  removal of load-bearing capability. The unit of learning is a **revertible, inspectable,
  composable artifact**, not a prompt fragment.

These operate at different layers and do not compete. If anything they compose: Aleph's kernel
adds *capabilities*; a CLHF-style loop would tune *how the agent chooses among them*. Aleph could
want both eventually. But Aleph must not confuse the two, and must not treat CopilotKit shipping
"self-improving agents" as prior art on its thesis. It isn't. It is prompt tuning with good
marketing.

There is one further reason not to buy it even if you wanted the loop: **it is a black box.** You
cannot see what the agent learned, you cannot review it before it takes effect, you cannot revert
one lesson, and you cannot ask why it behaved that way. For a *research* workbench — where the
whole discipline is that a claim is traceable to evidence and a state change is traceable to a
ledger row — an opaque, silently-mutating system prompt is not a feature, it is a contamination
risk. If Aleph builds this, the lessons must be first-class rows with provenance and a revert path.
That is a natural fit for the belief spine's existing shape, and a poor fit for a vendor's
hosted prompt store.

**Availability, plainly:** Self-Learning is **Enterprise tier, "Early access"** on the pricing
page, and "Coming Soon" on the product page. It is not something Aleph could adopt today even by
paying the Team price.

---

## 2. Memory — the most valuable thing found in this area

This is the part the prior pass missed entirely, and it is worth more to Aleph than everything
else on the Intelligence page combined — **not as a service to buy, but as a contract to copy.**

CopilotKit ships a complete, thoughtful long-term-memory design. The client hooks are open source
(`useMemories` in React, `injectMemories` in Angular); the store behind them is the paid platform,
and every route returns **422** without it. The contract:

| Operation | Route | Behaviour |
|---|---|---|
| List | `GET /api/memories?includeInvalidated=true` | Newest first; opt in to retired rows |
| Create | `POST /api/memories` | `{ content, kind, scope, sourceThreadIds }` |
| Supersede | `PATCH /api/memories/:id` | Retires `:id` **and** inserts the replacement atomically; response carries `retiredId` |
| Retire | `DELETE /api/memories/:id` | *Non-lossy* delete — the row survives, flagged |
| Recall | recall route | `{ query, limit, scope }`, semantic search |
| Subscribe | subscribe route | Live `memory_metadata` deltas over a per-user channel |

Design decisions worth stealing, each of which Aleph would otherwise have to discover the hard way:

- **Scope is `user` or `project`**, default `user`. Not global. (Aleph's rule that every row carries
  `project_id` is the same instinct, already stronger.)
- **Update is supersede, never patch.** A full replacement retires the old row and mints a new id.
  The docs warn explicitly that omitting `sourceThreadIds` *resets* rather than preserves — no
  silent partial merges. This is exactly the belief spine's revision model.
- **`sourceThreadIds` is provenance.** Every memory points back at the conversations that produced
  it. That is a citation, in all but name.
- **Delete is non-lossy.** Nothing is destroyed; it is invalidated and can still be listed. That is
  Aleph's retraction semantics, arrived at independently.
- **Write dedupes semantically.** The create response carries `absorbed: true` when the content was
  merged into a near-duplicate instead of inserted — so the store does not fill with restatements.
- **Access is granted per run, not per install.** `memory: { user: "read", project: "read-write" }`
  on the *call*. Omitting a scope disables it. A single `memory.access(...)` policy callback on the
  runtime decides the grant for one request, and it distinguishes `consumer: "agent" | "client"` —
  the agent and the browser can get different grants from the same policy. Failures are typed and
  pre-flight: `channel_memory_user_required`, `channel_memory_grant_invalid`,
  `channel_memory_subject_required`. The run refuses to start rather than half-executing.
- **Identity is server-resolved, always.** Every handler resolves the user from server-side auth and
  explicitly never trusts a client-supplied id. (Aleph learned this one the expensive way — see the
  agent-scope defect in CLAUDE.md.)
- **The client degrades honestly.** `isAvailable` flips false on 404/501 and the UI hides memory
  controls; `realtimeStatus` separately reports whether the live feed died, so the UI can stop
  showing a "live" badge over a frozen snapshot.
- **Agents reach memory over MCP.** The platform exposes memory to the agent as a JSON-RPC MCP
  endpoint — `POST {INTELLIGENCE_API_URL}/mcp`, `tools/call` with `recall_memory`, scoped by an
  `X-Cpki-User-Id` header, responding as an SSE stream. A documented gotcha: recall defaults to
  *user* scope with a small top-N, so a UI panel hard-coded to user scope will hide project
  memories the agent is visibly using.

Aleph has pgvector, project scoping, a ledger, and a claim model with evidence anchors. It could
implement this entire contract over its own Postgres in a bounded amount of work, get a memory
surface that its own belief engine governs, and — because the route shapes are public — have a
decent chance that CopilotKit's own `useMemories` hook would drive it unchanged.

---

## 3. Rich Threads — what "full-fidelity, persisted, resumable" really means

Concrete, and less magical than it sounds.

**Stored:** the **raw AG-UI event stream**, not a snapshot of the final message list. Every message,
tool call, and state change; generative-UI components *with their state*; human-in-the-loop
confirmations, overrides and edits; bidirectional shared state; voice transcripts and audio; file
uploads and attachments. Storing the event log rather than a snapshot is what makes a returning
client able to fetch only the events it missed instead of reloading everything.

**Resumable:** on reopening a thread, the platform checks for an in-flight run. No active run →
return history, client replays. Active run → return history **plus** open a WebSocket, client
replays then joins live. Threads take a **run lock** (Redis, default 20s TTL, heartbeat every 15s,
capped at one hour) so two clients cannot run the same thread at once; a second attempt gets HTTP
409, surfaced to the client as the typed code `agent_thread_locked`. State is reconstructed by
folding RFC-6902 JSON-Patch `STATE_DELTA` events onto the last `STATE_SNAPSHOT`.

**Thread management:** list (paginated), rename, archive (soft, reversible, hidden by default),
delete (permanent, irreversible), all synced across tabs and devices over WebSocket. Auto-naming
generates a 2–5 word title with **one LLM call per new thread**, on by default — turn it off with
`generateThreadNames: false` unless you want that spend. Thread history and *branching from any
point* is listed on the product page.

**What Aleph can have without paying anything.** This matters. The `/threads` CRUD routes are
Intelligence-only, but **durability is not**. The runtime's `AgentRunner` is a four-method abstract
class — `run`, `connect`, `isRunning`, `stop` — and swapping it swaps where run state lives:

- `InMemoryAgentRunner` — the default, and **what Aleph is running today**. Process-global map, lost
  on restart, divergent across replicas.
- `SqliteAgentRunner` — file-backed, restart-resilient, single-instance.
- **A custom runner** — the docs ship a Redis/Postgres skeleton. `run()` takes a distributed lock
  and appends each event to a durable stream; `connect()` replays it.

Aleph has Postgres and Redis already. A `PostgresAgentRunner` writing the AG-UI event stream into
Aleph's own tables would give resumable, restart-surviving, ledger-visible threads with **no
licence, no cloud, and no vendor**. Aleph's threads should carry `project_id` and write ledger rows
anyway, so a bought thread store was never going to satisfy its own house rules. This is the single
highest-value item in this document that Aleph can act on today.

---

## 4. Licensing and hosting — resolving the contradiction

The prior pass claimed Intelligence is *"explicitly not self-hostable from the OSS packages."* The
marketing page says self-hosted with offline licensing. **Both are true, and the distinction is the
whole point.** Evidence:

**Self-hosting is real and documented.** `docs.copilotkit.ai/premium/self-hosting` describes a Helm
chart at `oci://ghcr.io/copilotkit/charts/intelligence` deploying **`app-api`** (:4201),
**`app-frontend`** (:8080), an optional **`realtime-gateway`** (:4401), a `database-migrations` Job
and a `thread-culler` CronJob. It requires Kubernetes 1.28+, Helm 3.12+, PostgreSQL 14+, Redis 7+
(Valkey/ElastiCache fine), and an OIDC issuer (Keycloak, Okta, Entra, Auth0, Google Workspace).
A bundled Keycloak subchart exists for evaluation only. AWS and on-prem example values ship.

**But none of it is in the open-source packages.** The evidence is unambiguous:

- `@copilotkit/runtime-enterprise` — referenced by name in a published type comment as living in a
  *different, internal* monorepo — **404s on npm**. It is not public.
- The OSS repo ships only `@copilotkit/license-verifier` (MIT), which does exactly one thing:
  verify an **Ed25519-signed licence token offline** against a baked-in master public key, with a
  runtime key-attestation chain. Offline verification is the "air-gapped licensing" claim, and it
  is a *client-side gate*, not the feature.
- Internal package names leak through the published typings (`@cpki/ops-contracts`,
  `@cpki/license-catalog`, `ops-api`, `app-api`) — a closed monorepo the OSS runtime is a "Layer 3
  consumer" of.
- Maintainer skill `runtime/references/intelligence-mode.md` states flatly: *"Self-hosting
  Intelligence is not yet supported"* from the SDK's point of view, `organizationId` is *"reserved
  for future self-hosted deployments"*, and the recommended on-prem answer today is *"SSE mode +
  `SqliteAgentRunner`."* That file is somewhat behind the docs site, but its underlying claim —
  the SDK cannot stand this up, only connect to it — holds.

**The licence, decoded from the verifier's own types.** Tiers: `free`, `developer`, `pro`, `team`,
`team_self_hosted`, `enterprise`. Feature ids: `threads.retention_hours`, `threads.max_count`,
`multimodal_storage_gb`, `sdk.angular`, `deployment_via_helm_chart`, `analytics`, **`self_learning`**,
`msteams`. Payload also carries `seat_limit`, `remove_branding`, `expires_at`, and a `telemetry_id`
used to correlate outbound analytics back to the HubSpot contact who issued the licence. Note that
**self-hosting itself is a licence flag** (`deployment_via_helm_chart`) — the chart is public, the
right to run it is not.

**Price (public pricing page, checked today):**

| Tier | Price | Threads | Self-host | Self-Learning | Analytics |
|---|---|---|---|---|---|
| Developer | Free | 200, 3-day retention, 1 GB | "Runtime only" | No | No |
| Pro | $39/mo | 5,000, 5-day, 10 GB | "Runtime only" | No | No |
| Team | **$100/seat/mo**, up to 5 seats | 25,000, 14-day, 100 GB | **Yes, incl. database** | No | No |
| Enterprise | Custom (reported $5k/mo+) | Unlimited, custom | Yes + VPC/on-prem | **Early access** | **Early access** |

**Verdict for Aleph.** Self-hosting starts at Team, $100/seat/month, and buys a Kubernetes
deployment of a closed-source Postgres/Redis application whose job is to store threads and
memories. Aleph is a docker-compose, single-operator research workbench that **already runs
Postgres and Redis** and already has stricter rules about what a stored row must carry. Standing up
a second, opaque, licence-gated database next to the one Aleph governs would be a net loss of
control for a recurring fee. And the one feature that might have justified it — Automatic Learning —
is Enterprise-only, early access, unavailable at any published price. **Do not buy this.**

The one thing worth carrying forward: **`@copilotkit/license-verifier` (MIT) is a clean,
readable reference implementation of offline, signed, feature-flag entitlement** — Ed25519, baked
master key, attested runtime keys, typed boolean/numeric feature catalog, grace period, severity
levels. If Aleph ever ships plugins with tiered trust or third-party distribution, that file is
worth reading before designing anything.

---

## 5. Channels — an honest opinion

**What it is.** One agent, reachable from Slack, Teams, Discord, WhatsApp, or Telegram. Two
architectures:

1. **Managed** (`@copilotkit/channels` + Intelligence). CopilotKit holds the Slack signing secret
   and bot token, owns the public ingress URL, and delivers each turn to your process over an
   **outbound** WebSocket. No tunnel, no public URL of your own, no platform credentials in your
   app. Requires an Intelligence licence — `channels` is typed `undefined` in SSE mode and the
   constructor throws if you pass it.
2. **Direct adapters** (`@copilotkit/channels-slack` / `-teams` / `-discord` / `-telegram` /
   `-whatsapp`, all MIT, all v0.9.0). You hold the provider credentials and talk to Slack yourself.
   No licence. CopilotKit's docs note you can fully self-host the Channels SDK and *"implement the
   durable-data layer yourself"* — onboarding guides "coming soon".

**Both require a long-running Node process.** This is the only Intelligence-adjacent capability with
a hard Node dependency and no Python equivalent. A Next.js route, a Lambda, or an edge function
cannot host one, because activation holds a socket open.

**My honest opinion: Aleph does not need this now, and should not let it drive an architecture
decision — but it is the one thing in this area that a Node service uniquely buys.**

Against it: Aleph is a single-operator research workbench. Channels solves a *team* problem —
meeting colleagues where they already talk. Aleph has no colleagues in the loop. The failure
surface is large and genuinely nasty: the maintainer skills spend thousands of words on ways a
Channel installs cleanly, reports green, and silently answers nothing — a short-scoped Slack token,
a `socket_mode_enabled: true` manifest, a missing `ready()` on a deferring mount, a version skew
that rejects every delivery at the join boundary. Five systems owned by four parties have to agree.
That is a lot of operational surface for a workbench with one user. Managed Channels also carry
real limits: **no slash commands** and **no modal submissions** on the managed path — mentions,
messages, reactions, and button clicks only.

For it, honestly: there is one shape where this stops being enterprise decoration. Aleph runs
**long research jobs** — ingest, the deep-research loop, reviewers. "Kick off a literature sweep
from your phone, get pinged in Slack when the findings land, approve or reject a claim from a
button in the message" is a genuinely good fit for a single researcher who is not at their desk,
and Channels' human-in-the-loop buttons do work on the managed path. If Aleph ever wants that, it
needs a long-running Node host. That is a real capability, and it is worth naming as a cost of
deletion — but it is a *someday* capability, not a reason to keep 75 lines of Node running today.

---

## 6. MCP vs A2A vs AG-UI — and Aleph over MCP

CopilotKit's framing (`copilotkit.ai/learning/mcp-vs-a2a-vs-ag-ui`) is clean and I think correct.
Three protocols answering three different questions about **who is on the other end**:

| Protocol | Connects agent to | Shape |
|---|---|---|
| **MCP** | tools and data | JSON-RPC client/server; servers expose tools, resources, prompts |
| **A2A** | *peer agents* | Agents publish "Agent Cards"; a task lifecycle exchanges messages and artifacts over JSON-RPC/SSE |
| **AG-UI** | *users, inside an app* | Typed JSON events — lifecycle, text, tool call, state, custom — over SSE, WebSocket, or HTTP. **The only bidirectional one.** |

**A2UI is explicitly not a fourth protocol.** It is a declarative generative-UI *specification* — a
structured payload describing the interface an agent wants drawn — and it is transport-agnostic:
it *"rides over a protocol like AG-UI."* This settles a question Aleph has circled: A2UI is cargo,
AG-UI is the truck. Aleph's per-plugin catalogs are cargo manifests. Nothing about the "every
plugin publishes its own A2UI catalog" design is coupled to the truck's implementation language.

Their recommended adoption order: **MCP first** (most teams need tools before anything else),
**AG-UI the moment real users face the agent**, **A2A only when independently built agents must
discover and delegate to each other.**

**On the owner's open question — exposing Aleph over MCP to outside clients.** This framing makes
the answer sharp, and it is a genuinely attractive move:

- MCP and AG-UI are **orthogonal, not alternatives**. Serving Aleph's capabilities as an MCP server
  takes nothing away from the AG-UI surface the web app uses. They are different doors.
- What Aleph would expose is unusually well-suited to it: corpus search, claim lookup, citation and
  provenance walks, DOI verification, dataset queries. These are exactly "tools and resources over
  JSON-RPC" — read-mostly, typed, individually meaningful. Claude Desktop, Claude Code, or any other
  MCP client could then use Aleph's *research corpus* directly.
- It fits the plugin thesis with almost no strain: a plugin that already declares typed tools and an
  A2UI catalog is one adapter away from also declaring MCP tools. **One capability, three doors** —
  the web UI over AG-UI, an outside client over MCP, and (someday) Slack over Channels.
- Two cautions carried over from Aleph's own hard-won rules: every MCP tool call still has to
  resolve a `Principal`, stay inside one `project_id`, and write a ledger row — MCP has no opinion
  about authorization and will happily let you build a globally-scoped door into a project-scoped
  system. And a *write* tool exposed over MCP is an agent writing state directly, which Aleph's
  rules forbid; expose reads first.
- A2A is the one to ignore for now. Aleph has no peer agents built by other people.

There is also a live example of MCP used *inward* rather than outward, worth noting as a pattern:
Intelligence exposes its own memory to agents as an MCP server (`recall_memory`). Aleph could do
the same thing to its belief spine — the agent reaches claims through a tool boundary rather than
through direct database access, which is exactly the posture Aleph's rules already demand.

---

## 7. Capabilities found, and whether Aleph uses them

| Capability | Where it lives | Aleph today | What it would unlock |
|---|---|---|---|
| Durable, resumable threads via a custom `AgentRunner` | `@copilotkit/runtime` v2, **OSS** | **No** — running the default `InMemoryAgentRunner` | Conversations survive restart; reconnect mid-run; a real thread history |
| Thread CRUD + realtime sync (`useThreads`) | `@copilotkit/react-core` hook OSS, routes 422 without Intelligence | No | Thread sidebar, rename/archive, cross-tab sync — only if Aleph serves the routes itself |
| Scoped long-term memory (`useMemories`, supersede + retire + provenance + per-run grants) | Hooks OSS, store licensed | No | The best contract in this document to copy over Aleph's own Postgres |
| Memory-to-agent over MCP (`recall_memory`) | Intelligence platform | No | Pattern for exposing the belief spine to the agent through a tool boundary |
| Feedback capture (`/annotate`, `useLearnFromUserAction`, idempotent) | Route + hook OSS, sink licensed | No | The ingestion half of any learning loop — Aleph can serve `/annotate` itself |
| Learning Containers (`ɵlearning.containerId`) | OSS option, `ɵ` = unstable, throws in SSE mode | No | Scope key for per-user/team/org learning. The idea is free; the engine is not |
| Automatic Learning / CLHF | **Enterprise, early access, closed** | No | Prompt tuning from feedback. Adjacent to Aleph's thesis, not it |
| Channels (Slack/Teams/Discord/WhatsApp/Telegram) | Managed = licensed; direct adapters MIT. **Node-only, both** | No | Long research jobs answerable from a phone. Needs a long-running Node host |
| Offline signed licensing (Ed25519, feature catalog) | `@copilotkit/license-verifier`, **MIT** | No | Reference design if Aleph ever tiers or distributes plugins |
| Analytics / lakehouse / OTLP export | Enterprise, early access | Partly — Aleph has Langfuse + OTEL already | Little. Aleph's observability is comparable and self-owned |
| Self-hosted platform (Helm, k8s, Postgres, Redis, OIDC) | Team tier+, closed images | No | A second opaque database. Not wanted |

---

## 8. Where the prior recommendation was right

Being fair to it:

- **Nothing in Intelligence rescues `apps/copilot-runtime` as it stands.** Threads, memory,
  annotations, and learning **all** hard-fail without a paid Intelligence licence — 422, or a
  constructor throw. Keeping a Node service in the hope of using them is keeping it for something
  Aleph would have to buy, at Enterprise tier, for a feature still in early access.
- **The service as written uses none of this.** All 75 lines: one `HttpAgent`, one `a2ui` config,
  one Node listener. Default in-memory runner. No `licenseToken`, no `intelligence`, no
  `identifyUser`, no `memory`, no `channels`, no runner. Everything discussed in this document is
  a *potential*, not a loss.
- **Aleph is behind.** `@copilotkit/runtime` 1.63.2 here vs **1.68.2** current; `react-core` 1.58
  in the web app. Several APIs cited above (`memory.access`, `ɵlearning`, `useMemories`) may not
  exist in what is installed. Any "keep it for the platform" argument would need an upgrade first.
- **The route contracts do not require Node.** `/annotate`, `/memories`, `/threads` are ordinary
  HTTP shapes. FastAPI can serve them, and if it does, the OSS React hooks are candidates to drive
  them unchanged. **This argues for the FastAPI direction, not against it.**

---

## 9. Verdict on deleting `apps/copilot-runtime`, from this area

**Deleting it loses one real thing and one small one. Neither is urgent, and neither is a reason
to keep the service in its current form.**

**The real loss: Channels.** Slack, Teams, Discord, WhatsApp, Telegram are a **Node-only SDK with
no Python equivalent**, and they need a process that outlives a request. Whether managed (licensed)
or direct-adapter (MIT), a Channel needs a long-running Node host. If Aleph ever wants "start a
literature sweep from your phone, approve the findings from a Slack button", that host has to
exist. Deleting the service today does not make that impossible — a channel host is a small,
self-contained program that proxies to a remote AG-UI agent, and CopilotKit's own guidance for a
Python backend is exactly *"leave it alone; add a small TypeScript channel host that proxies"* — but
it does mean re-creating it later. **Name this as a deferred capability, not a blocker.**

**The small loss: the OSS client hooks stop having a server that speaks their dialect.**
`useThreads`, `useMemories`, `useLearnFromUserAction` all talk to a CopilotKit runtime. Without one,
Aleph either implements those exact route shapes in FastAPI (very achievable — they are documented
above) or writes its own hooks. Given that Aleph's threads and memories must carry `project_id`,
write ledger rows, and respect `access_scope`, it was **always** going to own that server side.
The hooks are worth keeping as a client-side option; the Node process is not required to serve them.

**Everything else on the Intelligence page is a non-loss.** Durable threads are achievable with a
custom `AgentRunner` over Aleph's own Postgres and Redis — no licence, no vendor, and the *only*
version consistent with Aleph's own rules about what a row must carry. Memory is a contract to
implement, not a service to rent. Automatic Learning is Enterprise-tier, early-access, opaque, and
— on the evidence — a prompt-tuning loop that cannot add a capability, which makes it adjacent to
Aleph's thesis rather than a version of it. Analytics duplicates Langfuse and OTEL, which Aleph
already has and already owns.

**The framing that should survive this document.** CopilotKit's own docs draw the open-source line
at durable data. **Aleph is on the paid side of that line already, with better governance.** The
right posture toward Intelligence is not customer and not competitor — it is *reader*. Take the
interface designs, which are genuinely good and hard-won: per-run permission grants, supersede-not-
patch, non-lossy retirement, provenance on every memory, semantic dedupe on write, server-resolved
identity, honest client degradation, idempotent feedback capture. Implement them over the database
Aleph already governs. Pay for none of it.

---

## Sources

- `/Users/jpmullins/Documents/code/aleph/.agents/skills/copilotkit-channels/SKILL.md`
- `/Users/jpmullins/Documents/code/aleph/.agents/skills/channels-setup/SKILL.md`
- `/Users/jpmullins/Documents/code/aleph/.agents/skills/setup-slack-channel/SKILL.md` (+ `references/`)
- `/Users/jpmullins/Documents/code/aleph/.agents/skills/runtime/references/intelligence-mode.md`
- `/Users/jpmullins/Documents/code/aleph/.agents/skills/runtime/references/agent-runners.md`, `agent-runners-custom.md`
- `/Users/jpmullins/Documents/code/aleph/.agents/skills/react-core/references/threads.md`
- `/Users/jpmullins/Documents/code/aleph/apps/copilot-runtime/node_modules/@copilotkit/license-verifier/dist/index.d.ts` — tiers, `FEATURE_IDS`, Ed25519 verification
- `/Users/jpmullins/Documents/code/aleph/apps/copilot-runtime/src/server.ts` — what Aleph's Node service actually does
- CopilotKit library source via their docs MCP: `packages/runtime/src/v2/runtime/core/runtime.ts`, `core/learning.ts`, `handlers/intelligence/memories.ts`, `handlers/intelligence/annotate.ts`, `handlers/shared/memory-policy.ts`, `intelligence-platform/client.ts`, `packages/react-core/src/v2/hooks/use-memories.tsx`, `use-learning-containers.tsx`, `lib/record-annotation.ts`, `packages/channels-core/src/memory.ts`
- `docs.copilotkit.ai`: `/premium/intelligence-platform`, `/premium/threads-explained`, `/premium/self-hosting`, `/channels/index`, `/channels/identity-and-memory`, `/cookbook/angular-adk-agentic-app`
- `copilotkit.ai/copilotkit-intelligence`, `/product`, `/pricing`, `/blog/copilotkit-enterprise-intelligence-platform`, `/learning/mcp-vs-a2a-vs-ag-ui`
- npm registry: `@copilotkit/runtime@1.68.2`, `@copilotkit/channels*@0.9.0`, `@copilotkit/sqlite-runner@1.68.2`; `@copilotkit/runtime-enterprise` → 404
