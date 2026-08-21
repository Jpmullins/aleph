# opencode — findings for the Aleph kernel

**Reviewed:** `/Users/jpmullins/Documents/code/inspiration/opencode` (~535k lines, 34 workspace packages, Bun + TypeScript).
**License:** MIT throughout — root `LICENSE` ("Copyright (c) 2025 opencode") and `"license": "MIT"` on every one of the 24 published `packages/*/package.json`. Safe to *reimplement*; per Aleph's standing constraint, do not vendor. Nothing here is copyleft or otherwise encumbered. Two caveats on copying verbatim: (1) `packages/codemode/src/interpreter/runtime.ts` is a 3,465-line hand-written JavaScript interpreter — reimplementing that from scratch would be a project, and a *bad* reimplementation is a security hole, so if Aleph wants confined agent code it should either port carefully with a `NOTICE` or use a real isolate; (2) the repo carries 17 `patchedDependencies` (`package.json:patchedDependencies`) against `@ai-sdk/*`, `effect`, and `@modelcontextprotocol/sdk` — meaning several behaviours depend on upstream forks, which will not reproduce if you copy the calling code alone.

**Note on "which version":** opencode is mid-rewrite. There is a **v1** plugin API (`packages/plugin/src/index.ts`, a fixed `Hooks` object) and a **v2** API (`packages/plugin/src/v2/`, imperative registration into stateful domains). *The v2 design is the interesting one and it is real, running code* — the whole boot path already runs on it. Where they differ I say so, because the v1 shape is exactly the anti-pattern you asked me to watch for.

---

## In one paragraph

opencode is a production coding agent whose core insight — the one worth taking — is that **a plugin does not "do things to" the system; it contributes a *rule* to a recipe, and the system re-cooks the whole dish whenever the recipe changes.** Every piece of live configuration (the list of agents, the catalog of models, the set of skills, the available commands) is a *domain*: a value rebuilt from scratch by replaying an ordered list of registered "transform" functions over a fresh blank state. Adding a plugin appends transforms and rebuilds; *removing* a plugin deletes its transforms and rebuilds — so there is **no undo code anywhere**, because nothing was ever mutated in place. Readers of that state pay nothing at all: they read a plain in-memory `Map`, and no plugin runs on the read path. Plugins that genuinely must intercept live operations (choosing which SDK object talks to a model, rewriting tool arguments) go through a separate, deliberately tiny set of "runtime hooks" whose results are memoised — so even those run once per model, not once per token. That split — *contribute to state cheaply and often; intercept operations rarely and cache the result* — is the whole answer to "how do I have plugins without getting slow". Everything else here (session persistence as an event log, a system prompt frozen into an immutable "epoch" so provider caching keeps working, a confined JavaScript interpreter that lets the model batch fifty tool calls into one program) is excellent operational engineering built on top.

**Jargon defined once:**
- **Effect** — the TypeScript library this codebase is written in. An `Effect<A, E, R>` is a *description* of a computation producing `A`, possibly failing with `E`, requiring services `R`. Nothing runs until the effect is executed. Its two features that matter here: **Scope** (a bag of finalizers; closing the scope runs them in reverse order) and **Layer** (a recipe for constructing a service from its dependencies, memoised so a shared dependency is built once).
- **Domain / transform / rebuild** — a domain is a piece of derived shared state; a transform is a registered function that edits a draft of it; a rebuild replays every active transform from a fresh initial value.
- **Location** — opencode's word for "one workspace/project directory". Most services are *Location-scoped*: one instance per open directory.
- **Draft** — a mutable editor handed to a transform; the transform mutates the draft, not the live state.

---

## 1. The extension model — what IS a plugin here

**A plugin is `{ id: string, effect: (ctx) => Effect<void, never, Scope> }`.** That is the entire contract:

```ts
// packages/plugin/src/v2/effect/plugin.ts:4-11
export interface Plugin<R = Scope.Scope> {
  readonly id: string
  readonly effect: (context: PluginContext) => Effect.Effect<void, R>
}
export function define<R = Scope.Scope>(plugin: Plugin<R>) { return plugin }
```

The setup function **returns nothing**. It registers imperatively, and every registration is attached to the plugin's `Scope`. `packages/plugin/src/v2/effect/PLAN.md:46` states it flatly: *"Plugin setup does not return hooks."* That single decision is what makes removal free (see §4).

The smallest complete real plugin in the tree — a built-in that contributes two slash commands:

```ts
// packages/core/src/plugin/command.ts:9-25
export const Plugin = define({
  id: "command",
  effect: Effect.fn(function* (ctx) {
    const location = yield* Location.Service
    yield* ctx.command.transform((draft) => {
      draft.update("init", (command) => {
        command.template = PROMPT_INITIALIZE.replace("${path}", location.project.directory)
        command.description = "guided AGENTS.md setup"
      })
      draft.update("review", (command) => {
        command.template = PROMPT_REVIEW.replace("${path}", location.project.directory)
        command.description = "review changes [commit|branch|pr], defaults to uncommitted"
        command.subtask = true
      })
    })
  }),
})
```

Twenty-five lines, no manifest, no registry file, no undo. `packages/core/src/plugin/variant.ts` is a similarly complete one for the model catalog; `packages/core/src/plugin/skill.ts:13-30` contributes one embedded skill.

There is a **Promise-flavoured** identical API for people who do not want Effect (`packages/plugin/src/v2/promise/README.md`), implemented as a pure adapter — `packages/core/src/plugin/promise.ts:20-92` wraps each domain's `transform` so an `async` callback becomes an Effect, running it on the *captured fiber context* so boot batching still applies (line 17-19 comment). Worth copying the idea: **one canonical runtime, one thin ergonomic wrapper**, not two implementations.

### Declaration, registration, discovery, start

- **Declared** in config: `plugins: ["@scope/name", "./plugins/local.ts", { package, options }]`, or *discovered* by globbing `{plugin,plugins}/*.{ts,js}` under any config directory — `packages/core/src/config/plugin/external.ts:42-71`.
- **Resolved**: an npm spec goes through `Npm.add()` (installs into a shared cache dir under a file lock — `packages/core/src/npm.ts`); a path becomes a `file://` URL.
- **Started**: plain dynamic `import(entrypoint)`, schema-validated to have `{id, effect}` or `{id, setup}`, then handed to `ctx.plugin.add(...)` — `external.ts:80-86`.
- **Version-gated**: `engines.opencode` in the plugin's `package.json` is checked with semver before load — `packages/opencode/src/plugin/shared.ts:193-204`, throwing `Plugin requires opencode ${range} but running ${version}`.

