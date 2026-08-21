# deepseek-harness — findings for the Aleph kernel

**Reviewed:** `/Users/jpmullins/Documents/code/inspiration/deepseek-harness` (read-only)
**License:** MIT, `Copyright (c) 2026 DeepSeek` (`LICENSE:1-3`). Vendored third-party licenses in
`THIRD_PARTY_NOTICES.md`. Safe to reimplement ideas; see [License and copying](#license-and-copying)
for the two things that would be unsafe to copy verbatim.
**Date of review:** 2026-08-19.

---

## In one paragraph

deepseek-harness (`dsh`) is DeepSeek's open-source agent harness. Its whole design is: *every part of
the product is a plugin*, including the parts you would normally consider "the app" — the model
adapter, the tool registry, the conversation log, and even the agent loop itself. It is built on a
framework called **Cordis** (vendored under `vendor/`, written by a third party, described by a paper
on "spatiotemporal composability" — the same paper Aleph's kernel is modelled on). A plugin is just a
JavaScript module that exports a function `apply(ctx)`. The `ctx` ("context") it receives is a
**service registry**: a shared object where plugins publish capabilities under names like `ctx.tools`
or `ctx.llm`, and find each other's capabilities by *name* rather than by importing each other's
code. A plugin declares which services it needs with `inject: ['tools', 'llm']`, and the framework
simply does not start it until those services exist — so there is no boot order to maintain. Every
registration a plugin makes (a tool, an event listener, a prompt paragraph) is a **reversible
effect**: it hands back an undo function, and unloading the plugin runs all of its undos. The whole
running system is described by a YAML file (`cordis.yml`) listing plugin rows, so a deployment is a
config edit, not a code change. On top of this, dsh adds the thing Aleph most wants: a toolset
(`cordis_define` / `cordis_run` / `cordis_stop` / `cordis_undefine`) that lets **the model write a
new plugin, in JavaScript, at runtime, and mount it into the live process** — inspected, gated, and
disposable. That half is genuinely built and shipped, not aspirational.

**Scale of what I read.** The repo is ~505k lines across ~200 workspace packages. I read, in full or
in substantial part: `packages/boot/app-boot`, `packages/core/{tools,scope,agent,session}` (heads and
the pipeline sections), `packages/extensions/{cordis-host-runner,tool-cordis}`,
`packages/llm/llm`, `packages/session/session-projection`, `packages/sandbox/sandbox-local`,
`packages/code-runtime/code-runtime-worker-thread`, `packages/preset/agent-presets` (README + core
doc), `packages/host/plugin-inventory`, `packages/api/{gateway,remotes}` (doc + lookup),
`packages/client/modules` (doc), plus `AGENTS.md`, `docs/architecture.md`, `docs/capability-seams.md`,
`docs/cordis-primer.md`, `docs/defensive-patterns.md`, `docs/subsystems/{invariants,scope,sandbox,
llm-streaming,client-modules,code-runtime}.md`, and the real deployed compositions
`packages/bundle/base/cordis.patch.yml` and `apps/cli/config/agent-presets/cordis/agent.cordis.yml`.

**What I did NOT read:** `packages/client/*` (540 TS files of browser UI), `apps/web`, `python/`,
`packages/sdk`, `packages/mcp`, `packages/workflow`, `packages/lsp`, `packages/schedule`, most test
suites, the `website/`, and — deliberately, since it is a separate assignment — the internals of
`vendor/cordis*`. Where I describe Cordis mechanics below, it is from dsh's own use of them, its
primer, and the sandbox façade that re-implements the same surface.

---

## 1. The extension model

**A plugin is a module.** Not a class, not a manifest entry, not a process. Concretely it is an ES
module that exports some subset of `{ name, inject, Config, apply }`. `apply(ctx, config)` is the
whole lifecycle hook: everything it does is a reversible effect on `ctx`, and unloading runs the
undos. There is no `activate`/`deactivate` pair and no `dispose()` method to write.

The smallest complete shipped plugin I found is `dsh-time-context` (adds the current time to each
model request). Its entire declaration surface:

`packages/context/time-context/src/index.ts:20-38, 145, 170`
```ts
/** Cordis plugin name used by loader diagnostics. */
export const name = 'time-context'

/** The agent registry that owns pre-step processing. */
export const inject = ['agents']

export interface Config {
  timeZone?: string
  refreshIntervalMs?: number
}
export const Config: z<Config> = z.object({
  timeZone: z.string(),
  refreshIntervalMs: z.number(),
})

export function apply(ctx: Context, config: Config): void {
  …
  ctx.on('agent/pre-step', async ({ agent, turn, step, signal }, next) => { … }, { prepend: true })
}
```

Four facts about that:

- `inject` is the dependency declaration *and* the capability grant (see §2, §6).
- `Config` is both a TypeScript interface and a runtime schema (`schemastery`), so a bad row in the
  YAML fails at load with a named field, not at first use.
- The single registration is `ctx.on(...)`, which returns a disposer the framework owns. There is no
  teardown code in this plugin at all — that is the point.
- The plugin never imports the agent loop. It attaches to a documented event.

**How one is declared, registered, discovered and started.** Registration is a *config row*, not
discovery. A running dsh is a plugin tree assembled from ordered YAML layers
(`docs/architecture.md:15-37`):

```yaml
# packages/bundle/base/cordis.patch.yml:15-30
- insert:
    - id: timer
      name: '@deepseek-ai/cordis-plugin-timer'
    - id: hmr
      name: '@deepseek-ai/cordis-plugin-hmr'
      config:
        root: ['.']
    - id: llm
      name: '@deepseek-ai/dsh-llm'
    - id: session
      name: '@deepseek-ai/dsh-session'
```

A **bundle** is an npm package shipping a `cordis.patch.yml`; a **profile** is a named stack of
bundles plus the user's own patch file. Layers apply in order — bundles, then the profile's patch,
then the home patch, then a `--patch` overlay — and a patch addresses a row **by id** and replaces
its whole config, or inserts new rows. `dsh --profile web --dump-config` prints the exact tree the
machine boots, and every row it prints is patchable.

The load-order comment on that file is worth quoting verbatim, because it is the property Aleph
wants:

```
# Row order carries no load semantics (activation is service-availability
# driven); the grouping is for readers.
```

There is **no boot sequence**. A plugin whose `inject` is unsatisfied simply stays in the `PENDING`
fiber state until the service appears, then activates. Removing a service re-parks its dependents
rather than crashing them.

---

## 2. Dependencies between plugins

**Plugin B absolutely can depend on plugin A, and this is the normal case — not the exception.**
This is the opposite of the VSCode fixed-extension-point model Aleph wants to avoid.

The dependency is expressed as a *service name*, not a package name:

```ts
export const inject = ['tools', 'systemPrompt', 'dynamicCordisRunner', 'cordisInspect']
// packages/extensions/tool-cordis/src/index.ts:27
```

`tool-cordis` depends on four other plugins' services and imports none of their implementations. It
resolves them as `ctx.tools`, `ctx.systemPrompt`, etc. Because the binding is by name, any package
that publishes `ctx.tools` satisfies it.

dsh formalizes this into a **capability seam**: three roles that must all exist for a capability to
be real (`docs/architecture.md:98-102`, `docs/glossary.md`):

| Role | What it is |
|---|---|
| Service Definition | The abstract service class + vocabulary types. Owns `ctx.<key>`. |
| Service Provider | A concrete implementation registered against that key. |
| Consumer | Something that calls it — usually a model-facing tool. |

`AGENTS.md:108` states the rule as a repo law: *"A capability seam comprises Service Definition /
Service Provider / Consumer roles. It is complete, never one role."* This is dsh's version of Aleph's
"ship a consumer with every producer."

The scale of actual inter-plugin dependency is documented in a **generated** graph,
`docs/capability-seams.md`, produced by `scripts/gen-doc-graphs.ts` with a completeness guard. It
lists ~45 seams. A representative row:

> `ctx.subprocess` | seam | owner `subprocess` | implementations `subprocess-local`, `subprocess-e2b` |
> **direct consumers** `bash-local`, `bash-sandbox`, `terminal-bash`, `lsp-stdio`, `subagent-acp`,
> `subagent-codex`, `subagent-claude-code`

Seven independent plugins depend on one plugin's service. Swapping `subprocess-local` for
`subprocess-e2b` moves Bash, PTY, LSP and three subagent backends onto a remote sandbox with **no
forks in any consumer** (`docs/architecture.md:102`). That is the payoff of name-bound dependencies
and it is the thing a fixed extension-point host cannot do.

**Visibility scoping.** A group row can declare an `isolate` realm, which makes a service visible
only inside that group:

```yaml
# apps/cli/config/agent-presets/cordis/agent.cordis.yml
- id: planning
  name: cordis:group
  group: true
  isolate:
    planMode: true
  config:
    - id: plan-mode
      name: '@deepseek-ai/dsh-plan-mode'
```

The comments around these realms are the best documentation of the *failure mode* of over-isolation
I have read anywhere. On why the background-jobs registry may **not** be isolated:

> "its producers sit outside any realm this file could put it in — `tool-bash` above resolves it with
> `ctx.get`, and an entry-local realm here is invisible to every sibling row, so `run_in_background`
> would answer 'background jobs unavailable' while these controls sat in the catalog."

---

## 3. How plugins communicate — the performance question

**This is the section that answers "how do I keep this fast".** dsh's answer is not one trick; it is
six separable rules, and each one is independently stealable.

### 3.1 In-process, the plugin boundary is a property read — nothing is serialized

`ctx.tools.register(...)`, `ctx.llm.stream(...)`, `ctx.fs.read(...)` are **direct method calls on
live objects** in one Node process. Nothing is serialized, nothing is proxied (except at the two
deliberate boundaries below), nothing is copied. A plugin holds `ctx` once, from `apply`, and the
service is resolved from it as a property. The service instance itself is cached by the framework —
resolution is a lookup on the context's service table, not a scan.

So the baseline "plugin tax" for an in-process call is: one property get, one method call. That is
already "as efficient as a single compiled system," and it is why dsh can afford to have ~200
plugins.

### 3.2 Interception is per-*stream*, not per-*chunk* — the key structural trick

The single most important performance decision in the codebase. Token streaming is the hottest data
path in an agent harness. dsh makes it interceptable **without** paying a plugin dispatch per token:

`packages/llm/llm/src/index.ts:913-925`
```ts
stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
  return this.streamWithRegistration(options)
}

private streamWithRegistration(options, prepared?): AsyncIterable<StreamChunk> {
  return this.ctx.waterfall(
    this,
    'llm/stream',
    options,
    () => this.adapterStream(options, prepared),   // <- returns an AsyncGenerator
  )
}
```

The `llm/stream` waterfall (dsh's around-middleware: each listener gets `(...args, next)` and calls
`next()` to delegate) runs **once per model call**. Its return value is an `AsyncIterable`. Once
constructed, the tokens flow through a plain async generator — `packages/llm/llm/src/index.ts:844-897`
— with zero event dispatch per chunk.

**This is a control-plane / data-plane split, and it is the model to copy.** Plugins negotiate *what
stream exists* (control plane, once); bytes then move through a direct iterator (data plane, N times).
A plugin that genuinely wants per-chunk visibility can wrap the iterator itself and pay for it — but
it is opt-in, not the default tax on everyone.

### 3.3 Fan-out is one subscription + N pure folds, gated by reference identity

The naive way to let 20 plugins react to a session event is 20 subscriptions and 20 dispatches per
event. dsh inverts it. `ctx.sessionProjections` subscribes to `session/event` **once**; every plugin
contributes a *pure fold*, and the framework drives them:

`packages/session/session-projection/README.md:22-23`
> **The framework drives, the domain computes.** The registry subscribes to `session/event` once;
> every committed event passes every unit's `apply` eagerly. Domains hold no subscriptions.
>
> **Same-reference means no work.** `apply` MUST return the same state reference for events that do
> not concern the unit; the drive gates the change feed on `Object.is`, so non-matching events cost
> one call and nothing downstream.

Enforced in code at `packages/session/session-projection/src/index.ts:414-415`:
```ts
const next = registration.def.apply(cell.state, event)
const changed = !Object.is(next, cell.state)
```

A `ProjectionDefinition` is `{ key, schema, init(), apply(state, event), view(state), stateVersion }`
— "three pure synchronous functions plus declarations, never an opaque getter." So a session event
that concerns nobody costs N cheap function calls that each return their argument, and *zero*
downstream work. Cells are `WeakMap`-keyed per session and built lazily.

Two supporting rules make the folds cheap: **whole-value event rule** (a state-carrying event carries
the complete post-change state, never a delta, so every transition is trivially cheap and last-wins),
and **synchronous unit discipline** (so `snapshot()` is one consistent cut in one tick).

The `token-meter` package holds itself to explicit `O(1)` fold state
(`packages/llm/token-meter/src/usage-projection.ts:84,157`, `surface-fold.ts:4`).

### 3.4 Big payloads never cross the boundary — handles do

Three separate mechanisms, all the same idea.

- **Attachments.** `ctx.attachments` commits image bytes content-addressed and returns a serializable
  `ImageAttachmentRef`. `packages/attachment/attachment/README.md:5`: *"consumers never persist
  browser paths, object URLs, provider URLs, or base64 in session events."* Only the LLM adapter, at
  the last possible moment, resolves the ref into provider-native content.
- **Spill.** `ctx.spillStore` takes an oversized tool result, writes it to a private session-scoped
  file, and returns a **model-facing locator plus retrieval hint**. The tool result that enters
  history is a preview + a locator, not the megabyte.
- **RPC lookups.** Across the browser wire, complex host objects are illegal by construction:
  `docs/api-gateway.md:11` — *"Complex Host objects cannot cross the wire directly; the business
  package must declare their association with a wire identity through `TypertLookupMap`… an `Agent`
  parameter named `agent` in the Host signature produces an `agentId` wire field, and the Gateway
  resolves that id to a Host object before invoking the business method."*

### 3.5 Validation happens at named boundaries, not at every call

`AGENTS.md:115` is the policy that keeps the in-process path free of defensive overhead:

> **Trust TypeScript at typed same-process boundaries.** Do not add runtime validation, fallback
> behavior, or hostile-input tests solely for values the static interface requires; validate at
> parser/config, queued, model/tool JSON, durable/file, worker, process, and wire boundaries.

Seven named validation boundaries. Everything between them is a plain typed call. This is the single
biggest lever on "plugin tax" in a strongly-typed system and it is a *policy*, not a mechanism.

### 3.6 Where the tax genuinely is, and how they paid it down

Three real hot spots, all handled explicitly:

**Per-token session events.** `assistant/chunk` events are genuinely per-token and genuinely durable.
The measurement and the fix are both in the module docstring —
`packages/core/session/src/chunk-rows.ts:1-17`:

> "Providers stream token-sized deltas, so a log stores hundreds of near-identical event lines whose
> JSON envelopes dwarf their payloads (**~56× measured on a real DeepSeek session**). This module
> packs each run of consecutive same-block delta chunks into ONE storage row … The encoder whitelists
> exact shapes — anything it does not fully recognize is stored verbatim, so unknown fields or future
> chunk variants lose compression, never data."

That is the only hard perf number I found in the repo, and it is honestly scoped ("measured on a real
DeepSeek session"). Note the fallback discipline: unrecognized shapes lose compression, never data.

**Cold session listing.** `ctx.sessionProjectionCache` durably checkpoints per-session projection
state and serves a "cold-read ladder: cache row + persistence tail replay, **so listings never load
full logs**" (`docs/capability-seams.md`, `sessionProjectionCache` row).

**Client plugin scanning.** `docs/subsystems/client-modules.md:44`: *"Scanning is incremental per
package; there is no full-rescan code path. Every cordis `internal/plugin` emission … marks the
fiber's entry name dirty, and a microtask flush reconciles each dirty name against the live loader
entries."* Package metadata, including the negative "not a client package" verdict, is cached per name
and never expires.

Also: the Windows sandbox rung caches its workspace ACE across sessions so "every later provision is
`O(1)` instead of re-propagating the tree per session"
(`packages/sandbox/sandbox-local/src/index.ts:8-20`), and the Python type emitter tracks a collision
counter per base name "so allocation is amortized `O(1)` instead of rescanning"
(`packages/core/tools/src/py-types.ts:149`).

### 3.7 Two places where things *are* serialized, both deliberate

- **Host ↔ browser** (`@Remote` methods over one Connection RPC carrier). JSON only, with generated
  concrete client functions — `docs/api-gateway.md:15`: *"The Client uses concrete functions on
  ordinary objects, **not a JavaScript Proxy**."* Codegen at build time, not reflection at call time.
- **Host ↔ model-written code** (the `vm` sandbox and the `run_code` worker thread). Values crossing
  into the sandbox are cross-realm-cloned with a stack-safe explicit walker that refuses anything
  lossy — `packages/extensions/cordis-host-runner/src/guard.ts:98-125` (`cloneJson`), rejecting class
  instances, functions, `Map`/`Set`, `Date`, `undefined` with a teaching error naming the fix.

### 3.8 One more structural win: mount the plugin tree once, route by scope key

This is the biggest per-session cost avoidance in the system and it deserves its own note.

A naive per-session plugin system instantiates the whole tree per conversation. dsh mounts each
**agent preset** (a `cordis.yml` describing one session's capability set) **once per process** under a
standing scope, and joins a new session to it by re-parenting a scope key
(`packages/preset/agent-presets/README.md:5`):

> "the roster mounts it ONCE per process under a standing scope, and each session that names it joins
> by having its agent scope key parented to the mount's … The mount's tools, prompt sections, and
> projection units exist exactly once and cover every joined agent — its plugins key their state by
> Session/Agent, so sessions stay apart inside one shared instance."

Reads then resolve `agent → preset → global`, nearest shadowing farthest
(`packages/core/scope/src/store.ts:192-217`). Joining a child agent to its parent's composition is a
**synchronous bind**, not a mount, which is what lets in-process subagent drivers compose children
inside a synchronous creation window.

---

## 4. Lifecycle

**Load / activate.** Not two steps. A row is created; its fiber is `PENDING` until `inject` is
satisfied, then `LOADING`, then `ACTIVE`. The five public phases are mirrored at
`packages/host/plugin-inventory/src/index.ts:22-40`: `pending | loading | active | failed | unloading`
(`DISPOSED` projects to `null`).

**Deactivate / unload.** One operation: dispose the fiber. Everything the plugin registered is an
effect on that fiber, so unwinding is automatic. The dynamic-package lifecycle module says it flatly
(`packages/extensions/cordis-host-runner/src/lifecycle.ts:4-7`):

> "Stopping needs no helper — a host half unwinds through an ordinary awaited `fiber.dispose()`,
> because everything the plugin registered is an effect on its fiber."

The effect idiom is a generator that yields its own undo — `packages/core/scope/src/store.ts:233-256`:
```ts
const dispose = ctx.effect(function* (this: ScopedLayers<L>) {
  …
  let undo: () => void
  try { undo = action(layer) } catch (error) { …; throw error }
  yield () => {                    // <- the undo, run on disposal
    undo()
    if (scope !== undefined && layer.isEmpty()) this.scoped.delete(scope)
    if (notify) this.onChange()
  }
  if (notify) this.onChange()
}.bind(this), options.label)
```
Note the two details: the undo is *collected before* the change notification fires, and an empty
scope layer is reclaimed on undo so scoped state does not leak.

**Teardown must reach quiescence.** `docs/defensive-patterns.md`:

> "**Dispose must reach quiescence, not just request it.** A teardown that issues kills/aborts but
> returns before the work stops leaves orphans. Make cleanup async and await the children's exit
> (kill → await `done`), and close listener/notification registries BEFORE killing so late
> completions stay silent."

Implemented literally: `packages/core/scope/src/index.ts` `quiesceFiber` loops
`while (fiber.inertia !== undefined) await fiber.inertia`.

**Hot reload.** Two kinds, both real.

1. *Config* hot reload, no restart. `watchUserPatches` registers the user's `cordis.patch.yml` with
   the HMR service and, on change, **transactionally reapplies the entire patch composition** to the
   root include entry (`packages/boot/app-boot/src/index.ts:227-260`). It deliberately re-reads the
   include's non-patch options each refresh so a concurrent writer's changes are not silently
   reverted.
2. *Code* hot reload via `@deepseek-ai/cordis-plugin-hmr`, mounted in the base bundle
   (`packages/bundle/base/cordis.patch.yml:18-22`).

**In-flight work during a swap.** Handled by *version pinning per participant*, not by draining.
Agent presets: "the composition a running session joined outlives its file changing or disappearing
underneath it" — each generation records its file's mtime+size stamp; a *new* session sees the stale
stamp and starts a new generation, while sessions already joined keep theirs
(`packages/preset/agent-presets/README.md`, "Where to call `mount()`"). Dynamic packages: each
activation has a `pluginRunId`, and any call carrying a stale one is refused with
`code: 'stale-run'` rather than executed against the new generation
(`packages/extensions/cordis-host-runner/src/index.ts:752-754`).

---

## 5. Failure and blast radius

**At load, dsh is maximally loud.** This is the honest-miss philosophy at boot.

`assertEntriesActivated` (`packages/boot/app-boot/src/index.ts:692-726`) audits the settled tree and
refuses to run a partially-composed system:

```ts
if (state === FIBER_FAILED) {
  try { await fiber.await() } catch (error) {
    rejectionReasons.push(error)
    failures.push(`${entry.options.name}: ${formatActivationError(error)}`)
  }
  continue
}
if (state === FIBER_PENDING) {
  const missing = Object.keys(fiber.inject).filter(service => fiber.ctx.get(service) === undefined)
  failures.push(`${entry.options.name}: pending (waiting for ${subject}: ${missing.join(', ') || 'unknown'})`)
}
```

A stuck plugin does not fail with "startup failed" — it names *which services it is still waiting
for*. The docstring states the reason a pending entry is reported differently: *"pending entries name
their unresolved services because no plugin error exists for that state."* The boot wrapper also digs
to the deepest `cause` so the diagnostic carries the plugin's own stack, not just the loader's wrap
chain (`packages/boot/app-boot/src/index.ts:815-822`).

`installFailLoud` (`:609-655`) turns a late unhandled plugin-init rejection into one labelled stderr
line and `exit(1)`. Its docstring is a small masterclass in teardown ordering: it writes the
diagnostic *before* releasing the terminal so a hanging disposer cannot swallow the reason; it keeps
the handler installed during release so a second rejection cannot kill the process mid-teardown and
strand the user's terminal in raw mode; and the release race has a 2s timeout whose timer stays
referenced so a never-settling disposer cannot let Node reach an empty loop and exit `0`.

**At runtime, containment is explicit and two-tiered.** The rule
(`docs/defensive-patterns.md`): *"Contain callback exceptions in the dispatcher … one bad subscriber
never breaks core lifecycle."*

The most interesting implementation is `packages/settings/settings/src/index.ts:772-799`, which splits
errors into two classes:

```ts
// Fan the event out one listener at a time (the plain emit stops at the
// first throwing listener, starving the rest). Invariant violations are
// harness-fatal by design and rethrow after every listener ran; any other
// failure is contained so one broken observer cannot wedge the commit
// path (and, through it, a provider's reload loop).
…
} catch (error) {
  if ((error as { code?: unknown } | null)?.code === 'INVARIANT') { invariantFailure ??= error; continue }
  this.warnListenerFailure(registration.ns, error)
}
…
if (invariantFailure !== undefined) throw invariantFailure as Error
```

**A contract violation is fatal; an ordinary bug is contained.** That is a much better default than
"contain everything" (which hides real corruption) or "crash on everything" (which lets one bad
plugin take the process down).

Same pattern in telemetry (`packages/session/session-telemetry/src/coordinator.ts:256-266`), which
wraps each capture step: *"cordis `emit` is stop-on-throw, so a throwing listener would starve every
subscriber registered after this plugin — nothing from the backend may escape."*

**Opposite failure postures at different phases.** `docs/subsystems/client-modules.md:44`:

> "The activation pass seeds the same dirty set with all current entries and flushes synchronously,
> so first scan and steady state share one implementation — with **opposite failure postures**. At
> activation, a malformed declaration or missing bundle … aggregates into one loud `AggregateError`
> listing every broken package: the fiber FAILS and the boot's fail-loud sweep reports it. In steady
> state, a broken package logs a warning and must not poison the others."

**Can one bad plugin take the process down?** At boot, yes — deliberately. At runtime, no for
listener bugs and for the code-runtime worker (a separate thread with a heap cap; heap overflow kills
the worker and surfaces as `kind: 'worker-exit'`). Yes, in principle, for a host-half dynamic package
doing something catastrophic — the `vm` sandbox is explicitly *not* a security boundary (§6).

**Can the system refuse to remove something load-bearing?** No, and this is a real gap versus Aleph's
kernel. There is no protected-core set and no removal refusal. What exists instead is *parking*: a
dependent whose service disappears returns to `PENDING` rather than crashing, and the guard façade's
error text teaches a dynamic plugin to declare `inject` precisely so this happens
(`packages/extensions/cordis-host-runner/src/guard.ts:724-727`):

> `service "${prop}" is not injected. Declare it: inject: ['${prop}', …] on your plugin, so cordis
> parks this dynamic package if the provider later goes away.`

Parking is a graceful degradation story, not a safety story. **Aleph's dependency-graph removal
refusal is a genuine improvement over this, and I would keep it.** The right combination is
*refuse to remove a protected/load-bearing capability* **plus** *park, don't crash, when a
non-protected one goes*.

**Rollback / quarantine.** `startHostHalf` disposes a failed fiber before rethrowing, so a failed run
never leaves a half-mounted plugin (`packages/extensions/cordis-host-runner/src/lifecycle.ts:29-43`).
`resolveRequestRun` unwinds the host half only when *this same request* evaluated it, "so a page that
cannot load its own half never stops a package the other pages are using"
(`packages/extensions/cordis-host-runner/README.md`). There is no probation ledger and no automatic
retry.

---

## 6. Trust and agent-authored code

**Yes, an agent can write and mount a plugin here, and this is the shipped feature.** The flow is
seven model-facing tools (`packages/extensions/tool-cordis/src/index.ts`):

`cordis_inspect_list` → `cordis_inspect_query` → `cordis_inspect_self` → `cordis_define` →
`cordis_run` → `cordis_stop` / `cordis_undefine`.

The model writes plain JavaScript (no TypeScript, no bundler, no imports) that must `return` a Cordis
plugin. A "package" has up to two halves: a **host half** (Node process) and a **client half**
(browser page). Versions are immutable: `packageId` is one frozen source version under a stable
`pluginId`; changing code means defining a new package, never overwriting.

### The inspection gate

Three separate gates, in order.

**Gate 1 — compile-only precheck at define time.** `packages/extensions/cordis-host-runner/src/sandbox.ts:206-214`:
```ts
export function precheckCode(code: string, half: 'code.host' | 'code.client'): void {
  try {
    // Compile-only: constructing the Script parses the source and runs nothing.
    new Script(`(async () => {\n${code}\n})()`, { filename: `cordis-dyn-${half}.js` })
  } catch (error) {
    if (!isSyntaxError(error)) throw error
    throw new Error(parseErrorMessage(half, syntaxErrorContext(error)))
  }
}
```
Unparseable code is refused *before an id exists*, so there is nothing to roll back. The failure text
is engineered to teach — it surfaces the vm's own offending-line-and-caret prelude, and detects the
two mistakes a model actually makes: a TypeScript `as` annotation, and `});` closing a call that was
never opened (`:179-194`).

**Gate 2 — a fresh vm realm with redirect traps.** `createSandbox` (`:129-145`) builds a `node:vm`
context whose globals are: a package-tagged write-through console, `btoa`/`atob`/`TextEncoder`/
`TextDecoder`, the `harness` helpers, and **callable traps for the Node APIs deliberately withheld**.
The traps do not just fail — they *redirect* (`:96-108`):
```ts
const NODE_API_REDIRECTS: Record<string, string> = {
  require: 'Node modules are unavailable. Use the cordis services on ctx instead — e.g. inject: [\'fs\'] for files, …',
  setTimeout: TIMER_REDIRECT,   // → declare inject:['timer'] and use ctx.timeout / ctx.interval,
                                //    "those calls are fiber effects, cleaned up automatically when stopped"
  fetch: 'Network access goes through the cordis web service: declare inject: [\'web\'] …',
}
```
A note on craft: only *function-valued* globals are trapped. `process` is left `undefined` rather
than given a throwing accessor, "because a throwing accessor would detonate the common `typeof
process` feature probe at resolution time."

**Gate 3 — the context façade, which is where "capability" actually lives.** This is the most
important single file for Aleph. `sandboxContext` (`guard.ts:718-786`) replaces the real `ctx` with a
`Proxy` whose `get` allows exactly three things:

```ts
const CTX_VERBS = new Set(['effect','on','once','provide','timeout','interval','setTimeout','setInterval','throttle','debounce'])
const TIMER_VERBS = new Set(['timeout','interval','setTimeout','setInterval','throttle','debounce'])

function declaredInjects(ctx: Context): Set<string> {
  return new Set(Object.keys(ctx.fiber.inject))
}
```

1. **lifecycle-safe verbs** (`CTX_VERBS`) — everything that creates a *reversible* effect;
2. **`ctx.tools`**, but through a read-only façade;
3. **services the plugin declared in `inject`** — and nothing else.

Everything else is denied. Crucially, the denial **distinguishes two causes so the error teaches the
right fix** (`:723-736`):
```ts
const denyRead = (prop: string): never => {
  if (ctx.get(prop) !== undefined) {
    return rejectGuard(reportFailure,
      `service "${prop}" is not injected. Declare it: inject: ['${prop}', …] on your plugin, `
      + 'so cordis parks this dynamic package if the provider later goes away.')
  }
  return rejectGuard(reportFailure,
    `sandbox ctx does not expose "${prop}". Available: … Framework internals (root, fiber, registry, extend, plugin, …) are withheld by design.`)
}
```

*"You forgot to declare it"* versus *"you may never have it"* are different errors. This is the exact
distinction Aleph's capability-access enforcement should make.

Three more hardening details in the same file:

- **`set` is refused**: *"A façade is not the real ctx; block writes rather than let package code
  stash state on a throwaway object and think it persisted."*
- **`has` reflects reachability without resolving** — `in` answers façade API + declared services,
  whether or not currently live, and never throws.
- **Service returns are re-guarded.** `guardedService` (`:685-698`) proxies every injected service so
  each return value passes `denyContext`, which refuses any value that `instanceof Context`
  (`:669-680`): *"a value that is one would be a fresh, unguarded handle back into the runtime — the
  exact escape the façade exists to close."* Promises are guarded on resolve.
- **`ctx.tools.get()` returns a schema view, never the live `ToolDefinition`** (`:640-654`) —
  *"Exposing the raw definition would hand package code the tool's `execute` function, letting it call
  another tool directly and bypass `ToolRuntime.execute` — identity protection, pre-policy, monotonic
  guards, around dispatch, post-policy, final observation, and result normalization."*

That last one is the deepest lesson: **a capability leak is usually a handle leak, and the handle is
usually a function on an object you returned for a different reason.**

### The honest trust statement

`packages/extensions/cordis-host-runner/README.md`, "Trust stance":

> "The vm sandbox isolates globals but **is not a security boundary**: Node globals are absent or
> redirect to Cordis services … yet the services it declares reach the live runtime. **Treat a dynamic
> package like bash access.**"

And the preset that enables it says the same to the user
(`apps/cli/config/agent-presets/cordis/agent.cordis.yml:9-13`):

> "TRUST: `cordis_mount` evaluates model-written JavaScript against the live runtime, and a
> composition this agent writes becomes a preset other sessions mount. **Treat a session on this
> preset as shell access** — the toolset's own documentation makes the same statement."

Same for the code runtime: `isolation` (`'worker-thread' | 'process' | 'container'`) is *"a
diagnostic label, **not a security claim**"* (`docs/subsystems/code-runtime.md:161`).

Refusing to overclaim isolation is itself the honest-miss philosophy.

### The human gate

Code never rides an announcement. A run request for a package with a browser half becomes a suspended
round trip settled by a *person* allowing or declining; `getClientCode` is the only path by which
source reaches a browser. Approval has two tiers: one check mark authorizes the current package
version, double check marks authorize future versions of the same plugin. And the failure mode is
stated honestly: with no page connected, the request "suspends like any other unanswered request and
ends in `cancelled`" — no timeout, no pretending.

### Real containment (where it exists)

- `packages/code-runtime/code-runtime-worker-thread/src/index.ts:24-49` — a fresh worker per program
  with: a **measured event-loop busy-time budget** (`computeMs`, sampled via
  `worker.performance.eventLoopUtilization()` — *"fair (a program awaiting a slow tool accrues
  nothing) and ungameable (a hot loop accrues whether or not a decoy dispatch is in flight)"*), a
  wall-clock backstop for what busy-time cannot see, a max output byte cap, and a heap cap.
- `native/landlock-run` — a ~300-line C11 self-restrict-then-exec Landlock launcher, statically
  linked against musl. It installs the ruleset **on itself** then `exec`s, so the ruleset is inherited
  across `execve` and the invoking harness stays unrestricted. Fail-closed: *"if the kernel cannot
  enforce, it exits without running the command."*

---

## 7. State and context

**A context object that is a service registry**, resolved by name. Not global singletons, not
constructor DI, not imports.

- `ctx.<key>` resolves a service; `ctx.get('key')` is the optional form that returns `undefined`.
- The *declared* form (`inject`) is a hard dependency: the plugin does not run without it, and parks
  if it goes away. The *optional* form (`ctx.get`) tolerates absence. The prompt dsh gives its own
  model states the choice precisely (`packages/extensions/tool-cordis/src/prompt.ts:60-62`):
  > "Read an optional Service with `ctx.get('serviceName')` by default and handle undefined. Declare
  > `inject: ['serviceName']` … only when the Service is a hard dependency and the Plugin must enter
  > waiting until Cordis reactivates it after the Service appears."

**Is everything reachable by everything?** For statically composed plugins, largely yes within a
realm — that is the cost of the model, and dsh accepts it. Three mitigations:

1. **`isolate` realms** on group rows scope a service's visibility to a subtree (§2).
2. **Scoped layers.** A registry's contributions are filed into the *calling context's* scope layer,
   and reads resolve `agent → preset → global` with nearest shadowing farthest
   (`packages/core/scope/src/store.ts:152-217`). So "the tool registry" is one object, but two agents
   see different tools from it. A deliberate detail: `peek()` is *chain-blind*, "so callers addressing
   one scope's OWN contributions (its restrictions, its guards) must not silently pick up an
   ancestor's."
3. **The façade** for dynamic plugins (§6) — the only place where access is actually *enforced*.

**Cycle safety.** Scope parent links are cycle-checked on every bind and rebind
(`packages/core/scope/src/index.ts:54-59`), and re-linking requires the privileged
`ScopeParentBinding` handed only to the original binder — *"there is no open re-link path, so a
scope's ancestry cannot be moved by anyone but the original binder."*

---

## 8. Concurrency model

Single-process, async/await, cooperative — with escapes to threads and processes at named seams.

- **Cancellation is an `AbortSignal` threaded end to end.** Every waterfall payload carries one
  (`agent/pre-step`, `agent/request`, `agent/request-error`, `agent/turn-stopping` — all with
  `signal: AbortSignal`, `packages/core/agent/src/runtime-types.ts:231-278`). A `tools/execute`
  wrapper *may replace* the signal for its delegated lifetime but **cannot remove it**: "the registry
  fuses every replacement with the captured caller signal"
  (`packages/core/tools/src/index.ts:387-393`). That is a genuinely good design — it lets a timeout
  wrapper narrow cancellation without letting a buggy wrapper detach the user's cancel button.
- **Tool concurrency is declared per call, data-dependent, and fail-closed to serial**
  (`packages/core/tools/src/index.ts:1276-1284`):
  ```ts
  executionMode(exec: ToolExecutionInput): ToolExecutionMode {
    const tool = this.resolveExecution(exec.name, exec.agent, exec.parent !== undefined)
    if (!tool?.isConcurrencySafe) return { kind: 'exclusive' }
    try {
      const concurrencySafe: unknown = tool.isConcurrencySafe(exec.arguments)
      return concurrencySafe === true ? { kind: 'parallel' } : { kind: 'exclusive' }
    } catch { return { kind: 'exclusive' } }
  }
  ```
  *"Only an exact `true` is parallel; unknown, hidden, undeclared, invalid, or throwing classifiers
  are exclusive."* Exclusive calls form ordering barriers.
- **Long-running work leaves the turn.** `ctx.jobs` registers background work (background bash, PTY
  sends, subagent delegations) with per-owner concurrency admission (default 10); `job_*` tools let
  the model collect or kill it.
- **Asynchronous results re-enter the conversation through a four-verb inbox**, not by blocking a
  tool (`packages/core/agent/src/runtime-types.ts:105-142`):

  | verb | semantics |
  |---|---|
  | `send(msg, target, wakeup)` | route to a `next-turn` / `next-step` boundary, optionally waking |
  | `followup(msg)` | queue a turn of its own and wake |
  | `steer(msg)` | consumed at the nearest step boundary; idle driver starts a turn |
  | `inject(msg)` | queue model-facing context for the next pre-step **without waking** |

  The dynamic-package runner uses `agent.steer(...)` to report an async render failure or a host
  handler error back into the model's context long after the tool returned
  (`cordis-host-runner/src/index.ts:1019-1110`). The model prompt is explicit about the contract:
  *"Do not wait inside a Tool for approval or browser work that can happen only after the current turn
  ends."*
- **Threads and processes** at seams: `code-runtime-worker-thread` (worker per program),
  `ctx.subprocess` (process tree ownership, stdio dispositions, kill escalation),
  `workflow-worker-thread`.
- **Shared-state corruption prevention** is by construction rather than by locks: projections are
  pure folds over an append-only log; session events are append-only; registry mutations are
  synchronous single-statement effects with an undo; the inbox is a replay-once projection of durable
  splice events (`packages/core/agent/src/inbox.ts:25-40`).

---

## 9. What an agent / tool actually is

**An `Agent` is a live handle**, not a class you subclass: `{ id, ctx, session, inbox, status,
cancel(), send/followup/steer/inject, runMaintenance() }`. `ctx.agents` is the registry of live
handles plus a create/resume factory seam. `ctx.agentLoop` is *"the one concrete loop plugin"* — and
notably, `docs/capability-seams.md` records that *"extension packages depend on `dsh-agent` events and
services, not on this package."* The loop is replaceable because nothing imports it.

The turn model (`docs/architecture.md:63-84`) — a **step** is one model request plus its tool calls; a
**turn** is zero or more steps:

```
turn/start
  claim next-step input plus one queued message
  assemble prompt sections + tool schemas
  -> agent/pre-step                   reject | enter(messages)
     step/start
     append entered messages as user/message
     derive model history from the log
     agent/request -> llm/stream -> assistant/chunk* -> assistant/message
     tool/call* -> tools/pre-execute -> tools/execute -> tools/post-execute -> tool/result*
     step/end
  -> agent/turn-stopping
turn/end
```

**A tool** is a `ToolDefinition` registered on `ctx.tools`, carrying: `name`, `description`,
`parameters` (a `ParameterSchemaSpec` compiled to JSON Schema), `execute`, an **`output` block that is
mandatory** (`{ schema, render, presentationMeta? }` — registration throws without it,
`packages/core/tools/src/index.ts:1037-1046`), optional `timeoutMs`, and optional
`isConcurrencySafe(args)`.

Two things about the tool pipeline are worth stealing on their own:

**Monotonic guards.** `packages/core/tools/src/index.ts:1101-1128`:
> "Register a monotonic guard after the extensible `tools/pre-execute` waterfall. A plain-context
> guard applies globally; one registered through `agent.ctx` applies only to that agent. **Any
> matching guard may deny by returning a reason, while no guard can force-allow a call another guard
> denied.**"

Monotonicity means adding a plugin can only ever *reduce* what is permitted. Security composes.

**`restrict()` refuses no-ops and unknown names.** Same file, `:1069-1096`:
```ts
if (allow === undefined && deny === undefined) {
  throw new Error('tools.restrict({}) is a no-op: pass `allow` and/or `deny` (an empty filter is almost always a materialized-empty-config bug)')
}
…
if (unknown.length > 0) {
  throw new Error(`tools.restrict() names unknown global tool… known global tools: ${[...known].sort().join(', ') || '(none)'}`)
}
```
An empty restriction is treated as a *bug*, not as "restrict nothing." Aleph should adopt this
posture everywhere a filter can be materialized empty by a config path.

**Code Mode.** Instead of N model round trips for N tool calls, the registry can collapse the whole
catalog behind one `run_code` tool: the model writes a program, calls tools as
`await tools.name(args)` inside it, and only the curated program output enters model history. Each
sub-dispatch is still logged for reconstruction. This is a *token/latency* optimization at the
protocol level and it is enabled by the same registry that does everything else
(`packages/core/tools/src/code-mode.ts`). The collapse statement is explicit to the model
(`index.ts:57`): *"`run_code` is the only tool you can call directly — a tool call naming any other
tool fails."*

**Model-visible ⟺ logged.** `AGENTS.md:107`: *"anything that reaches a model request must be
reconstructable from the session log; a new model-visible input requires a session event."* Enforced
by a runtime invariant. This is the same discipline Aleph's ledger aims at, applied to prompts.

---

## 10. The single best idea, and the single worst

### Best: the `./invariant` companion contract

Every one of the ~219 workspace packages publishes a `./invariant` subpath exporting a Cordis plugin
that registers *runtime* checks under its own npm package name
(`docs/subsystems/invariants.md`). A check may assert only **authoritative event streams or mutable
data** — never "does this service exist" or "does this method have a name."

The part that makes it great is the negative case. **184 of the 219 invariant files are deliberately
empty**, and each one carries a comment beginning `No runtime invariant:` followed by a
package-specific explanation of why nothing is checkable. For example:

- `packages/llm/llm-deepseek/src/invariant.ts:18` — *"No runtime invariant: this package exposes no
  independent event sequence or mutable data relation…"*
- `packages/context/tmux-context/src/invariant.ts:18` — *"No runtime invariant: a reading is a
  per-turn snapshot of external tmux state, so the session…"*
- `packages/llm/token-meter/src/invariant.ts:18` — *"No runtime invariant: token estimates are
  per-call outputs and the private…"*

And `pnpm run verify-package-invariants` "mechanically rejects generated markers, unexplained empty
installers, non-empty installers that omit or ignore the reporter, incorrect registration names, and
incomplete export, publication, dependency, or bundle wiring."

A real one, for contrast — `packages/core/scope/src/invariant.ts:16-32` asserts that every
scope-filtered event is dispatched with a carrier keyed to the same subject its arguments name, and
the failure message tells you the fix (`use agentEvents(ctx, agent)`).

This is *institutionalized honesty about coverage*. It is exactly the antidote to the failure mode
Aleph's CLAUDE.md documents at length: a doc that asserts invariants which are false in code. Here,
"we checked and there is nothing to check, because —" is a first-class, mechanically-verified
artifact. Combined with the two-tier containment (`code: 'INVARIANT'` is fatal, everything else is
contained), the invariant becomes a *live* contract rather than a comment.

### Worst: prose volume as the primary carrier of design intent

The reasoning in this repo is extraordinary — but a very large fraction of it lives only in prose:
READMEs, module docstrings, `docs/`, `.agents/notes/`, and 15KB of `AGENTS.md` — all of it maintained
in **two languages** (`.md` + `.zh.md` + an `.i18n.yaml` per file), gated by a `doc-sync` job and
`verify-doc-budgets` word ceilings.

The cost shows up structurally:
- `packages/extensions/tool-cordis/src/api-catalog.ts` is **4,761 lines** of hand-maintained
  descriptions of other packages' APIs, existing so the model can query them. It is a second copy of
  facts that live in the source.
- `docs/capability-seams.md` and `docs/config-catalog.md` are generated (good), but sit beside
  hand-written `docs/subsystems/*.md` covering the same subsystems (drift surface).
- Rules like "the agent path talks only to the gateway," "no hardcoded tunables," "every registration
  is an effect" are enforced by *review and prose*, with only some backed by a `scripts/` gate.

For Aleph the lesson is sharp: dsh's per-package `invariant.ts` is the *right* shape (executable, in
the package, mechanically verified) and its `api-catalog.ts` is the *wrong* shape (a 4.7k-line
hand-written mirror). Prefer generating the model-facing catalog from the same declarations the
runtime uses — which Aleph already does for the A2UI catalog via `scripts/gen_catalog.py`, and should
extend to the kernel's capability surface.

**Runner-up worst:** no protected core and no removal refusal (§5). `undefine` and `stop` will happily
take away whatever the model asks for; the safety story is only that dependents park.

---

## Calibration: what Aleph's kernel already has

Read after forming the view above, so the recommendations are not duplicates. Aleph's
`packages/aleph-kernel` (~2,120 lines) is closer to dsh than I expected, and ahead of it in three
places.

**Already present, do not rebuild:**

- *Reversible effects with LIFO unwind* — `spec.py:41-55`, an async-generator `Setup` where every
  `yield` is an inverse. Same idiom as `ctx.effect(function*(){ … yield undo })`
  (`scope/src/store.ts:233-256`).
- *Realm-based isolation* — `context.py:145-160` `Context.isolate(key, realm)` with root-realm
  fallback, so isolation is additive. Structurally identical to Cordis `isolate:` on a group row, and
  the fallback behaviour is *better*: dsh's realms are invisible to siblings, which is the exact trap
  the `agent.cordis.yml` comments spend paragraphs warning about.
- *The two-message denial* — Aleph already distinguishes `UndeclaredAccess` ("Add `x` to its
  `requires`") from `InactiveAccess` ("its provider is not active"), `errors.py:27-56`, and the
  docstrings already state why the caller's correct response differs.
- *Provide-with-inverse by construction* — `context.py:126-140`: *"There is no way to publish without
  also registering the removal."*
- *Mandatory probes* — `spec.py:21-38`, with a failing probe required to carry a detail. This is
  **ahead of dsh**, which probes only sandbox backends (`sandbox-local`) and nothing else.
- *Protected core + removal refusal* — `ProtectedCapability`, `DependentsWouldBreak`,
  `Kernel.blast_radius()`. **Ahead of dsh, which has neither** (§5).

**Not present, and the highest-value additions from this review:**

- Return-value re-guarding (steal #4) — nothing stops an Aleph capability from returning a live
  `Context` or an invocable handle to an undeclared service.
- Stream-construction interception (steal #1) — Aleph has no equivalent seam yet, and this is where a
  plugin system becomes slow if it is designed wrong.
- Framework-driven pure folds (steal #2) — Aleph's derived-confidence and projection paths currently
  have no shared drive.
- Monotonic guards and no-op-refusing restrictions (steal #5).
- The `./invariant` companion with explained emptiness (steal #6) — Aleph's acceptance checks are the
  nearest analogue but are centralized, not package-owned.
- The four-verb agent inbox (steal #12).
- A named list of validation boundaries (steal #16).

**The real gap Aleph's audit already found** — the agent-facing half being unreachable — is precisely
the half dsh has *shipped*. `AgentPluginAPI` (`agent_api.py:66-176`: `inspect` / `install` / `enable`
/ `check_health` / `disable`) is the right surface; what dsh adds on top of it and Aleph lacks is
(a) a model-facing tool set wired to it (`tool-cordis`), (b) an inspect provider protocol so the model
can query the live service/event catalog before writing code (`cordis_inspect_list` /
`cordis_inspect_query`), (c) a human approval gate with a two-tier grant, and (d) immutable versioned
definitions with a stale-generation refusal. Those four are what turn `install()` from an API into a
feature.

---

## Worth stealing for Aleph

Ordered by expected value. Items marked ✔ are already in Aleph's kernel — listed only so the
orchestrator does not schedule them twice.

1. **Interception at stream-construction, never per-chunk.** Make Aleph's kernel effects wrap the
   *creation* of an iterator/generator, and let the data flow through a plain generator afterwards.
   `llm/llm/src/index.ts:913-925` is the template. This is the single answer to "will plugins make it
   slow": the plugin tax is `O(calls)`, not `O(bytes)`.

2. **Framework-driven pure folds with reference-identity gating.** One subscription; every plugin
   contributes `{ init, apply, view, stateVersion }`; `Object.is(next, prev)` gates all downstream
   work; `stateVersion` invalidates the persisted cache. `session-projection/src/index.ts:414-415`.
   For Aleph this maps directly onto belief-layer derived state: a claim edge that concerns no
   projection costs one no-op call.

3. **The *three*-message capability denial.** ✔ partly. Aleph already separates "undeclared" from
   "provider inactive" (`errors.py:27-56`). dsh adds a third that Aleph lacks: *"withheld by design"*
   — a key the runtime has but will never expose to this caller (`guard.ts:723-736`, framework
   internals). Add it, and keep dsh's habit of naming the available alternatives in the message.

4. **Re-guard what services return, and never return an invocable handle.** `denyContext`
   (`guard.ts:669-680`) refuses any returned `Context`; `sandboxTools.get()` returns a schema view,
   never the live definition with its `execute`. Aleph's `code-runner` and any future agent-facing
   kernel API should assume the leak is a function on a returned object.

5. **Monotonic guards + no-op-refusing restrictions.** Adding a plugin may only narrow permissions;
   no guard can force-allow (`tools/index.ts:1101-1128`). And an empty filter throws
   (`:1069-1078`) — treat a materialized-empty policy as a bug, because it almost always is.

6. **The `./invariant` companion with mandatory explained emptiness.** Per-package runtime checks
   over owned event streams / mutable data, with a mechanical gate rejecting unexplained empties.
   Then split error classes: contract violation (`code: 'INVARIANT'`) is fatal; everything else is
   contained (`settings/src/index.ts:772-799`).

7. **Mount the composition once; join by re-parenting a scope key.** Per-project or per-session plugin
   sets should not re-instantiate the tree. One standing mount, plugins key state by project/session,
   reads resolve `agent → preset → global` with nearest-shadows-farthest
   (`agent-presets/README.md`, `scope/src/store.ts:192-217`). Generation-pin by file stamp so a
   running session outlives an edit to its composition.

8. **Boot audit that names unresolved services, not "startup failed."** `assertEntriesActivated`
   (`app-boot/src/index.ts:692-726`) — for `PENDING` entries, list exactly which injected services are
   still `undefined`. Aleph's kernel probe already runs a live read path; add this diagnostic shape.

9. **Handles, not payloads, across every boundary.** Content-addressed attachment refs; spill locators
   for oversized tool text; `agentId` wire fields resolved to live objects host-side. Aleph's asset
   store already does the first; extend the discipline to any future plugin-to-plugin transfer.

10. **Declared, per-call, fail-closed concurrency.** `isConcurrencySafe(args)`; only exact `true` is
    parallel; unknown/throwing ⇒ exclusive barrier (`tools/index.ts:1276-1284`).

11. **Signal fusion, not signal replacement.** A wrapper may narrow the cancellation signal for its
    delegated lifetime but the runtime re-fuses the caller's signal, so cancellation can never be
    detached by a wrapper bug (`tools/index.ts:387-393`).

12. **The four-verb agent inbox** (`send` / `followup` / `steer` / `inject`) for async plugin results
    re-entering the model context without blocking a tool call
    (`agent/src/runtime-types.ts:105-142`). Aleph's research loop and reviewers need exactly this.

13. **Compile-only precheck before an id exists**, with teaching errors carrying the offending source
    line (`sandbox.ts:206-214`, `:179-194`). Aleph's AST gate should refuse before minting a plugin id
    and should surface the caret line, not a class name.

14. **Measured busy-time budgets, not wall clocks**, for sandboxed code — plus a wall-clock backstop
    for what busy-time cannot see (`code-runtime-worker-thread/src/index.ts:24-49`).

15. **Functional probes over presence checks, failing closed.** `sandbox-local` actually runs
    `bwrap … -- true` and `sandbox-exec -p <real profile> -- true` before believing a backend exists;
    a missing binary probes `unusable` and the consumer falls closed
    (`sandbox-local/src/index.ts:67-92`). Aleph's capability probes already exercise a read path —
    keep that, and add the `full`/`partial` **reported enforcement** distinction so a partial
    guarantee is never reported as a full one.

16. **Validation only at seven named boundaries** (`AGENTS.md:115`). Write the list down for Aleph
    (HTTP/wire, DB/durable, model-tool JSON, config, subprocess, sandbox, queue) and let everything
    between them be a plain typed call.

17. **Opposite failure postures by phase.** Loud/aggregate at activation; warn-and-isolate in steady
    state (`docs/subsystems/client-modules.md:44`).

18. **Boot manifest as ordered patch layers addressed by row id**, with `--dump-config` printing the
    exact resolved tree. Aleph's `aleph.toml` should gain a dump that a user can diff and patch.

---

## Worth avoiding

1. **Don't drop the protected-core / removal-refusal.** dsh has none; its only story is parking. Aleph
   already has the better mechanism — keep it, and add parking as the *graceful* behaviour for
   non-protected capabilities.

2. **Don't hand-maintain a model-facing API catalog.** `tool-cordis/src/api-catalog.ts` is 4,761 lines
   describing other packages' services to the model. Generate it from the same declarations the
   runtime binds, the way Aleph generates the A2UI catalog.

3. **Don't call a `vm`/worker/realm a security boundary.** dsh is admirably honest that it is not
   (`cordis-host-runner/README.md`, `code-runtime.md:161`) — but the *design* consequence is that
   agent-authored plugin mounting requires a whole session to be treated as shell access. If Aleph
   wants agent-authored plugins to be *safer* than bash rather than *equivalent* to it, the boundary
   has to be a process/container with a capability-mediated syscall surface, not a `vm` façade.
   The façade is a great *ergonomics and accident* gate; it is not containment.

4. **Don't require every plugin to be an npm package.** dsh's ~200-package workspace buys real
   independence but costs a `package.json`, `tsconfig.json`, `tsdown.config.ts`, `README.md` +
   `README.zh.md` + `README.i18n.yaml`, and an `invariant.ts` per plugin — before a line of behaviour.
   For Aleph, a plugin should be able to be *one file* with a manifest block, with promotion to a
   package only when it needs its own dependency closure. (dsh's own dynamic packages prove the point:
   they are literally a string of JavaScript.)

5. **Don't let a run "succeed" when only half of it landed.** dsh names this itself as a known
   limitation: *"A successful run does not mean the UI rendered. `run` returns once the answering page
   has LOADED the browser half; React renders afterwards."* They mitigate with a separate
   `reportRenderFailure` channel that carries no settle authority. Better: make the receipt name the
   stage it actually reached.

6. **Don't suspend forever with no timeout on a human gate.** A `cordis_run` for a browser-half
   package in a headless deployment waits until the asking turn is cancelled — *"unattended automation
   cannot use packages with a browser half."* An honest miss with a deadline beats an honest hang.

7. **Don't split one rule across two files and call it duplication-exempt.** `guard.ts` carries
   `jscpd:ignore` blocks duplicating the context façade and the intrinsic checks between host and
   browser halves, with a long comment justifying it. The justification is sound (each half must test
   against its own realm's `Context`), but it is a smell worth designing around rather than annotating.

8. **Don't maintain design rationale bilingually by hand.** Every doc exists as `.md`, `.zh.md`, and
   `.i18n.yaml`. Whatever the organizational reason, for Aleph it would triple the surface that can
   drift from code.

---

## License and copying

- **deepseek-harness itself: MIT** (`LICENSE`). Ideas and patterns are free to reimplement; a verbatim
  port needs the copyright notice retained (Aleph's `NOTICE` convention covers this).
- **`vendor/*` is not dsh's code.** It is vendored, rescoped Cordis (`@deepseek-ai/cordis`,
  `cordis-plugin-loader`, `-include`, `-group`, `-hmr`, `-timer`, `cosmokit`, `schemastery`), pinned
  by upstream SHA in `vendor/README.md` and marked `private: true`. Copying from `vendor/` copies
  *upstream Cordis*, under its own license and authorship — do not treat it as MIT-DeepSeek. Aleph
  should reimplement the concepts (fibers, inject-driven activation, effect disposal) as it already
  is, and cite the paper rather than the vendored source.
- **`native/landlock-run` has its own `LICENSE`** and ships as prebuilt per-platform npm packages;
  check it separately before borrowing any of the ~300 lines of C11.
- **`THIRD_PARTY_NOTICES.md` is 16KB** — dsh depends on `node-pty` (patched), `@earendil-works/pi-ai`,
  `koffi`, `esbuild`, and others with their own terms.
- Aleph's standing constraint already forbids adding `@deepseek-ai/*` as a runtime dependency. Nothing
  here changes that: everything in "Worth stealing" is a *pattern*, reimplementable in ~50–300 lines
  each, and none of it requires copying dsh source.
