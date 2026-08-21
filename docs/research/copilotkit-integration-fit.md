# CopilotKit integration fit — what adopting more of it would actually cost Aleph

**Area:** integration-fit. **Date:** 2026-08-20. **Status:** research, read-only.
**Sources:** the thirteen official CopilotKit skills installed at `.agents/skills/` (written by the
CopilotKit maintainers, `library_version: 1.68.2`), the installed packages themselves, the published
package registries, and Aleph's own code.

---

## In one paragraph

Aleph runs a small Node program — `apps/copilot-runtime`, 75 lines — sitting between the browser and
the Python API. A previous review said: delete it, because the one job it does (giving the agent a
tool for drawing UI cards) now exists in Python. That review was right about the tool and wrong about
the service. The Node program does six things, not one, and the other five have no Python equivalent
today. The most immediate: **the `render_a2ui` tool the agent is told to use in its system prompt is
never defined anywhere in Aleph's Python code.** It exists only because the Node program injects it
into every request. Delete the service this week and every generative-UI card in the chat panel goes
dark, while the agent keeps being instructed to call a tool that no longer exists. Separately, Aleph's
wiring of CopilotKit is out of date in a way that *versions alone don't show*: it uses a provider
component the maintainers explicitly say not to use, and wires the card renderer by hand in the exact
way the maintainers flag as a HIGH-severity mistake. The real recommendation is neither "keep" nor
"delete": it is **make the Node service thin and idiomatic (about a day's work), which fixes two
items on Aleph's own Known-broken list, and re-evaluate deletion in six months when the Python A2UI
path has a wire-level lifecycle.**

Terms used below, defined once:

- **AG-UI** — the wire protocol between a browser and an agent. Server-Sent Events (a one-way HTTP
  stream) carrying typed events: run started, text chunk, tool call, tool result, state snapshot, run
  finished. Aleph's FastAPI already speaks this.
- **A2UI** — a separate spec for an agent to *describe* a UI ("a Card containing a Column containing
  these three Texts") as JSON, which the browser then renders against a registered **catalog** of
  components. Aleph has 21 hand-written card components and a `catalog.json`.
- **The runtime** — CopilotKit's server-side half. Not the agent; a layer in front of the agent that
  owns transport, capability advertisement, thread state, and middleware.
- **Middleware** here means: code that wraps the agent, edits the request going in, and edits the
  event stream coming out.

---

## 1. What I verified, concretely

### Version drift

| Thing | Aleph has | Current | Gap |
|---|---|---|---|
| `@copilotkit/react-core` (web) | **1.58.0** (lockfile; manifest says `^1.58`) | 1.68.2 | 10 minors |
| `@copilotkit/runtime` (Node service) | **1.63.2** (exact pin) | 1.68.2 | 5 minors |
| `@copilotkit/a2ui-renderer` (transitive) | 1.58.0 | 1.68.2 | 10 minors |
| `@ag-ui/client` — web tree | **0.0.53** | 0.0.58 | 5 |
| `@ag-ui/client` — Node tree | **0.0.57** | 0.0.58 | 1 |
| `copilotkit` (PyPI) | 0.1.91 | 0.1.95 | 4 patches |
| `ag-ui-langgraph` (PyPI) | **0.0.36** | 0.0.43 | 7 |
| `ag-ui-protocol` (PyPI) | 0.1.18 | 0.1.20 | 2 |

Two things stand out and neither is the headline number.

**First: the client and server disagree inside one repo.** The browser bundles AG-UI client 0.0.53;
the Node service bundles 0.0.57. CopilotKit has an error class for exactly this
(`CopilotKitVersionMismatchError` / `VERSION_MISMATCH`), and the debug skill's very first diagnostic
step is "run `npm ls @copilotkit/runtime @copilotkit/react-core @copilotkit/core @ag-ui/client` —
version mismatches between runtime and React packages are a common root cause." The severity is
dev-only INFO, so it will not fail loudly; it will produce odd behaviour and a console notice.

**Second: `apps/copilot-runtime` is not in the pnpm workspace.** `pnpm-workspace.yaml` lists only
`apps/web` and `tests/playwright`. The Node service has its own `package-lock.json` and its Dockerfile
runs `npm ci`. So the repo runs **two package managers**, and the security `overrides` block in
`pnpm-workspace.yaml` (prismjs, postcss, dompurify, js-yaml — added by Dependabot) **does not apply to
the Node service at all**. CI installs it separately (`npm --prefix apps/copilot-runtime ci`). The
Dockerfile carries a comment recording that this already bit once: the manifest floated `^1.58`,
accidentally resolved 1.63.2, and the deployed container "only worked because of that accidental
float," because `a2ui.defaultCatalogId` did not exist in 1.58's middleware.

That is the honest integration cost of the second language today: not the language, the *second
dependency universe*.

### The v1 → v2 story: Aleph is already on v2, and that part is done

The `copilotkit-upgrade` skill exists to move applications off the GraphQL-based v1 runtime onto the
AG-UI-based v2 runtime. **Aleph does not need it.** Every import is already `/v2`:

- `apps/web/src/lib/copilot.tsx` imports from `@copilotkit/react-core/v2`
- `CopilotChatSurface.tsx` uses `useAgentContext` and `useFrontendTool` — the v2 names, not
  `useCopilotReadable` / `useCopilotAction`
- `ActivityCard.tsx` uses `useAgent` — the v2 name, not `useCoAgent`
- the Node service imports `CopilotRuntime` from `@copilotkit/runtime/v2`
- FastAPI mounts `ag_ui_langgraph.add_langgraph_fastapi_endpoint`, and the code comment records that
  the v1 `CopilotKitRemoteEndpoint` path was tried and abandoned because it "crashes on `dict_repr`"

There is no GraphQL anywhere, no `runtime-client-gql`, no service adapters, no `@copilotkit/react-ui`
in the dependency list. **The v1→v2 migration is complete.** Whoever did it did it properly. That is
worth saying plainly, because it means the remaining gap is not a migration — it is drift and idiom.

---

## 2. Where the prior recommendation was right

Being fair to it first:

1. **`get_a2ui_tools` really does exist in Python.** I downloaded and unpacked
   `ag_ui_langgraph-0.0.43-py3-none-any.whl`. `get_a2ui_tools` is exported from `__init__.py` and
   implemented in `a2ui_tool.py`. It is real, it is current, and it is a thin adapter over
   `ag-ui-a2ui-toolkit` 0.0.4.
2. **The Python toolkit is more capable than I expected.** `ag_ui_a2ui_toolkit` carries a full
   generation pipeline: prompt assembly, catalog resolution from run state, a *prior-surface finder*
   so the agent can edit a surface it drew earlier instead of redrawing it, a structural validator
   (`validate_a2ui_components` — detects reference cycles in the component tree, unresolvable data
   binding paths, missing required props), and a **recovery loop** that feeds validation errors back
   to the model and retries. Nothing in Aleph does any of that today.
3. **The browser-facing protocol really is a small number of routes.** From
   `dist/v2/runtime/core/fetch-handler.d.mts` and the route table in the runtime skill, SSE mode
   serves: `GET /info`, `POST /agent/:id/run`, `GET /agent/:id/connect`,
   `POST /agent/:id/stop/:threadId`, `POST /transcribe`. Five. The thread routes only exist in
   Intelligence mode. FastAPI could serve these.
4. **`server.ts` genuinely is 75 lines** and contains no Aleph business logic. It is not a load-bearing
   application; it is configuration.

If the question were only "does the current file justify a second language," the answer would
plausibly be no. That is the question the prior pass asked.

---

## 3. Where it was wrong

### 3a. Deleting the service today would break generative UI immediately

`render_a2ui` **is not defined anywhere in Aleph's Python.** I grepped the whole tree. The only hits
are in `copilot_agent.py`'s *system prompt* — "render it as an interactive card via render_a2ui",
"Emit the card via your render_a2ui tool using the exact component name above" — and a comment in
`subagents/viz_builder.py`. The tool itself is injected at request time by the Node runtime's
`a2ui: { injectA2UITool: true }` config.

So the deletion is not "move one job from JS to Python." It is: write and wire `get_a2ui_tools`,
supply the catalog to it, verify the streaming behaviour, and *then* delete. Those are not the same
size of task, and the prior pass costed the second one as if it were the first.

### 3b. The A2UI middleware does six things; tool injection is one

I read the type definitions of `@ag-ui/a2ui-middleware@0.0.10` (bundled with the runtime). The class
`A2UIMiddleware` documents these responsibilities:

1. **`injectToolAndFlag`** — injects `render_a2ui` with the correct parameter schema, replacing any
   existing definition. *(This is the one the prior pass found in Python.)*
2. **`injectSchemaContext`** — injects Aleph's catalog into `RunAgentInput.context` under a specific
   marker description, so the agent knows which components exist. The Python toolkit's
   `resolve_a2ui_catalog(state)` reads exactly this — its docstring names
   `@ag-ui/a2ui-middleware` as the stamper.
3. **`injectToolGuidelines`** — injects protocol instructions and a worked example as context, "so it
   can produce valid A2UI without agent-specific prompting." Aleph currently hand-writes some of this
   into its own system prompt instead.
4. **`processStream` + streaming extractors** — this is the big one. The middleware watches the
   *partially streamed* tool arguments and progressively extracts complete components and data items
   from half-finished JSON (`extractCompleteItems`, `extractCompleteObject`,
   `extractDataArrayItems`, `extractStringField`). This is what makes a card paint in as it is
   generated rather than appearing all at once at the end.
5. **`buildLifecycleActivity`** — a documented generation lifecycle riding one stable message id:
   `status: "building" | "retrying" | "failed"`, then replaced in place by the painted surface. It
   carries a live token estimate for the skeleton, a retry counter ("Retrying… (N/M attempts)"), and a
   `debugExposure` knob. The source comment is explicit that this middleware is **"the single emitter
   of the generation lifecycle for all of them — Python and TypeScript alike."**
6. **`processUserAction`** — the return path. When a user clicks a button inside a drawn card, the
   click arrives as `forwardedProps.a2uiAction`, and the middleware turns it into a synthetic
   `log_a2ui_event` tool call + result appended to the conversation, so the agent sees "the user just
   clicked Approve" as a normal part of its history.

The Python `get_a2ui_tools` covers (1) and part of (3), plus validation and recovery that the JS side
does differently. It does **not** cover (4), (5), or (6) — and its own docstring confirms the split:
*"this adapter does NOT emit any A2UI-specific custom events."* The streaming lifecycle is the
middleware's job in both languages.

**Plain version:** Python can now generate the card. The Node layer is what makes the card appear
progressively, show a "building…" state, retry visibly when the model produces something invalid, and
carry button clicks back to the agent. Those are not cosmetics for a research tool whose cards are the
primary interaction surface.

### 3c. `/info` is a versioned internal contract, not a documented route

If FastAPI serves AG-UI directly, it must also serve `GET /info`. I read the actual implementation
(`dist/v2/runtime/handlers/get-runtime-info.mjs`). It returns:

```
version, agents{name,description,className,capabilities}, audioFileTranscriptionEnabled,
mode, threadEndpoints{list,inspect,mutations,realtimeMetadata}, suggestions,
intelligence{wsUrl}?, a2uiEnabled, a2ui{enabled,agents}?, openGenerativeUIEnabled,
licenseStatus?, telemetryDisabled
```

These are not decorative. `a2uiEnabled` is what makes the browser auto-mount the A2UI renderer — the
`a2ui-renderer` skill states that without it "the provider's a2ui prop silently no-ops." `capabilities`
feeds `useCapabilities()`, the hook the plugin vision would use to feature-gate UI per agent.
`threadEndpoints` gates `useThreads`. `version` feeds the client's mismatch check.

None of this is in any public protocol document. It is CopilotKit's internal handshake, and it grew
three new fields between 1.58 and 1.63. Reimplementing it in Python means owning a contract that
changes on someone else's release cadence, with no test that fails when it drifts — and Aleph's own
CLAUDE.md is emphatic about exactly this failure mode ("a column, table, or service that is written
correctly and read by nothing").

---

## 4. Is Aleph's integration idiomatic? No — and the wiring drift is worse than the version drift

Seven concrete deviations from the maintainers' documented path. Note that **five of them are fixable
without upgrading anything**, because the APIs already exist in the installed 1.58.

| # | Aleph does | Docs say | Severity per docs |
|---|---|---|---|
| 1 | `<CopilotKitProvider>` (`lib/copilot.tsx`) | Use `<CopilotKit>`. `CopilotKitProvider` is "a functionality subset"; the upgrade skill says **"Do not migrate to it"** | stated twice, in two skills |
| 2 | `renderActivityMessages={[createA2UIMessageRenderer(...)]}` | Use `a2ui={{ theme, catalog }}`. Manual wiring "duplicates the renderer and can race with the auto-injected one" | **HIGH** |
| 3 | `createCopilotNodeListener` from `/v2/node` | `createCopilotRuntimeHandler` is "the strongly-preferred primitive"; framework adapters are "avoid at all costs" | **CRITICAL** for Express/Hono; Node listener is the same class |
| 4 | `new HttpAgent({ url })` from `@ag-ui/client` | For a self-hosted LangGraph AG-UI server: `LangGraphHttpAgent` from `@copilotkit/runtime/langgraph` | named in the integration matrix |
| 5 | No `hooks` on the handler | `hooks.onRequest` is where auth lives; it is the only surface that forwards a thrown `Response` | this *is* Aleph's Known-broken "runtime bridge does not forward the caller's credential" |
| 6 | Default `InMemoryAgentRunner` | "Shipping InMemoryAgentRunner to production" — threads lost on restart, divergent across replicas | **HIGH** |
| 7 | Catalog stamped as `defaultCatalogId: "aleph://v1"` only | Fine, but the *frontend*-registered catalog path (`state["ag-ui"]["a2ui_schema"]`) is the one the plugin vision needs | — |

Two of these deserve expansion.

**#2 is the interesting one.** I checked the installed 1.58.0 type definitions:
`CopilotKitProviderProps` at line 2282 already declares `a2ui?:`, and at line 2239 already declares
`openGenerativeUI?:`. **Aleph is not blocked by its version from doing this correctly — it is using the
1.58-era hand-wired pattern when 1.58 itself already shipped the declarative one.** And it is running
`@copilotkit/a2ui-renderer` at 1.58.0 while the runtime side is at 1.63.2, which is precisely the skew
that makes a hand-wired renderer fragile. This is drift of *practice*, not of dependency.

**#6 undercuts something Aleph already built.** `copilotkit_endpoint.py` goes out of its way to pass a
durable Postgres checkpointer — "without it every restart drops the agent's history and plan." But the
CopilotKit layer in front of it runs the default in-memory runner, so the *runtime's* view of a thread
(active runs, replayable event stream, stop semantics) is lost on restart regardless. Aleph paid for
durability on one side of the bridge and not the other.

**On upgrading:** the moves are small but not zero. Switching `CopilotKitProvider` → `CopilotKit`
requires adding `useSingleEndpoint={false}`, because the compatibility bridge defaults it to `true`
and a multi-route backend will 404 — the setup skill flags this explicitly. `useFrontendTool`
parameters are already Zod in Aleph, so no schema rewrite. Nothing in the 1.58→1.68 range appears to
break Aleph's actual call sites; the risk is the a2ui renderer double-mounting during the transition
(fix by dropping the manual `renderActivityMessages` in the *same* change that adds `a2ui={{...}}`,
not before or after).

---

## 5. The decisive comparison

### Keep and upgrade `apps/copilot-runtime` (make it thin)

| Gain | Concrete |
|---|---|
| Progressive card painting + building/retrying/failed lifecycle | `A2UIMiddleware.processStream` + `buildLifecycleActivity`. No Python equivalent exists; the Python adapter's own docstring says it emits no A2UI events. |
| Card button clicks reach the agent as conversation history | `processUserAction` → synthetic `log_a2ui_event` call + result. |
| Frontend-registered catalogs reach the agent automatically | `injectSchemaContext` stamps the catalog into request context; the Python toolkit reads it from there. **This is the mechanism the plugin vision needs** — plugin installs, its catalog registers, the agent learns about it without a redeploy. |
| Auth at the front door, one place | `hooks.onRequest` / `onBeforeHandler` with typed `route.method` / `route.agentId`. Fixes Known-broken #4 (credential not forwarded). |
| Durable, resumable threads without a managed service | `AgentRunner` is a 4-method abstract class. A `PostgresAgentRunner` for Aleph is a genuinely small, well-specified piece of work (the skill ships a Redis skeleton). Fixes the in-memory gap. |
| **MCP Apps** — sandboxed third-party UI | `@ag-ui/mcp-apps-middleware` is already installed as a runtime dependency. Discovers UI-enabled tools from MCP servers, injects them, emits activity snapshots with resource URIs. **Runtime-only. No Python equivalent exists.** For a system whose thesis is "the agent writes plugins for itself," a sandboxed third-party UI channel is not a side feature. |
| **Open-ended generative UI** | `OpenGenerativeUIConfig` is exported by the installed runtime 1.63.2; `openGenerativeUI?:` is on the installed 1.58 provider props. Agent emits complete HTML/SVG into a websandbox iframe with declared `sandboxFunctions`. Aleph already has a `sandbox` iframe policy for code-runner artifacts, so the security posture already fits. **Unused today, available today.** |
| Voice transcription | `POST /transcribe` + `TranscriptionService`. Advertised via `/info`, gated client-side by `useCapabilities()`. |
| Slack / Teams | `@copilotkit/channels-*` 0.2.1 is already installed. Requires Intelligence mode (see below). |
| Someone else maintains the handshake | `/info`, event translation, tool-call arg accumulation, reconnect. Currently free. |

| Cost | Concrete |
|---|---|
| A second language at the front door — permanently | Node 22 container, 512 MB, one more compose service, one more restart surface. |
| A second dependency universe | npm + `package-lock.json` outside the pnpm workspace; the repo's security `overrides` do not reach it; a separate CI install step. |
| Version-skew maintenance | Already live: web at 1.58 / AG-UI 0.0.53, runtime at 1.63.2 / AG-UI 0.0.57. Needs a policy: one version, bumped together. |
| An extra hop for auth and observability | Every request crosses a boundary where Aleph's `Principal`, ledger, and OTEL context are not automatic. |
| It is the only JS in an otherwise Python backend | Pyright-strict does not cover it; ruff does not cover it; the acceptance suite does not run it. |

### Delete it and serve AG-UI from FastAPI

| Gain | Concrete |
|---|---|
| One language, one dependency tree, one lockfile, one container fewer | Real, and the pnpm-`overrides` gap closes with it. |
| Auth is native | `Principal`, agent tokens, project scope, ledger, OTEL all already exist in FastAPI. No header forwarding to invent. |
| Thread state can be Postgres from the start | Aleph already has the checkpointer and the store. |
| Fewer hops | Browser → FastAPI directly; the `/copilotkit` route already exists. |

| Loss | Concrete |
|---|---|
| **Generative UI stops working on day one** unless `get_a2ui_tools` lands in the same change | `render_a2ui` is prompt-referenced and defined nowhere in Python. |
| Progressive painting, building/retrying/failed lifecycle, live token estimate | Would have to be built in Python against the AG-UI activity-snapshot wire format. Not documented as a public contract. |
| Card-click → agent round trip | Same. `processUserAction` has no Python counterpart. |
| Frontend-registered catalog delivery | Aleph would pass the catalog server-side as a static `catalog=` param. Fine for one hand-maintained catalog; **directly at odds with per-plugin catalogs registered at runtime.** |
| MCP Apps | No Python path at all. |
| Open-ended generative UI (`openGenerativeUI`) | Runtime-side feature; the client half needs `/info` to advertise `openGenerativeUIEnabled`. |
| Transcription, Channels, Intelligence threads | All runtime-side. |
| Aleph owns `/info` forever | ~12 fields of undocumented internal handshake, versioned on CopilotKit's cadence, with no test that fails when it drifts. |

### Does keeping it mean permanently running a second language for the front door?

Honestly: yes, and that is a real architectural commitment worth being deliberate about. But two
things reduce its weight.

First, **the front door is already Python.** FastAPI already serves four SSE streams of its own
(`agent_events`, `changes`, `surfaces`, `assets`). The Node service is not "the front door" — it is a
sidecar on one path. Deleting it does not consolidate the front door; it consolidates *one lane* of it.

Second, **the amount of JS is a choice.** Written idiomatically —
`createCopilotRuntimeHandler` + hooks + a runner — the file stays under 120 lines and contains zero
Aleph domain logic. That is a configuration file that happens to be TypeScript. Compare that to
reimplementing the A2UI streaming lifecycle in Python: that is *more* code, in the language you
prefer, that you then own forever against an upstream that keeps moving.

---

## 6. The middle path — and it is the right answer

Three options, in ascending cost.

### Option A — Fix the wiring. No upgrade, no deletion. ~1 day.

Everything here uses APIs already present in the installed versions.

1. `lib/copilot.tsx`: `CopilotKitProvider` → `CopilotKit`, add `useSingleEndpoint={false}`, replace
   `renderActivityMessages={[...]}` with `a2ui={{ theme: a2uiDefaultTheme, catalog: buildAlephCatalog() }}`
   **in the same commit** (dropping one and adding the other separately gives you either no renderer or
   two).
2. `server.ts`: `createCopilotNodeListener` → `createCopilotRuntimeHandler`, and swap `HttpAgent` for
   `LangGraphHttpAgent`.
3. Add `hooks.onRequest` that reads the browser's `Authorization` header and forwards it to the
   FastAPI agent URL. **This closes Known-broken item 4** ("the runtime bridge does not forward the
   caller's credential"), which currently makes `oidc` mode unusable.
4. Pin `@copilotkit/*` to one version across web and runtime. Add a CI check that the two trees agree —
   the same shape as `check-catalog-generated.sh`, and for the same reason: three copies that disagree
   is how the `ClaimCard.confidence` bug survived.
5. Move `apps/copilot-runtime` into the pnpm workspace so the security `overrides` reach it.

### Option B — Add the pieces that are already paid for. ~3–5 days on top of A.

6. Write `PostgresAgentRunner` (subclass `AgentRunner`: `run`, `connect`, `isRunning`, `stop`). Gets
   durable, resumable, restart-surviving threads with **no managed dependency**, and matches the
   durability Aleph already bought on the LangGraph side. The runtime skill ships a Redis skeleton to
   translate.
7. Upgrade both trees to 1.68.2 together. Nothing in Aleph's call sites appears to break.
8. Turn on `openGenerativeUI` behind a flag, reusing the existing code-runner sandbox posture.
9. Evaluate `mcpApps` against the plugin vision — this is the sandboxed third-party-UI channel and it
   has no Python alternative.

### Option C — Delete. Not now; revisit in ~6 months.

The precondition is not "does Python have a tool" (it does). It is: **does Python have a wire-level
A2UI lifecycle emitter?** Today, no — the middleware's own source says it is the single emitter for
Python and TypeScript alike. Watch `ag-ui-a2ui-toolkit` (0.0.4 today, moving fast — it exists
specifically so new framework adapters share the logic). When the streaming lifecycle moves into the
toolkit, revisit. Aleph's docs already have a pattern for this: state the condition that unblocks the
deletion, the way `docs/acceptance.md` §E does for the wiki.

### What the Python SDK can and cannot do, precisely

`copilotkit` 0.1.95 (PyPI) is **not** an equivalent of the JS runtime, and it is important not to read
the version numbers as comparable. It is an agent-side SDK: `CopilotKitState`, `LangGraphAGUIAgent`,
`Action`/`Parameter`, `a2ui` operation builders and `a2ui_prompt`, `set_forwarded_headers` /
`install_httpx_hook` for header propagation, plus CrewAI glue. Useful — the header-propagation helpers
are directly relevant to Aleph's credential problem. But it has **no** `CopilotRuntime`, no `/info`
handler, no `AgentRunner`, no middleware chain, no MCP Apps, no transcription, no Channels. There is no
"adopt the runtime's capabilities via the Python SDK" path, because the Python package is a different
layer of the stack, not a port of the same one.

---

## 7. Failure modes Aleph will hit, from `copilotkit-debug`

Matched against Aleph's actual configuration. The first three are live today.

| Failure | Aleph's exposure |
|---|---|
| **`VERSION_MISMATCH`** | **Live.** react-core 1.58.0 vs runtime 1.63.2; AG-UI 0.0.53 vs 0.0.57. Dev-only INFO severity, so it will not fail CI — it will just be weird. |
| **Reasoning events stall the agent** (issue #3323) | **Likely live.** Aleph routes through a LiteLLM gateway to whatever model the project profile binds; Anthropic reasoning tokens through an older AG-UI translation is the exact reported shape. The 0.0.53 → 0.0.58 gap is where those fixes landed. |
| **Event-name prefix mismatch, Python SDK + `ag-ui-langgraph`** (issue #3519) | **Possible.** Reported against `copilotkit_emit_*` helpers with `ag-ui-langgraph`. Aleph is on 0.0.36 vs 0.0.43. Resolution given: update `ag-ui-langgraph`. |
| **`AGENT_NOT_FOUND`** | Structural. Aleph names the agent `assistant` in three places — `server.ts`'s agents map, `LangGraphAGUIAgent(name="assistant")`, and the client. Nothing checks they agree. |
| **A2UI "Catalog not found"** | Already hit once and fixed by hand — `server.ts` carries a long comment explaining that without `defaultCatalogId` the middleware stamps an upstream id the frontend never registers. The idiomatic `a2ui={{ catalog }}` wiring removes the class of bug. |
| **`Cannot read properties of null (reading 'subscribe')` in A2A/A2UI** (issue #3429) | Initialization-order bug in the same subsystem Aleph leans on hardest. |
| **`a2uiAction` leaking between runs** | Only if Aleph hand-rolls the action bridge. The built-in bridge strips it in a `finally`. An argument for using the built-in path rather than a custom one. |
| **Thread already running / double-submit** | In SSE mode this surfaces as a bare `500` with `"Thread already running"` — no typed error code. Needs a client-side busy guard, or `InMemoryAgentRunner({ onConcurrentRun: "supersede" })`. |
| **CORS + credentials** | `cors: true` (Aleph's current setting) means allow-all *without* credentials. The moment auth headers or cookies cross from `:5173` to `:4000`, this needs explicit `origin` + `credentials: true` on both ends. This is a hard prerequisite for Option A step 3. |

The debug skill also names two tools Aleph is not using and should: `@copilotkit/web-inspector` for
live AG-UI event tracing, and the `copilotkit-docs` MCP server (already configured in this repo).

---

## 8. One strategic caveat, stated plainly

`CopilotKitIntelligence` — durable threads, the dashboard, Slack/Teams Channels, automatic learning —
is a **managed cloud service and is not self-hostable.** The runtime skill states it directly:
"Intelligence mode targets the managed CopilotKit Intelligence service and is not self-hostable," and
"if you need on-prem durable threads today, use SSE mode with a persistent runner." `organizationId` is
reserved for a future self-hosted deployment that does not exist yet.

That matters because Aleph's own memory records "Aleph serves no models — connects out to
OpenAI-compatible endpoints only; deploys via docker compose." Adopting Intelligence means adopting a
hosted dependency with an outbound websocket, which is a different posture from everything else in the
stack. **Everything I recommend in Options A and B is SSE mode — self-hosted, no CopilotKit account, no
API key.** Channels and durable managed threads are genuinely attractive and genuinely a different
decision; they should be evaluated on their own, not smuggled in as part of a runtime upgrade.

---

## 9. Bottom line for the deletion question

**Deleting `apps/copilot-runtime` today loses something real, and the loss is larger than the prior
pass measured.** It loses the tool the agent's own prompt depends on, the progressive-painting and
retry lifecycle for every card, the click-back path from cards to agent, the mechanism by which a
frontend-registered catalog reaches the agent, the MCP Apps channel, the open-ended generative UI
band, and it hands Aleph permanent ownership of an undocumented `/info` handshake.

But the prior pass was right that the service **as currently written** is not carrying its weight: it
is 75 lines using a discouraged handler, a generic agent class, no auth hook, an in-memory runner, its
own package manager, and a version that disagrees with the browser it serves.

The correct verdict is not keep-or-delete. It is: **the service is under-used, not unnecessary.** Fix
the wiring (Option A, ~1 day, closes two Known-broken items and needs no upgrade), then decide whether
the capabilities in Option B are worth the second language. That decision will be far better informed
than either the original recommendation or a reflexive keep.