**The loader that loads external plugins is itself a plugin** (`id: "config-plugin"`). So is the models.dev data source, so is every provider integration, so is config projection. `packages/core/src/plugin/internal.ts:108-123` is the entire boot: twelve `add()` calls inside one `State.batch`.

---

## 2. Dependencies between plugins

**There is no plugin dependency graph, deliberately.** `PLAN.md:405` — *"The runtime does not infer cross-domain dependencies."* Instead there are three mechanisms, and they are better than a dependency graph:

**(a) Ordered contribution to shared domains.** Plugins do not depend on each other; they depend on *domains*, and order is deterministic: plugin registration order, then transform-registration order within a plugin (`PLAN.md:97`). A later plugin sees and can overwrite what an earlier one wrote, because they edit the same draft in sequence. The house ordering is documented as policy (`PLAN.md:255-274`): built-ins → base data sources (models.dev) → config projections → provider normalisation → **user plugins** → core finalisation. So a user plugin always gets the last word before invariants are enforced.

**(b) Cross-domain reads + explicit re-subscription.** A transform may read another domain's *committed* state and must arrange its own rebuild when that changes:

```ts
// PLAN.md:376-403 — AnthropicAgentPlugin
yield* ctx.agent.transform(Effect.fn(function* (agent) {
  const providers = yield* ctx.catalog.provider.list()      // read another domain
  if (!providers.some((p) => p.id === "anthropic")) return
  agent.update("anthropic-reviewer", (item) => { /* ... */ })
}))
yield* ctx.event.subscribe("catalog.updated").pipe(         // and re-run when it changes
  Stream.runForEach(() => ctx.agent.rebuild()), Effect.forkScoped)
```

This is a real, working `A-depends-on-B` without any manifest. The cost is that the dependency is *implicit* and the author must remember the subscription; the benefit is no resolution phase, no version solving, no cycles.

**(c) Plugins can add plugins.** `ctx.plugin.add(...)` / `.remove(id)` are part of the plugin context (`packages/plugin/src/v2/effect/plugin.ts:13-16`, implemented `packages/core/src/plugin/host.ts:193-196`). That is the composability escape hatch: a suite plugin can install its members. Load cycles are caught at runtime — `packages/core/src/plugin.ts:44` dies with `Plugin load cycle detected for ${id}`.

### Is this the VSCode anti-pattern?

**Split verdict, and the split is instructive.**

- **The server-side v2 API is NOT the anti-pattern.** Six domains (`agent`, `catalog`, `command`, `integration`, `reference`, `skill`) each accept unbounded, ordered, composing contributions. Any plugin can rewrite any other plugin's agent, model, or command, because all of them are just edits to one shared draft. That is genuinely open.
- **The v1 API IS the anti-pattern**, textbook: `packages/plugin/src/index.ts:222-335` is a closed `Hooks` interface with ~17 named callbacks (`"chat.params"`, `"tool.execute.before"`, `"permission.ask"`, …), each with a fixed `(input, output)` shape where you mutate `output`. Nothing composes; adding an extension point requires editing the host's type. Several are prefixed `experimental.` — the tell that the surface is being extended one hole at a time.
- **The TUI plugin API is a mild anti-pattern with an escape hatch.** Slots are a fixed named map — `home_prompt`, `sidebar_content`, `session_prompt_right`, etc., `packages/plugin/src/tui.ts:455-486`. *But* the slot map is generic: `type TuiSlotMap<Slots> = TuiHostSlotMap & Slots` (`tui.ts:488`), and `api.ui.Slot` is exposed to plugins, so plugin A can render `<Slot name="my_thing" />` and plugin B can fill it. Untyped across the boundary, but structurally open.

**Aleph lesson:** the reason v2 escapes the trap is that extension points are not *callbacks on operations* but *contributions to values*. There is no way to enumerate "the 7 things you can hook" because the thing you hook is a data structure.

---

## 3. How plugins communicate — the performance question

This is the section that answers the owner's worry, and the answer is good.

### The mechanism is direct in-process function calls on a plain array. Nothing is serialised.

```ts
// packages/core/src/state.ts:61-85 — the whole engine
export function create<State, DraftApi>(options: Options<State, DraftApi>) {
  let state = options.initial()
  let transforms: { run: TransformCallback<DraftApi> }[] = []
  const semaphore = Semaphore.makeUnsafe(1)

  const materialize = Effect.fnUntraced(function* () {
    const next = options.initial()                    // fresh blank state
    const api = options.draft(next)                   // wrap in a draft editor
    for (const transform of transforms)               // replay every active transform
      yield* apply(transform.run, api).pipe(Effect.withSpan("State.reload.update"))
    yield* commit(next)                               // finalize + swap in
  })
  const reload = () => semaphore.withPermit(materialize())
  return { get: () => state, transform: /* ... */, reload }
}
```

`transforms` is a JS array of closures. Calling one is a direct call. There is no message bus, no IPC, no RPC, no structured clone, no JSON. Plugins live in the **same process, same heap, same event loop** as the host, loaded by `import()`.

### Plugin references are resolved once and cached — usually never touched again

`get: () => state` (line 88) is a **synchronous property read of a materialised value**. Every consumer of a domain — the tool registry, the model resolver, the session runner — reads a plain `Map`:

```ts
// packages/core/src/catalog.ts:192-197
model: {
  get: Effect.fn("CatalogV2.model.get")(function* (providerID, modelID) {
    const record = state.get().providers.get(providerID)   // <- Map lookup. No plugin runs.
    ...
```

**This is the key structural fact.** Plugin code runs at rebuild time only. On the read path the plugin tax is *literally zero* — indistinguishable from a hard-coded constant, because by then it *is* one.

### Data across the boundary: references and drafts, not payloads

Transforms receive a **draft editor** — a small facade over the live mutable structure (`packages/core/src/catalog.ts:107-158`; `packages/core/src/plugin/host.ts:33-42`). No copying. The host wraps values in `mutable(...)` (`host.ts:18`) which is a *type-level* cast, not a runtime clone. Big payloads (documents, model catalogs) live in the domain's `Map` and plugins hold references into it.

The one place data *is* copied is the confined interpreter (§6): `copyIn`/`copyOut` deep-copy every value crossing the sandbox boundary (`packages/codemode/src/tool-runtime.ts:171, 297-315`). That is the price of confinement and the authors paid it only there.

### Where a hot loop would pay the tax — and how it is avoided

The one genuine per-operation plugin path is `AISDK`: plugins decide which SDK object and which language model back a given model id. That is on the path to *every LLM call*. The authors memoise it:

```ts
// packages/core/src/aisdk.ts:198-227
language: Effect.fn("AISDK.language")(function* (model) {
  const key = `${model.providerID}/${model.id}/${model.request.variant ?? "default"}`
  const existing = languages.get(key)
  if (existing) return existing                        // <- hot path exits here
  const options = prepareOptions(model, model.api.package)
  const sdkKey = JSON.stringify({ providerID: model.providerID, api: model.api, options })
  const sdk = sdks.get(sdkKey) ?? (yield* service.runSDK({...})).sdk   // hooks run on miss only
  sdks.set(sdkKey, sdk)
  const result = yield* service.runLanguage({ model, sdk, options })   // hooks run on miss only
  languages.set(key, language)
  return language
})
```

The plugin hook chain runs **once per (provider, model, variant)** for the life of the Location. The single `JSON.stringify` in the whole plugin path is the cache key, computed on a miss. Hook dispatch itself is a bare `for` loop over closures with an `Effect.isEffect` check so synchronous hooks skip the async machinery entirely (`aisdk.ts:174-182`).

The **tool** path does the same at a different granularity: `ToolRegistry.materialize(permissions)` (`packages/core/src/tool/registry.ts:106-122`) snapshots the effective tool table *once per provider turn* and returns a closure over it; every tool call inside that turn does a `Map.get` on the snapshot, not a registry walk.

### Rebuild-cost controls

- **Serialised and coalesced.** One semaphore per domain (`state.ts:64, 85`). Concurrent rebuilds queue; the plan specifies at most one extra scheduled rebuild during an active one (`PLAN.md:104`).
- **Batching.** `State.batch` collects every domain that wants a rebuild and rebuilds each **once** at the end (`state.ts:33-42`). Boot wraps all twelve internal plugins in one batch (`plugin/internal.ts:108-123`), and so does every plugin add/remove (`plugin.ts:52, 89`). So installing a plugin that touches four domains costs four rebuilds, not sixteen.
- **Lazy per-workspace construction with idle eviction.** The Location service graph is built on first use and torn down after inactivity: `LayerMap.make((ref) => ..., { idleTimeToLive: "60 minutes" })` — `packages/core/src/location-services.ts:89-110`.

### Measured benchmarks and perf-motivated comments

There is **no runtime benchmark harness** for the plugin system. There is one for the *test suite*: `perf/test-suite.md` is a disciplined hypothesis table with a primary metric (`METRIC test_suite_seconds`), before/after medians, and a keep/revert decision per change — ~25 rows, e.g. `7.905s → 2.980s`. Worth stealing as a *method* even though the subject is tests.

Genuine perf-motivated code comments exist and they are honest:

```ts
// packages/llm/src/cache-policy.ts:76-83
// Single pass over `messages`, substituting the one updated entry. Long
// conversations call this on every request, so avoid `.map()` here — its
// closure dispatch and identity copies show up in profiling.
const result = messages.slice()
result[index] = next
```

and the cost model for prompt caching is spelled out in the file header (`cache-policy.ts:1-37`): default `"auto"` places cache breakpoints at the last tool definition, last system part, and latest user message, justified by "Anthropic 5m-cache write is 1.25x base, read is 0.1x, so a single reuse within 5 minutes already wins."

**Bottom line for Aleph:** opencode's plugin system is as fast as a compiled monolith on the read path *because on the read path it is one*. The composition happens at rebuild time and produces an ordinary data structure.

---

## 4. Lifecycle — load, activate, deactivate, reload

### Server side: scope-owned, no restart, no undo code

```ts
// packages/core/src/plugin.ts:43-83 (add), abridged
const add = Effect.fn("Plugin.add")(function* (id, effect) {
  if (loading.has(id)) return yield* Effect.die(`Plugin load cycle detected for ${id}`)
  yield* locks.withLock(id)(                              // per-id mutex
    ... State.batch(Effect.gen(function* () {
      const existing = active.get(id)
      active.delete(id)
      if (existing) yield* Scope.close(existing, Exit.void).pipe(Effect.ignore)   // replace = close old first
      const child = yield* Scope.fork(scope)
      yield* effect(host).pipe(
        Scope.provide(child),
        Effect.withSpan("Plugin.load", { attributes: { "plugin.id": id } }),
        Effect.onExit((exit) => (Exit.isFailure(exit) ? Scope.close(child, exit) : Effect.void)),  // failed setup unwinds
      )
      yield* events.publish(Event.Added, { id })
      active.set(id, child)
    })), ...)
})
```

`remove` is nine lines: take the scope, close it (`plugin.ts:85-98`). Closing the scope runs the finalizer each `transform()` registered (`state.ts:117`), which splices the transform out of the array and triggers a rebuild. **The plugin's effect on the world disappears because the world is recomputed without it.** No plugin author ever writes an inverse.

Hot replacement: `add` with an existing id closes the old scope *before* running the new setup, in the same batch, so the swap is one rebuild. `PLAN.md:278` specifies (not yet obviously implemented) that a same-id replacement should retain its ordering position.

**In-flight work during a swap:** three distinct mechanisms.
1. A rebuild captures its transform list at the start; registration changes during a replay affect the *next* rebuild (`PLAN.md:105`, and structurally: `materialize` iterates `transforms`, and registration does `transforms = [...transforms, t]`, a copy-on-write that leaves the in-flight iteration untouched — `state.ts:114, 100`).
2. Runtime-hook arrays are likewise replaced wholesale, so an in-flight `run()` finishes on its captured array (`aisdk.ts:164-171`).
3. **Tool calls carry an identity token**, so a turn that advertised the old tool cannot silently execute the new one — see §5.

### TUI side: explicit activate/deactivate with a teardown deadline

The TUI has a second, independent plugin runtime (`packages/opencode/src/plugin/tui/runtime.ts`, 1,131 lines) with the API Aleph actually wants: `plugins.list() / activate(id) / deactivate(id) / add(spec) / install(spec)` (`packages/plugin/src/tui.ts:617-623`), surfaced in a real UI (`packages/tui/src/feature-plugins/system/plugins.tsx`), with enabled-state persisted to a KV store (`runtime.ts:481-488`).

Teardown is a LIFO unwind **with a budget**:

```ts
// packages/opencode/src/plugin/tui/runtime.ts:426-462
const dispose = async () => {
  if (done) return
  done = true
  ctrl.abort()                                  // AbortSignal for in-flight plugin work
  const queue = [...list].reverse()             // LIFO
  list = []
  const until = Date.now() + disposeTimeoutMs   // total budget, default 5000ms
  for (const item of queue) {
    const left = until - Date.now()
    if (left <= 0) { fail("timed out cleaning up tui plugin", {...}); break }
    const out = await runCleanup(item.fn, left)
    if (out.type === "ok") continue
    if (out.type === "timeout") { fail(...); break }
    if (out.type === "error") { fail("failed to clean up tui plugin", {...}) }   // continue anyway
  }
}
```

A failing finalizer does not abort the unwind; a hanging one cannot wedge the host. Activation that throws immediately disposes whatever the plugin managed to register (`runtime.ts:534-546`), and a plugin disabled *while* it was initialising is disposed on completion (`runtime.ts:548-552`).

---

## 5. Failure and blast radius

**Load-time failure is contained and silent-ish.** External plugin load is wrapped in `Effect.ignoreCause` (`packages/core/src/config/plugin/external.ts:87`) — a broken plugin is skipped and boot proceeds. Setup failure closes the child scope, removing any partial registrations (`plugin.ts:62`), records the exit in a `failures` map, and fails anyone `wait()`ing on that id (`plugin.ts:73-79`).

**Runtime failure is largely NOT contained.** Transforms have no typed error channel: `PLAN.md:91` — *"Transforms have no typed error channel. Unexpected failures are defects."* — and `state.ts:75` does `Effect.orDie` on a transform's result. A plugin whose transform throws takes the rebuild down as a defect. Since domain state is rebuilt as a unit, one bad transform poisons the whole domain. There is no per-plugin quarantine, no retry, no circuit breaker, and no probation.

**Isolation boundary: none.** Plugins are `import()`ed into the host process with full ambient authority: `fs`, `net`, `child_process`, `process.env`. There is one Proxy-based *capability scoping* mechanism, but it scopes *lifetime*, not *authority* — see §7.

**TUI side is better.** Per-plugin scopes, per-plugin error logging with the plugin id (`runtime.ts:37-46` slot errors; `runtime.ts:139-141` `fail()`), timeouts on cleanup, and a UI listing each plugin's `enabled`/`active` state (`runtime.ts:490-500`).

**Can the system refuse to remove something load-bearing?** No, and this is a real gap relative to Aleph's kernel. There is no protected-core set. What exists is adjacent and worth noting:
- The **service** graph refuses at *compile time*: `LayerNode` type-errors with `{ "Missing dependencies": ... }` if a node's layer needs a service no listed dependency provides (`packages/core/src/effect/layer-node.ts:10-14`), throws on cycles with the full path (`layer-node.ts:189-193`), and refuses a hot replacement that changes identity or introduces new error types (`layer-node.ts:117-127, 144-149`). But that governs *services*, not plugins.
- Plugin *load* cycles are caught at runtime (`plugin.ts:44`), and removing a plugin mid-load dies (`plugin.ts:86`).

So: opencode can prove its service graph is well-formed, and can prove a replacement is type-compatible, but cannot stop you removing the plugin that supplies your only model provider.

---

## 6. Trust and agent-authored code

**A plugin is arbitrary trusted code — full stop.** Dynamic `import()`, no AST gate, no signature, no permission manifest. The only gate is a semver compatibility check (`packages/opencode/src/plugin/shared.ts:193-204`). An agent can write a file into `.opencode/plugin/` and it will be picked up by the glob on the next config load. That is a supply-chain surface and the codebase does not pretend otherwise.

**But there is a serious sandbox elsewhere, and it is the best idea in the repo: CodeMode.**

`packages/codemode` is *"Effect-native confined code execution over explicit, schema-described tools"* (README:3). The model writes a small JavaScript program; the program can call **only** the tools the host placed in its `tools` tree, and has no filesystem, process, network, module, or global authority. The isolation boundary is a **hand-written AST interpreter**, not `eval`, not `vm`, not a worker: `acorn` parses, TypeScript transpiles, and `packages/codemode/src/interpreter/runtime.ts` walks the AST with an explicit scope chain. Globals are a small allowlist of *reference objects* rather than real functions (`runtime.ts:635-651`: `Number`, `String`, `Boolean`, `parseInt`, `encodeURIComponent`, …). Unsupported syntax is a typed diagnostic (`runtime.ts:522`). Every value crossing the boundary is deep-copied and JSON-normalised (`tool-runtime.ts:171, 297`). Budgets are explicit and per-execution:

```ts
// packages/codemode/src/codemode.ts:9-17
export type ExecutionLimits = {
  readonly timeoutMs?: number       // wall clock
  readonly maxToolCalls?: number    // admitted calls
  readonly maxOutputBytes?: number  // model-facing output
}
```

Failures are **data, not exceptions**: `Result = Success | Failure` with a stable `DiagnosticKind` union (`codemode.ts:64-75`) — `UnknownTool`, `InvalidToolInput`, `ToolCallLimitExceeded`, `TimeoutExceeded`, … — so the model gets a structured, actionable error and the host never sees a throw. `toolCalls` is retained on failure so a partial execution is auditable.

And the payoff is a *performance* one: `packages/opencode/src/tool/code-mode.ts` exposes **one** tool named `execute` that fronts every connected MCP server's tools. Instead of N model round-trips for N tool calls, the model writes one program that loops, branches, and runs calls in parallel. This is simultaneously the answer to context bloat (one tool definition instead of hundreds) and to latency (one round trip instead of N).

Progressive disclosure of the catalog is careful: a token budget (default 2,000, chars/4) with **round-robin fairness across namespaces** so every namespace gets representation before any gets everything, the instructions state honestly whether the list is `COMPLETE` or `PARTIAL - N of M shown`, and a `tools.$codemode.search` tool is *always registered* even when the catalog is fully inlined so a speculative call never fails (README "Discovery").

---

## 7. State and context — how a plugin reaches shared services

**Not globals. Not a service locator. Explicit injection at plugin-construction time.**

Internal plugins declare their service requirements *in their type*, and the loader provides them:

```ts
// packages/core/src/plugin/internal.ts:81-106 (abridged)
const add = <R>(input: Plugin<R>) => {
  const loaded = {
    id: input.id,
    effect: (context: PluginContext) =>
      input.effect(context).pipe(
        Effect.provideService(Catalog.Service, catalog),
        Effect.provideService(CommandV2.Service, commands),
        Effect.provideService(Integration.Service, integration),
        /* ...14 services total... */
      ),
  }
  return plugin.add(PluginV2.ID.make(loaded.id), loaded.effect)
}
```

`Requirements` (`internal.ts:37-52`) is an explicit union of the 15 services an internal plugin may ask for. Ask for something outside it and it does not compile.

**External plugins get strictly less.** Their `PluginContext` (`packages/plugin/src/v2/effect/context.ts:12-22`) is exactly `{ options, agent, aisdk, catalog, command, integration, plugin, reference, skill }` — eight domain facades plus their own config. No database, no filesystem service, no HTTP client. The README is explicit that even the server client is withheld for now: *"The public server client will be exposed separately. It is intentionally not part of `PluginContext` yet."* (`v2/effect/README.md:8`). And the facades are *narrowing*: `host.ts` wraps every domain method so a plugin gets SDK-typed values while core keeps branded IDs and decoded schemas (`host.ts:36-41`).

That is a genuine capability discipline **at the domain level**. It is not a sandbox — the plugin can still `require("fs")` — but it does mean the *supported* surface is small, typed, and enumerable.

**Two other scoping mechanisms worth stealing:**

**(a) Lifetime scoping by Proxy.** The TUI wraps the shared keymap service so that every registration method a plugin calls automatically enrolls its disposer in the plugin's scope — the plugin author writes no cleanup:

```ts
// packages/opencode/src/plugin/tui/runtime.ts:143-160
function createScopedKeymap(keymap, scope) {
  const cache = new Map<PropertyKey, unknown>()
  return new Proxy(keymap, {
    get(target, prop) {
      const value = Reflect.get(target, prop, target)
      if (typeof value !== "function") return value
      if (cache.has(prop)) return cache.get(prop)
      const fn = ScopedKeymapMethods.has(prop)
        ? (...args) => {
            const dispose = value.apply(target, args)
            return scope.track(typeof dispose === "function" ? dispose : undefined)  // auto-enroll
          }
        : (...args) => value.apply(target, args)
      cache.set(prop, fn)                                    // per-property memo: no Proxy cost after first
      return fn
    },
  })
}
```

`ScopedKeymapMethods` is an explicit `Set` of the mutating method names (`runtime.ts:80-107`) — everything else passes through unwrapped. Note the `cache`: the Proxy is paid once per property, not per call.

**(b) Scope hoisting: two lifetimes from one graph.** `LayerNode.hoist(root, tag)` walks the dependency DAG and *slices out* every node tagged `global`, returning `{ node, hoisted }` — the per-Location subgraph and the shared subgraph:

```ts
// packages/core/src/location-services.ts:89-110
LayerMap.make((ref: Location.Ref) => {
  const allReplacements = replacements.concat([[Location.node, Location.boundNode(ref)]])
  // Apply replacements during hoist, not afterward: replacements can
  // introduce new tagged dependencies (Location.boundNode depends on
  // Project), and the hoist walk is the only pass that can still slice
  // those back out.
  const location = LayerNode.hoist(locationServices, Node.tags.values.global, allReplacements)
  return LayerNode.compile(location.node).pipe(
    Layer.fresh,
    Layer.provide(LayerNode.compile(location.hoisted)),
  )
}, { idleTimeToLive: "60 minutes" })
```

One declared graph, two runtime lifetimes, checked by the type system (`tags({ location: ["global"], global: [] })` — `packages/core/src/effect/app-node.ts:3-6` — declares that location-scoped nodes may depend on global ones but not the reverse).

**And at the HTTP boundary,** a request's scope is *provided*, not looked up: middleware reads the session's directory, gets that Location's memoised service graph, and provides it to the handler (`packages/server/src/middleware/session-location.ts:54-63`). A handler cannot reach the wrong workspace's services because it never had them.

---

## 8. Concurrency model

Single process, single-threaded event loop, **Effect fibers** (green threads) for concurrency. No OS threads for logic, no worker processes for plugins.

- **Per-key serialisation.** `KeyedMutex` gives one semaphore per key with automatic entry GC (`packages/core/src/effect/keyed-mutex.ts:20-42`): "same key → queue, different key → run independently". Plugin add/remove locks on plugin id (`plugin.ts:46, 88`).
- **Per-domain serialisation.** One `Semaphore(1)` guards each domain's rebuild (`state.ts:64`), so no two rebuilds of the same domain interleave and no transform sees a half-built draft.
- **Per-session serialisation with coalesced wakeups.** `SessionRunCoordinator` (`packages/core/src/session/run-coordinator.ts`) ensures one active drain per session: a second `run(key)` **joins** the active one (`:72`) rather than racing, `wake(key)` sets a `pendingWake` flag that starts exactly one successor when the current drain settles (`:52-63`), and `interrupt(key)` clears pending work before interrupting the fiber.
- **Parallel fan-out where it is safe.** Tool calls within a turn start eagerly into a `FiberSet` and are joined before continuation (`packages/core/src/session/runner/llm.ts:277, 135-136`). Context sources load with `concurrency: "unbounded"` (`packages/core/src/system-context/index.ts:180-190`). MCP servers connect concurrently (`packages/opencode/src/mcp/index.ts:504-528`).
- **Publication ordering.** Event publication during a turn is funnelled through its own `Semaphore(1)` so stream events, tool results, and flush cannot interleave (`session/runner/llm.ts:236-238`).
- **Real threads only for rendering.** Markdown parsing and syntax highlighting run in a Web Worker (`packages/session-ui/src/components/markdown-worker.ts`).

**What prevents plugins corrupting shared state:** transforms edit a *fresh* draft that is not visible until `commit`, rebuilds are serialised, and reads see only committed state. Between rebuilds the state is effectively immutable. That plus the single-threaded loop makes the whole thing race-free without locks in plugin code.

---

## 9. What a tool and an agent actually are

**A tool is an opaque frozen token whose behaviour lives in a module-private `WeakMap`:**

```ts
// packages/core/src/tool/tool.ts:69-90 (abridged)
const runtimes = new WeakMap<AnyTool, Runtime>()

export function make(config) {
  const tool = Object.freeze({}) as Definition<Input, Structured>
  const definitions = new Map<string, ToolDefinition>()
  runtimes.set(tool, {
    definition: (name) => {
      const cached = definitions.get(name)
      if (cached) return cached                              // JSON Schema derived once per name
      const definition = new ToolDefinition({ name, description: config.description,
        inputSchema: toJsonSchema(config.input), outputSchema: toJsonSchema(config.structured ?? config.output) })
      definitions.set(name, definition)
      return definition
    },
    settle: (call, context) => /* decode input → execute → encode output → project to model content */,
  })
  return tool
}
```

Holding a tool value gives you nothing; only `Tool.definition(name, tool)` / `Tool.settle(tool, call, ctx)` (module exports) can reach the runtime. That is capability-safety by lexical scope, achieved in ~10 lines. Schemas are Effect Schema, decoded on input and encoded on output with typed `ToolFailure` on mismatch (`tool.ts:92-110`) — a tool that returns garbage produces *"Tool returned an invalid value for its output schema"*, not a downstream crash.

**Dispatch and the stale-call guard** — the single best small idea for hot-swapping:

```ts
// packages/core/src/tool/registry.ts:50-61
const settleWith = Effect.fn("ToolRegistry.settle")(function* (input, advertised?: object) {
  const registration = local.get(input.call.name)?.at(-1)?.registration ?? applications.entries().get(input.call.name)
  if (!registration)
    return { result: { type: "error", value: advertised ? `Stale tool call: ${input.call.name}`
                                                       : `Unknown tool: ${input.call.name}` } }
  if (advertised && registration.identity !== advertised)
    return { result: { type: "error", value: `Stale tool call: ${input.call.name}` } }
  ...
```

Every registration carries `identity: {}` — a fresh object token. `materialize()` snapshots the effective table at the start of a turn and closes over it; when a call settles, the token in the snapshot is compared against the token currently registered. If a plugin swapped the tool mid-turn, the call fails as **stale** rather than silently executing different code. Registrations stack (`local.set(name, [...existing, {token, registration}])`, `registry.ts:93`) and disposal removes only that entry, revealing the previous one (`registry.ts:96-101`) — so uninstalling a tool override restores the original.

**Permissions** are `(action, resource)` with wildcards, last-match-wins, defaulting to `ask`:

```ts
// packages/core/src/permission.ts:76-86
export function evaluate(action, resource, ...rulesets) {
  return rulesets.flat().findLast((rule) =>
    Wildcard.match(action, rule.action) && Wildcard.match(resource, rule.resource))
    ?? { action, resource: "*", effect: "ask" }
}
```

Note the two-level separation documented in `packages/core/src/tool/AGENTS.md:45-47`: *"Definition filtering is catalog visibility, not execution authorization."* The registry removes wholly-denied tools from the advertised list (`registry.ts:112-113`) but never authorizes execution; each tool leaf asks for its own permission at the moment of the side effect. And a user reply can be a **correction** with feedback, not just deny (`CorrectedError`, `permission.ts:62-64`) — the feedback goes back to the model.

**An agent** is a *row in a rebuildable domain*, not a class: `{ id, description, mode: "primary"|"subagent", system, model, permissions, steps, hidden }` contributed by transforms (`packages/core/src/agent.ts:22-66`). Built-ins are contributed by a plugin (`packages/core/src/plugin/agent.ts`), config-defined agents by another (`config/plugin/agent.ts`), user plugins by a third — all editing the same `Map`. Selecting one is a `Map.get` (`agent.ts:90-101`).

**Skills** are markdown files, listed in the system context and loaded on demand through a `skill` tool that injects the file's content plus a sampled file listing (`packages/core/src/tool/skill.ts:34-51`). Progressive disclosure: names + descriptions always in context, bodies only when chosen.

---

### Session, state and persistence (asked separately — worth its own note)

SQLite via Drizzle, **event-sourced**. `session_message` rows carry `(session_id, seq)` with a unique index (`packages/core/src/session/sql.ts:118-136`); `EventTable` + `EventSequenceTable` give a per-aggregate monotonic log. The durable-event commit runs **projectors inside the same transaction as the insert** (`packages/core/src/event.ts:~305-315`), and replay is idempotent-or-fatal: replaying an event at a seq that already exists succeeds only if id, versioned type and data are deep-equal, otherwise `Replay diverged at aggregate ... sequence ...`. There is an `owner_id` on the sequence row for multi-writer claims.

**Resume** = re-projecting messages from the log. **Branching** = `parent_id` on `session` (`sql.ts:30`), which is how subagent sessions nest.

**Compaction** is a message *type* in the same log: `SessionHistory.latestCompaction` finds the highest `seq` with `type = "compaction"` and history loads only from there forward (`packages/core/src/session/history.ts:13-53`). So compaction never destroys anything; it moves a pointer.

**The Context Epoch is the idea I would steal first.** `CONTEXT.md` defines it: *"The span during which one initially rendered System Context remains the immutable provider-cache baseline."* The system prompt is composed of typed `Source<A>` values, each with a stable key, a codec, an infallible `load`, and three pure renderers — `baseline(current)`, `update(previous, current)`, and optional `removed(previous)` (`packages/core/src/system-context/index.ts:33-39`). At epoch start the baseline text is rendered and a JSON `Snapshot` of every source's value is persisted (`session_context_epoch` table, `session/sql.ts:166-174`). On each subsequent turn, `reconcile` compares live values against the snapshot and, if anything changed, emits a **mid-conversation system message** describing the change:

```ts
// packages/core/src/system-context/builtins.ts:33-39
SystemContext.make({
  key: SystemContext.Key.make("core/date"),
  codec: Schema.toCodecJson(Schema.String),
  load: DateTime.nowAsDate.pipe(Effect.map((date) => date.toDateString())),
  baseline: (date) => `Today's date: ${date}`,
  update: (_previous, date) => `Today's date is now: ${date}`,
})
```

The prompt prefix therefore **never changes within an epoch**, so provider prompt caching keeps hitting across an entire long session. A source that cannot be observed returns `unavailable` and the runtime keeps its last admitted state rather than silently dropping it (`index.ts:26-28`) — and a full *replacement* is **blocked** while any previously-admitted source is unavailable (`index.ts:283-287`), so you never build a baseline that quietly omits something. Epochs end only at compaction, session move, or an incompatible transition (`context-epoch.ts:59-70`). Duplicate keys are rejected at composition time (`index.ts:315-321`).

---

## 10. The single best idea, and the single worst

### Best: state-as-replay — removal is recomputation, so nobody writes undo code

`packages/core/src/state.ts:61-127`, 67 lines of substance, and the spec that argues for it in `specs/v2/catalog-config-plugin-lifecycle.md:180-190`: *"Plugin disablement removes its config transform and lets services rematerialize without manual undo."*

The reason this beats the revertible-effect model Aleph currently has is not elegance, it is **correctness by construction**. A revertible effect is a pair (do, undo) whose correctness depends on the author writing a genuine inverse, and there is no way to test that the inverse is right except by trying. A transform has no inverse to get wrong: the state after removing plugin P is *by definition* the state you get by replaying everyone except P. The failure mode changes from "silent drift after N add/remove cycles" to "cannot happen".

It composes with the rest: because state is derived, *ordering* is a first-class knob (`PLAN.md:255-274`), *batching* is trivial (`state.ts:33-42`), and *finalisation* — the host's chance to enforce invariants after all plugins have spoken — is a single hook in the pipeline (`state.ts:66-70`, used for catalog policy filtering at `catalog.ts:160-169`).

Honourable mentions: the **Context Epoch** (§ above), **CodeMode** (§6), and the **stale-tool identity token** (§9).

### Worst: the plugin system is a defect-amplifier, and it is three systems pretending to be one

Three separate problems that are really one problem — *no isolation and no blame*:

1. **A defect in one transform kills its whole domain.** `PLAN.md:91`: transforms have no typed error channel; `state.ts:75` does `Effect.orDie`. Since a rebuild replays *all* transforms into one draft, a single throwing plugin means the domain does not rebuild — no per-plugin quarantine, no "disable the offender and continue", no probation. For a system whose thesis is *the agent writes its own plugins*, a model that has no answer to "the agent wrote a bad one" is the wrong model.
2. **Load failures are swallowed.** `packages/core/src/config/plugin/external.ts:87` — `.pipe(Effect.ignoreCause)`. A user plugin that fails to import produces no diagnostic on the server path at all. (The TUI path, by contrast, logs with plugin id and surfaces status in the UI — evidence the authors know better and the server side has not caught up.)
3. **Three plugin systems.** v1 `Hooks` (`packages/plugin/src/index.ts`, ~17 fixed callbacks, several `experimental.`-prefixed), v2 domains (`packages/plugin/src/v2/`), and the TUI slot/scope runtime (`packages/opencode/src/plugin/tui/runtime.ts`, 1,131 lines that re-implement scoping, activation, install and teardown *again* with different semantics — Promises, timeouts, KV-persisted enable flags). `packages/core/src/tool/AGENTS.md:57` admits the gap: *"Plugin boot has not been redesigned to register canonical tools through `Tools.Service`."* The v2 plugin API **cannot register a tool at all** — the `PluginContext` has no `tool` domain (`context.ts:12-22`), even though `PLAN.md:180` lists `ctx.tool.transform`/`ctx.tool.hook` as the design. So the flagship extension type is still stuck on the legacy path.

The lesson for Aleph is not "don't do this" so much as *sequence it differently*: opencode built the excellent composition model first and left blame/isolation for later, and now has three half-migrated systems. Aleph's audit found the opposite shape (boot half strong, agent-facing half unreachable) — the correction is to make the agent-facing half **the only** half, so a second system never gets started.

---

## Worth stealing for Aleph

1. **Replace revertible effects with replayable transforms for all derived state.** Aleph's kernel should distinguish two things it currently conflates: *irreversible side effects on the world* (open a connection, start a process — these genuinely need inverses and LIFO unwind, keep that) and *contributions to derived configuration* (which tools exist, which agents exist, which retrieval strategies are registered, which model profile binds what). The second class should be `State.create`-style: `initial()` + ordered transforms + `finalize()` + a serialising semaphore. Then "deactivate plugin" is `scope.close()` and Aleph's guardrail question — *did removing this actually undo everything?* — stops being a question. `packages/core/src/state.ts:61-127`.

2. **Split "contributes to state" from "intercepts an operation", and memoise the second.** This is the concrete answer to the owner's speed worry. State contributions cost nothing on the read path because the read path reads a materialised `Map` (`catalog.ts:192-197`). Operation interception is confined to a handful of explicit hook points whose *result* is cached by a stable key (`aisdk.ts:198-227`). Adopt the rule: **a plugin may not be on a per-token or per-chunk path; if it must influence one, it influences the object that path uses, once, at construction.**

3. **Context Epoch: freeze the system prompt, deliver changes as mid-conversation messages.** Aleph's research loop runs long sessions with changing context (new sources ingested, claims revised, project state moving). Re-rendering the system prompt each turn destroys provider prompt caching — the single largest avoidable cost in a long agent session. Model each contributor as a `Source<A>` with `{key, codec, load, baseline, update, removed?}`, persist a snapshot, and emit deltas. `packages/core/src/system-context/index.ts`, `packages/core/src/session/context-epoch.ts`, `CONTEXT.md`. The `unavailable` sentinel (temporarily unobservable ≠ removed, and replacement *blocks* rather than silently omitting) is the detail that makes it safe.

4. **CodeMode: one `execute` tool over a confined interpreter, instead of N tool definitions.** Directly solves the problem Aleph will hit as plugin suites multiply — every plugin wanting to add tools, blowing the context and forcing one model round-trip per call. One tool definition, a budgeted round-robin catalog with an always-registered `search` escape, explicit `{timeoutMs, maxToolCalls, maxOutputBytes}` budgets, and failures returned as typed `Diagnostic` data rather than thrown. Aleph already has `code-runner`; the missing piece is exposing *Aleph's own typed services* as the confined program's tool tree so the agent can orchestrate ingest→search→claim-extract in one program. `packages/codemode/`, `packages/opencode/src/tool/code-mode.ts`.

5. **Identity tokens on registrations, so a hot swap cannot silently execute new code for an old call.** Every registration carries a fresh `{}` token; a turn snapshots the effective table; settlement compares tokens and returns `Stale tool call: <name>` on mismatch. Twelve lines, and it is the difference between "hot swap is safe" and "hot swap is safe until someone swaps mid-turn". `packages/core/src/tool/registry.ts:50-61, 93-101`.

6. **A DAG of service nodes that type-checks its own dependencies, cycles, and replacements — then `hoist` for scope.** `LayerNode.make({service, layer, deps})` produces a compile error naming the missing service (`layer-node.ts:10-14`), throws with the full cycle path (`:189-193`), refuses a replacement across identity or tag (`:144-149`), and `hoist(root, tag)` slices one declared graph into two runtime lifetimes — global vs per-workspace — with idle eviction (`location-services.ts:89-110`). Aleph's capability graph already computes dependencies; adding tags + hoist gives per-project capability instances for free, and `unbound` nodes (`:98-106`) give you declared-but-unimplemented holes that must be filled before compile.

Smaller but cheap:

- **Proxy-scoped capability handles** (`packages/opencode/src/plugin/tui/runtime.ts:143-160`) — auto-enroll every disposer into the caller's scope, with a per-property memo so the Proxy costs nothing after first access. Plugin authors write no cleanup.
- **Teardown with a total budget** (`runtime.ts:426-462`) — LIFO, per-item timeout against a shared deadline, a failing finalizer does not abort the unwind, an `AbortSignal` for in-flight work.
- **`batch()` for coalesced rebuilds** (`state.ts:33-42`) — a `Context.Reference` holding a `Set<Reload>`; nested batches join the outer one automatically.
- **Bounded SSE subscribers with a typed overflow error** (`packages/core/src/event.ts:152-164`, `packages/server/src/handlers/event.ts`) — a dropping queue of 256 that *fails* the stream with `SubscriberOverflow` rather than growing without bound, plus a 15s heartbeat and `X-Accel-Buffering: no`.
- **Per-key at-most-one-in-flight + at-most-one-queued with superseding** (`packages/session-ui/src/components/markdown-worker-transport.ts`, 41 lines) — the right shape for any "re-render this streaming thing" pipeline.
- **UI: `partDefaultOpen`** (`packages/session-ui/src/components/part-default-open.ts`) — per-tool-kind defaults for whether a tool card starts expanded, with deletion-only edits collapsed. Tiny, and exactly the kind of judgement that makes an agent transcript readable.
- **The `perf/test-suite.md` hypothesis table** — metric, hypothesis, change, before, after, keep/revert, notes. Steal the *form*.
- **Permission replies can be a correction with feedback, not just allow/deny** (`packages/core/src/permission.ts:62-64`) — the feedback becomes model-facing.

---

## Worth avoiding

1. **Do not ship two plugin APIs, ever.** opencode has three (v1 `Hooks`, v2 domains, TUI slots) and the migration is visibly stalled: `PLAN.md:476-481` lists "Remove returned hooks" as future work, and the v2 API still cannot register a tool (`context.ts:12-22` has no `tool` domain; `packages/core/src/tool/AGENTS.md:57` admits it). Aleph's audit already found a dead agent-facing half — the fix is to delete the dead half, not to let the boot half become "v1".

2. **Do not make plugin errors defects.** `Effect.orDie` on a transform (`state.ts:75`) plus `Effect.ignoreCause` on external plugin load (`config/plugin/external.ts:87`) means a bad plugin either kills a whole domain or vanishes without a trace. Aleph needs the opposite for agent-authored plugins: a typed error channel per transform, per-plugin attribution on every failure, and an automatic quarantine that disables the offender and rebuilds without it. Aleph's spawn-ledger-with-probation is the right instinct — opencode has nothing like it.

3. **Do not adopt a fixed named-slot map as the UI extension model.** `TuiHostSlotMap` (`packages/plugin/src/tui.ts:455-486`) enumerates `home_prompt`, `sidebar_content`, `session_prompt_right`, … Adding a new place to render requires editing the host. Aleph's pane model is already better; keep it, and if you want plugin-contributed UI, make the *pane registry* a rebuildable domain like every other and let plugin-declared slot names be first-class rather than a generic-parameter afterthought.

4. **Do not let "plugin" mean "arbitrary trusted code" if agents are going to write plugins.** opencode's plugins are dynamic `import()` with no gate beyond an `engines` semver check (`packages/opencode/src/plugin/shared.ts:193-204`). That is defensible for a developer tool where the human chose the package; it is not defensible when the *agent* authors the file, and a glob over `.opencode/plugin/*.ts` will pick it up. Aleph's AST gate and the CodeMode-style confined interpreter are the two real answers — note that opencode itself reached for confinement the moment the *model* was the author.

5. **Do not let `finalize` become a second, privileged plugin system.** `PLAN.md:241` is right — *"Core finalization is for invariants and materialization, not plugin extension behavior"* — but `catalog.ts:160-169` already does policy filtering *and* event publishing there, and the plan notes the old design had "catalog hooks invoked from the catalog finalizer" that v2 is removing (`PLAN.md:276`). Keep finalisation to invariant enforcement, and put the post-commit notification after the commit, not inside it.

6. **Do not build cross-domain atomicity in, but do not pretend the problem is absent.** `PLAN.md:336`: *"The two domains rebuild sequentially. This plan does not add a cross-domain atomic transaction."* Consumers can therefore observe a state where the catalog has been rebuilt but the agents that reference it have not. For opencode that is a cosmetic flicker; for Aleph, where a belief-layer plugin and a retrieval plugin might disagree about which sources exist, it could be a correctness bug. At minimum, version each domain's committed state and let readers detect a torn read.

7. **Do not copy the deep-copy boundary outside the sandbox.** `copyIn`/`copyOut` (`packages/codemode/src/tool-runtime.ts:171, 297`) recursively rebuild every object crossing into and out of the interpreter. That is correct *there* — it is the confinement boundary. Do not generalise it to plugin↔host calls: opencode's fast path works precisely because plugins pass references into live structures and nothing is copied.
