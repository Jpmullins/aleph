# cordis — review for Aleph's kernel

**Reviewed:** `/Users/jpmullins/Documents/code/inspiration/cordis`, at `8cc9e33`, package
`cordis@4.0.0-rc.8` ("Meta-Framework of Spatiotemporal Composability").
**License:** MIT, © 2021-present Shigma (`LICENSE:1-3`). Safe to reimplement. Do not vendor.
**Size:** 4,015 lines of runtime source across 8 packages + 4,340 lines of tests. `packages/core/src`
alone is 1,848 lines and is the entire model; everything else is a plugin written against it.
**Self-description:** `packages/core/README.md:5-9` — "A Meta-Framework of Spatiotemporal
Composability", pointing at the paper `cordiverse/paper` and at deepseek-harness's docs.

---

## In one paragraph

cordis is a small framework whose only structural idea is a **context object**. A plugin is a plain
function that receives a context and does things with it. Anything the plugin *does* — register an
event listener, open a socket, start a timer, publish a service — it does by handing the context a
"do this, and here is how to undo it" pair, and the context remembers the undo. That is the whole
disposal story: unloading a plugin means running its undos backwards. Plugins talk to each other by
**services**: one plugin writes `ctx.provide('db', pool)`, and any other plugin that declared
`inject: ['db']` can then just write `ctx.db.query(...)` — an ordinary in-process method call, no
message bus, no serialization. The clever part is what happens when `db` goes away: cordis notices,
tears down every plugin that declared it, and rebuilds them automatically when a `db` comes back.
Those two things — undo-tracked effects, and dependents that rebuild themselves when a dependency
changes — are what the paper calls *temporal* and *reactive* composability, and cordis implements
both in about 500 lines (`packages/core/src/fiber.ts`). What it does **not** have is any notion of
trust, sandboxing, agents, tools, or a refusal to remove something load-bearing. It is a very good
composition engine with no opinion about who is composing.

Jargon used below, defined once:
- **fiber** — cordis's word for one *live instance* of a plugin. Loading the same plugin twice under
  two configs gives two fibers sharing one "runtime".
- **service** — a named object published on the context (`db`, `logger`, `timer`).
- **inject** — a plugin's declaration of which service names it needs.
- **effect** — a side effect paired with its undo function.
- **disposer / inverse** — the undo function.
- **epoch** — cordis's identity token for "the exact set of dependency instances I was built
  against"; when it changes, the plugin is rebuilt.
- **realm / isolate** — a way to give one subtree of plugins its own private binding of a service
  name (cordis's answer to multi-tenancy).

---

## 1. The extension model

### What a plugin *is*

A plugin is **a function, a class, or an object with an `apply` method** — that is the entire
contract (`packages/core/src/registry.ts:63-100`):

```ts
export type Plugin<T = any> =
  | Plugin.Function<T>      // (ctx: Context, config: T) => any
  | Plugin.Constructor<T>   // new (ctx: Context, config: T) => any
  | Plugin.Object<T>        // { apply(ctx: Context, config: T): any }
```

with optional static metadata on the same value (`registry.ts:69-75`):

```ts
export interface Base<T = any> {
  name?: string
  Config?: StandardSchemaV1<any, T>   // a Standard Schema validator
  inject?: Inject                     // string[] | Dict
  provide?: string | string[]
  intercept?: Dict<boolean>
}
```

There is no manifest format at the plugin level, no `activate()` export, no class to subclass, no
registration call. A file that default-exports a function is a plugin.

### Smallest complete real plugin in the tree

`packages/hmr/tests/plugin.ts:1-12` (a test fixture, but it is a real loaded plugin):

```ts
import { Context } from 'cordis'

export const name = 'test-plugin'
export let value = 'initial'

export function apply(ctx: Context) {
  ctx.on('hmr-test/get-value', () => value)
  ctx.effect(() => () => {
    ctx.root.emit('hmr-test/disposed')
  })
}
```

Nine lines. `ctx.on(...)` registers a listener whose removal is recorded automatically
(`events.ts:128-134` wraps it in `ctx.fiber.effect`). `ctx.effect(setup)` is the general form: the
setup function returns a disposer.

A real *service*-providing plugin, `packages/timer/src/index.ts:11-52`:

```ts
export class TimerService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'timer')
    ctx.mixin('timer', ['timeout', 'interval', 'throttle', 'debounce', ...])
  }

  timeout(callback: () => void, delay: number) {
    const dispose = this.ctx.effect(() => {
      const timer = setTimeout(() => { dispose(); callback() }, delay)
      return () => clearTimeout(timer)      // <-- the undo, next to the do
    }, 'ctx.timeout()')
    return dispose
  }
}
export default TimerService
```

Two things worth noting. First, `extends Service` does exactly one interesting thing — its
constructor calls `self.ctx.reflect.provide(name, self, this[symbols.check])`
(`packages/core/src/service.ts:33`), i.e. publishing is a *constructor side effect*, so "the class
exists" and "the service is published" are the same event. Second, `ctx.mixin('timer', [...])`
(`reflect.ts:239-265`) grafts the service's methods onto the *context type itself*, so downstream
plugins write `ctx.timeout(...)` rather than `ctx.timer.timeout(...)`. **A plugin can extend the
shared vocabulary of every other plugin.** That is the structural reason cordis does not have
VSCode's fixed-extension-point problem.

### How one is declared, registered, discovered, started

- **Declared:** as above — a function/class/object with optional `inject`/`Config` statics.
- **Registered:** `ctx.plugin(plugin, config)` → `RegistryService.plugin`
  (`registry.ts:193-213`). It looks up (or creates) a `Plugin.Runtime` keyed by the resolved
  callback function in a `Map<Function, Plugin.Runtime>` (`registry.ts:127`), then constructs a
  `Fiber`. Two loads of the same module share one runtime and get two fibers.
- **Discovered:** by the optional loader plugin. `packages/core/bin.js:1-16` is the whole CLI:

  ```js
  const ctx = new Context()
  ctx.baseUrl = pathToFileURL(process.cwd()).href + '/'
  await ctx.plugin(Loader)
  await ctx.loader.create({ name: '@cordisjs/plugin-include', config: { path: './cordis.yml' } })
  ```

  and `cordis.yml` is a flat list of entries (`packages/hmr/tests/cordis.yml:1-10`):

  ```yaml
  - id: timer
    name: '@cordisjs/plugin-timer'
  - id: hmr
    name: '@cordisjs/plugin-hmr'
    config: { root: ['.'], debounce: 100 }
  - id: test
    name: ./plugin
  ```

  `name` is fed straight to `import()` (`loader/src/config/tree.ts:103-120`), so it is either an npm
  specifier or a relative path. There is no allow-list, no signature, no scan-a-directory fallback
  — but also no restriction beyond "resolvable by Node".
- **Started:** `Entry._init` (`loader/src/config/entry.ts:158-172`) imports, unwraps the default
  export, then calls `ctx.registry.plugin(plugin, config, getOuterStack)`.

**Everything is a plugin, including the loader.** `Loader` is itself mounted with `ctx.plugin(Loader)`;
`Include` (the YAML file reader) is an entry inside the loader; groups, isolation, HMR are all
plugins. The core has no built-in knowledge of files, YAML, or hot reload.

---

## 2. Dependencies between plugins

**Peer-to-peer service dependencies, not host extension points.** This is the model Aleph wants.

Plugin B depends on plugin A by *naming a service*, never by naming A:

```ts
class Hmr extends Service { ... }          // packages/hmr/src/index.ts:49-51
@Inject('loader')
@Inject('timer')
```
or, equivalently, `static inject = ['loader']` (`packages/include/src/index.ts:49`), or
`{ inject: ['foo'], apply(ctx) {...} }` (`core/tests/isolate.spec.ts:11-18`).

`Inject.resolve` (`registry.ts:42-61`) normalizes all three spellings — array, dict,
prototype-chained dict from the `@Inject` decorator — into `Dict<name, config>`. The dict form
carries *per-dependency configuration* (an "intercept"), which is stitched onto the fiber's context
at construction (`fiber.ts:137-144`).

Resolution has two halves:

1. **Availability tracking.** `Fiber._checkImpl(name)` (`fiber.ts:371-383`) asks
   `reflect._getImpl(name, strict=true)` for the current implementation, runs the service's optional
   `check()` predicate, and caches the `Impl` in the fiber's private `_store`. `Impl` records
   `{ name, fiber, value, check }` (`reflect.ts:54-59`) — note it records the *providing fiber*, not
   just the value.
2. **Epoch computation.** `Fiber._refresh()` (`fiber.ts:385-397`):

   ```ts
   let epoch = ''
   for (const name of Object.keys(this.inject)) {
     const impl = this._store[name]
     if (!impl) { epoch = INACTIVE; break }
     epoch += ':' + impl.fiber.uid
   }
   this._setEpoch(epoch)
   ```

   The epoch is a string naming the *exact instance identities* of every dependency. If any
   dependency is missing the epoch is `'__INACTIVE__'`. If a dependency is replaced by a different
   instance, its `uid` changes, so the epoch changes even though the name is still available.

Circularity: cordis does **not** detect cycles. Two plugins that inject each other simply never
activate — each waits for the other's epoch to become resolvable. There is no diagnostic. The paper
notes a runtime *could* report this from the declarations; cordis does not (this is exactly what
Aleph's `support.py` docstring claims, and it checks out).

`provide` in the plugin metadata (`registry.ts:73`) exists in the type but is **never read as a
graph declaration**. Its only consumer is `service.ts:19` — `name ??= this.constructor['provide']`,
a fallback for the *service's own name*. Provision is otherwise discovered dynamically, when
`ctx.provide()` actually runs (`reflect.ts:175-203`). So cordis knows a plugin's *requires*
statically and its *provides* only at runtime. **That asymmetry is precisely why it cannot compute a
blast radius** (see §5), and why Aleph's decision to declare `provides` up front
(`spec.py:89`) is the right one — as long as Aleph also checks the declaration is honoured.

---

## 3. How plugins communicate — the performance question

### The mechanism

**Direct, synchronous, in-process property access and method calls on a shared object graph.** No
serialization, no message passing, no IPC, no workers. `ctx.db.query(x)` is a real method call on the
real object the provider constructed. Big payloads are never copied — plugins pass object references
because they share one heap.

There is *also* an event bus (`packages/core/src/events.ts`) with five dispatch modes — `emit`,
`parallel`, `serial`, `bail`, `waterfall` (`events.ts:89-126`) — but it is used for **broadcast and
interception**, not for RPC. Internal control-plane events (`internal/plugin`, `internal/status`,
`internal/service`, `internal/get`, `internal/set`, `internal/update`) let plugins hook the kernel's
own decisions; the loader uses `internal/update` to persist a plugin's config change back to
`cordis.yml` (`loader/src/index.ts:74-86`).

### Is a reference cached?

**No, and this is the design's expensive corner.** Every read of `ctx.someService` runs the Proxy
handler at `packages/core/src/reflect.ts:62-98` and, on success, returns
`getTraceable(ctx, impl.value)` — which allocates a **fresh `Proxy`** every time
(`utils.ts:110-118` → `utils.ts:157-212`). Reading a *method* off that proxy allocates a **second**
`Proxy` (`createShadowMethod`, `utils.ts:148-155`). There is no memo table anywhere.

Worse, `reflect.ts:71` allocates an `Error` **before** it knows whether the access will fail:

```ts
if (Reflect.has(target, prop)) {
  return getTraceable(ctx, Reflect.get(target, prop, ctx))
}
const error = new Error(`cannot get property "${prop}" without inject`)   // <-- line 71, unconditional
```

Services live in `reflect.store`, not as own properties of the context object, so `Reflect.has` is
false for every service — the `new Error` is on the **hot path of every single cross-plugin access**.
The same is true for the mixed-in kernel methods: `ctx.on`, `ctx.emit`, `ctx.effect`, `ctx.plugin`
are all `accessor` props registered by `reflect.ts:144-147`, so they take the same branch. In V8 a
bare `new Error()` captures a structured stack trace eagerly.

The same pattern recurs at composition time on purpose: `buildOuterStack()` (`utils.ts:275-278`)
allocates an `Error` to snapshot the caller's stack and is the *default argument* of
`registry.plugin()` (`registry.ts:193`) and of every `fiber.effect()` (`fiber.ts:307`);
`composeError` (`utils.ts:260-273`) allocates another per invocation. This buys genuinely excellent
diagnostics — a stack trace that crosses async plugin boundaries and names the `cordis.yml` entry
(`entry.ts:136-144`) — at a real cost.

### Measured

No benchmarks exist in the repo. `cordis` has no `node_modules` here, so I built a structurally
faithful reproduction of the access path (Node v22.23.2, Apple silicon) — the code models
`reflect.ts:62-98` → `events.waterfall` → fiber-store walk → `createTraceable` →
`createShadowMethod`. Absolute numbers are machine-specific; the ratios are the point.

| operation | ns/op |
|---|---|
| direct `obj.query(1)` | 0.9 |
| `Proxy` get trap only | 13.4 |
| `new Proxy(fn, {apply})` alloc + call | 34.6 |
| `getPropertyDescriptor` prototype walk | 21.0 |
| **`new Error(msg)` (default `stackTraceLimit = 10`)** | **821.8** |
| same, `Error.stackTraceLimit = 0` | 122.7 |
| same, from 5 frames deep | 1374.6 |
| `buildOuterStack()` | 858.3 |
| **full modelled `ctx.db.query(1)`** | **~1,427** |

So a cross-plugin call costs on the order of **1–1.5 µs**, of which ~60-90% is the unconditional
stack capture and the rest is proxy allocation. Hoisting the service into a local (`const db =
ctx.db` once, then call in a loop) removes the `Error` but *not* the per-method proxy allocation:
that path still measured ~190 ns/call.

For calibration, Aleph's current kernel, measured against the real code
(`packages/aleph-kernel/src/aleph_kernel/context.py:89-114`, CPython 3.13):

| operation | ns/op |
|---|---|
| direct `db.query(1)` | 26.3 |
| `ctx.db.query(1)` | 138.0 (5.2× direct) |
| `ctx.get("db")` | 87.0 |
| hoisted `h.query(1)` | 26.6 (0 overhead) |

Aleph's resolution is *ten times cheaper than cordis's in absolute terms* and — crucially —
**hoisting eliminates it entirely**, because `Context.get` returns the raw object rather than a
fresh proxy. That is a real advantage Aleph already has and should not give away.

### Where a hot loop pays the tax, and whether the authors avoided it

They mostly avoided it by **convention rather than by mechanism**: plugin code resolves services in
`apply()` / the constructor and stores them (`this.ctx`, `this.internal`, `this.loader`), so the
crossing happens at wiring time. `Hmr` caches `this.internal = this.ctx.loader.internal`
(`hmr/src/index.ts:83`) rather than re-reading it. But `Service` subclasses that write
`this.ctx.counter.increase()` per call (as in `core/tests/service.spec.ts:44-50`) pay it on every
call, and nothing in the framework warns you.

The one explicitly performance-motivated change in the history is
`29581f6 perf(core): avoid binding callbacks in event dispatch (#38)`: `EventsService.dispatch` used
to `.map(hook => hook.callback.bind(thisArg))`, allocating a bound function per listener per emit;
it now returns `[thisArg, callbacks]` and uses `Reflect.apply` (`events.ts:72-126`). The same commit
added the `&& this._hooks['internal/dispatch']?.length` guard so the tracing broadcast is skipped
when nobody is listening. Both are exactly the right instincts. There is one other perf comment, in
the logger ring buffer (`logger.ts:193`).

**The honest reading:** cordis is optimized for *composition-time clarity*, not *call-time
throughput*. It assumes plugin boundaries are crossed while wiring, not inside inner loops. For a
chatbot framework (its origin: Koishi) that is correct. For Aleph — where a retrieval hot loop may
cross the ingest/embedding/store boundary per chunk — it is the wrong default, and the specific
mistake to not copy is `reflect.ts:71`.

---

## 4. Lifecycle

Six states (`fiber.ts:78-85`): `PENDING → LOADING → ACTIVE → UNLOADING`, plus `FAILED` and
`DISPOSED`. `PENDING` means "declared dependencies are not all available".

### The reactive loop, which is the heart of the design

1. A provider appears or disappears. `ReflectService.provide` (`reflect.ts:175-203`) writes the
   `Impl` into the symbol-keyed store and calls `notify([name])`; its *disposer* deletes it and calls
   `notify([name])` again.
2. `notify` (`reflect.ts:205-227`) walks **every fiber of every runtime**, and for each fiber that
   injects an affected name calls `_checkImpl` then `_refresh`.
3. `_refresh` recomputes the epoch (§2) and calls `_setEpoch`.
4. `_setEpoch` (`fiber.ts:399-413`) is the state machine:

   ```ts
   private _setEpoch(epoch: string) {
     const oldEpoch = this._runner.epoch
     if (epoch === oldEpoch) return          // nothing actually changed — no work
     this._runner.epoch = epoch
     if (this.inertia) return                // a transition is already in flight
     this._updateState(() => {
       if (epoch !== INACTIVE && oldEpoch === INACTIVE) {
         this.inertia = this._reload();  return FiberState.LOADING
       } else {
         this.inertia = this._unload();  return FiberState.UNLOADING
       }
     })
   }
   ```

5. `_reload` / `_unload` (`fiber.ts:415-458`) each re-check the epoch when they finish, and chain
   the opposite transition if it moved while they were running.

This **inertia lock** is the answer to "what happens to in-flight work during a swap": transitions
for a given fiber are strictly serialized and never interleaved. A dependency that vanishes during a
load does not abort the load — the load completes, then the unload runs. Pinned by
`core/tests/fiber.spec.ts:7-63` ("inertia lock 1/2/3"), which drives fake timers through
load-then-dispose-then-reprovide and asserts every intermediate state.

The epoch-as-string trick also gives **free change coalescing**: if a provider is removed and an
identical-uid provider re-added within one tick, the epoch is unchanged and no work is done
(`fiber.ts:401`). And "inertia lock 2" (`fiber.spec.ts:27-41`) shows a *different* provider arriving
mid-load resolving to `ACTIVE` without a spurious teardown.

### Teardown

`Fiber._unload` (`fiber.ts:437-458`):

```ts
await Promise.all(this._disposables.clear().map(async (dispose) => {
  try { await composeError(async (info) => { ...; await dispose() }, this._runner.getOuterStack) }
  catch (reason) { this.ctx.logger.error(reason) }
}))
```

`DisposableList.clear()` (`utils.ts:26-30`) returns the disposers **reversed**, so LIFO. Each one is
individually try/caught — **one failing disposer does not strand the rest**
(`core/tests/fiber.spec.ts:87-104` pins it). Note the disposers run concurrently via
`Promise.all` on an already-reversed list; ordering is by start, not completion.

Within a single `ctx.effect(...)`, disposal is strictly sequential and reversed
(`fiber.ts:281-294`), which `core/tests/dispose.spec.ts:37-74` pins as `[3, 2, 1]`.

### Effects, and why the generator form matters

`ctx.effect(execute, label)` (`fiber.ts:275-340`) accepts four shapes (`fiber.ts:54-64`): a function
returning a disposer, a `Promise` of one, a **generator yielding several**, or an **async generator
yielding several**. The generator form is the good one:

```ts
root.effect(function* () {
  yield dispose1
  yield root.on('custom-event', () => {})
  yield dispose2
})
```

Do and undo sit on adjacent lines, and if the generator throws at step 3 the disposers from steps 1-2
still run (`dispose.spec.ts:197-208`, asserting `seq === [1]`). This is precisely Aleph's
`EffectScope.drive` contract (`packages/aleph-kernel/src/aleph_kernel/effects.py:70-95`) — Aleph
ported it correctly, and Aleph's version is *better*: cordis checks its abort guard only on the
**async**-iterator branch (`fiber.ts:263`, `if (runner.epoch !== oldEpoch) return`) and loops
`while (true)` with no guard on the sync branch (`fiber.ts:248-255`). Aleph normalized to one guarded
path. That improvement is real and the Aleph docstring's claim about it is accurate.

`ctx.effect` returns a disposer that is *also* a thenable resolving to a disposer
(`fiber.ts:322-337`) — `dispose()` cancels now, `await dispose` waits for setup to finish and *then*
gives you a disposer. Clever, and `dispose.spec.ts:165-183` shows why it exists, but it is the kind
of API that costs a reader ten minutes.

### Hot reload

`packages/hmr/src/index.ts` — 405 lines, and the only file in the repo that is genuinely hairy.
Mechanism:

1. `chokidar` watches the source tree (`hmr:109-113`).
2. Changed file classified against **externals** (the dependency closure of the CLI entry point,
   computed once at startup from Node's internal module graph, `hmr:117-123`). A change there means
   `loader.exit()` — full process restart, no HMR (`hmr:133`).
3. Otherwise `analyzeChanges` (`hmr:174-227`) does a fixed-point propagation over the module graph
   to split files into `accepted` (reload) and `declined` (don't), reading real ESM link edges via
   `job.linked`.
4. Plugin **entry files are the atomic reload unit** (`hmr:236-272`): for each loaded entry, if any
   file in its dependency closure is accepted, the whole plugin reloads.
5. Both Node's ESM `loadCache` and the CJS `require.cache` are **backed up** and cleared
   (`hmr:290-309`), then entry modules are re-imported. Any import failure → `rollback()` restores
   both caches and returns (`hmr:311-329`).
6. The swap (`hmr:331-374`): `registry.delete(plugin)` disposes every fiber of the old runtime, then
   for each old fiber `oldFiber.parent.registry.plugin(newPlugin, oldFiber.config, ...)` recreates it
   **at the same tree position with the same config**, and re-links `fiber.entry`. If anything
   throws, the catch block restores the old caches *and re-registers the old plugin objects*.

So: **hot reload exists, is transactional-ish, and rolls back on failure.** It is well tested — 800
lines in `packages/hmr/tests/index.spec.ts` covering reload, revert, dependency-chain reload,
syntax-error rollback and recovery, debounce batching, service-providing plugins, entry
re-association, rapid successive reloads, and event-handler add/remove.

The caveats are real, though: it reaches directly into Node private internals
(`loader/src/internal.ts:96-122` calls `requireBuiltin('internal/modules/esm/loader')` and
version-sniffs Node 22 vs 24 into two incompatible `ModuleLoader` interfaces), the rollback is
best-effort rather than atomic (the catch block can itself throw and only logs), and in-flight
*work* is not drained — only in-flight *transitions* are serialized by the inertia lock. A request
executing inside the old plugin's code keeps running against a torn-down context.

---

## 5. Failure and blast radius

**Throw at load.** Caught. `Fiber._reload` (`fiber.ts:415-435`) catches, logs, sets `_error`, forces
the epoch to `INACTIVE` → state `FAILED`. `core/tests/fiber.spec.ts:65-85` loads the *same plugin
twice* with different configs, one throwing: `fiber1` is `FAILED`, `fiber2` is `ACTIVE`, and the
listener the failing instance registered before throwing still fires once. So **one bad plugin
instance does not take down its siblings, and does not take down the process.**

**Throw at runtime.** Not contained. A plugin's exported method throwing propagates to whoever called
it. `ctx.parallel` aggregates listener failures into an `AggregateError` instead of short-circuiting
(`events.ts:89-94`, pinned by `events.spec.ts:56-72`) — but `ctx.emit` does not
(`events.ts:96-99`): the first throwing listener aborts the rest of the emit. There is no supervisor,
no restart policy, no circuit breaker.

**Isolation.** None beyond the context. Same process, same heap, same event loop. A plugin can
`process.exit()`, monkey-patch a prototype, or hold the loop.

**Retry.** None automatic. A `FAILED` fiber sits there until `update()` or `restart()`
(`fiber.ts:468-485`) — but note `update()` clears `_error` and restarts, so editing a plugin's config
in `cordis.yml` is the retry mechanism.

**Can it refuse to remove something load-bearing?** **No.** There is no protected set, no support-set
computation, no `blast_radius`. `registry.delete(plugin)` (`registry.ts:162-171`) unconditionally
disposes every fiber. What happens instead is *graceful cascade*: dependents notice via `notify`,
transition to `PENDING`, tear down their own effects, and rebuild if the dependency returns. That is
a genuinely good failure mode — nothing is left holding a dead reference — but it is not a guardrail,
and there is no way to ask "what would break if I removed this" before doing it.

`ReflectService.provide`'s disposer does one nice thing here (`reflect.ts:195-201`):

```ts
return async () => {
  delete this.store[key]
  const fibers = this.notify([name])
  await Promise.allSettled(fibers.map(fiber => fiber.await()))
  // ensure self access before dependencies cleanup
  delete this.ctx.fiber.store![name]
}
```

The provider removes itself from the *global* store first, waits for every dependent to finish
tearing down, and only then drops its *own* handle — so a dependent's disposer can still reach the
service it is releasing. That ordering detail is easy to get wrong and cordis got it right.

---

## 6. Trust and agent-authored code

**There is none. cordis has no trust model at all.**

- No sandbox, no permission system, no code inspection, no signature checking.
- `import(name)` on whatever string is in the config file (`loader/src/config/tree.ts:103-120`).
- Worse, config files are **executable**. `loader/src/config/utils.ts:4-8`:

  ```ts
  export const evaluate = new Function('ctx', 'expr', `
    with (ctx) {
      return eval(expr)
    }
  `) as ((ctx: object, expr: string) => any)
  ```

  and `packages/include/src/index.ts:8-16` registers a YAML tag `!!js` producing `{__jsExpr}` nodes,
  which `Entry._resolveConfig` (`entry.ts:79-82`) passes to `interpolate` → `evaluate`. So a
  `cordis.yml` value `!!js ctx.loader.exit()` is arbitrary code with the context in scope. This is a
  deliberate feature for config-time interpolation and a total trust hole.
- `include`'s `patches` mechanism (`include/src/index.ts:101-164`) lets an outer config override
  fields of an inner one by id, with a name-mismatch guard — a nice pattern for layered config, and
  the closest thing here to a policy layer, but it governs *configuration*, not *capability*.

**Aleph's AST gate, spawn ledger, probation and protected-core set have no counterpart in cordis.**
Those are Aleph inventions. Whether they work is a separate question (§9), but they are not
superficial ports of something cordis has.

---

## 7. State and context

**A context object obtained by prototype-chained derivation, mediated by a `Proxy`.**

`new Context()` (`context.ts:36-49`) builds the root and immediately wraps itself:
`const self = new Proxy(this, ReflectService.handler); ... return self`. Every context anyone ever
holds is that proxy or a descendant of it.

Derivation (`context.ts:55-63`):

```ts
extend(meta = {}): this {
  const self = Object.create(getTraceable(this, this))
  for (const prop of Reflect.ownKeys(meta)) {
    Object.defineProperty(self, prop, Reflect.getOwnPropertyDescriptor(meta, prop)!)
  }
  ...
}
```

`Object.create` — a child context is a **prototype-chained object**, which is why forking is nearly
free (§8) and why lookups fall through to the parent automatically.

Two derivations matter:
- `ctx.isolate(name, label?)` (`context.ts:65-69`) shadows the `[symbols.isolate]` map so that `name`
  resolves to a different `Symbol`. Since `reflect.store` is keyed by that symbol
  (`reflect.ts:184-190`), two isolated subtrees see genuinely independent bindings of the same name.
  `core/tests/isolate.spec.ts:7-56` walks four provide/dispose combinations across three realms.
  Passing an explicit `label` symbol lets two subtrees *share* an isolated realm
  (`isolate.spec.ts:58-102`).
- `ctx.intercept(name, config)` (`context.ts:71-77`) shadows a per-service config map, resolved by
  walking the prototype chain and merging outward-in (`service.ts:51-67`). This is how you configure
  *someone else's* service for your subtree without touching them — e.g. `logger` level per subtree
  (`logger.ts:214-224`).

The loader turns isolation into config (`loader/src/config/isolate.ts:67-149`): an entry may declare
`isolate: { db: true }` (private realm) or `isolate: { db: 'shared-name' }` (named global realm),
with realm garbage-collection when the last referencing entry goes away
(`isolate.ts:151-168`). `packages/loader/tests/isolate.spec.ts` is 538 lines of edge cases — nested
realms, changing the provider, changing the injector, transferring entries in and out of groups. This
subsystem is small in code and enormous in behavioural surface.

### Is access scoped, or is everything reachable?

**Partially scoped, and weaker than Aleph's.** The Proxy `get` handler
(`reflect.ts:80-94`) walks the *fiber chain*:

```ts
let fiber = (ctx[symbols.shadow] as Context ?? ctx).fiber
while (true) {
  const impl = fiber.store?.[prop]
  if (impl) return getTraceable(ctx, impl.value)
  if (prop in fiber.inject) { error.message = `... in inactive context`; throw error }
  if (!fiber.runtime) throw error
  if (fiber.parent[symbols.isolate][prop] !== key) throw error
  fiber = fiber.parent.fiber
}
```

`fiber.store` holds (a) the fiber's own provisions and (b) exactly the names it declared in `inject`
(`fiber.ts:416`, `reflect.ts:191`). So:

- A plugin that **declared** `inject: ['db']` sees `db` wherever in the tree it is provided.
- A plugin that **did not declare it** still sees it if any *ancestor* fiber provides or injects it —
  including the root, whose store holds every root-level provision.
- A plugin sees nothing provided in a *sibling* subtree it did not declare.

So `inject` in cordis is primarily a **reactivity declaration** ("restart me when this changes"), and
only secondarily an access declaration. Aleph's `Context.get`
(`packages/aleph-kernel/src/aleph_kernel/context.py:101-114`) refuses any key outside `requires`
absolutely, with no ancestor fallthrough. **Aleph's is the stronger boundary and should stay.**

### The shadow/traceable machinery

`getTraceable` / `createTraceable` / `createShadow` / `createShadowMethod` / `withProps`
(`utils.ts:110-217`) exist to answer one question: *when plugin B calls a method on plugin A's
service, and that method internally does `this.ctx.effect(...)`, whose fiber owns the effect?* The
answer cordis wants is **the caller's**, so that when B unloads, the effect A created on B's behalf
goes away. `Counter` in `core/tests/utils.ts:48-64` is the canonical demo, and
`core/tests/service.spec.ts:36-74` asserts the counter increments survive/vanish correctly across
disposal.

This is a genuinely important property and it is the single subtlest thing in the codebase. It is
also where the bugs are: of the last 10 commits, four touch it — `8abd903 fix(core): track direct
service callers`, `be7d36e fix(core): apply shadows to callable services`, `752dbee fix(core): keep
wrapped fiber state canonical`, plus a dedicated test file `core/tests/shadow.spec.ts` whose whole
job is pinning caller-vs-shadow identity.

---

## 8. Concurrency model

**Single-threaded, async, cooperative.** Node's event loop; no threads, no worker_threads, no
processes, no actors. Nothing prevents two plugins mutating shared state — the only reason it works
is that JS has no preemption, so a synchronous block is atomic.

What cordis *does* control is **transition concurrency**:
- Per fiber, the `inertia` promise serializes load/unload (`fiber.ts:399-413`, §4).
- `Fiber.await()` (`fiber.ts:460-466`) loops `while (this.inertia) await this.inertia` — waiting for
  quiescence, not for a single promise, because a transition can chain another.
- `EntryTree.await()` (`tree.ts:39-45`) does the same across all entries: repeatedly gather all
  in-flight tasks and settle them until none remain.
- `Loader[Service.check]` (`loader/src/index.ts:133-137`) can make the `loader` service report itself
  *unavailable while any entry is still transitioning*, if a consumer intercepts with
  `{ await: true }`. That is a neat use of the service `check` hook: a dependent that must not see a
  half-loaded tree simply doesn't see the service.

Group updates fan out with `Promise.all` (`group.ts:55-63`), and disposal fans out with
`Promise.all` (`fiber.ts:438`), so *independent* plugins do load and unload in parallel.

Comparison: Aleph serializes **all** composition changes under one `asyncio.Lock`
(`packages/aleph-kernel/src/aleph_kernel/kernel.py:79`, `225`, `239`, ...). Simpler and provably free
of interleaving; also strictly less concurrent, and it means a slow plugin teardown blocks every
other composition change process-wide. cordis's per-fiber inertia is the finer-grained design.

---

## 9. What an agent / tool is

**Nothing. cordis has no concept of an agent, a tool, an LLM, or a model.** It is a general
application framework (originating as the core of the Koishi chatbot framework) that deepseek-harness
builds an agent system *on top of*. There is no tool registry, no schema-to-model exposure, no
prompt, no token.

The only thing in the neighbourhood is the config schema: plugins may declare
`Config?: StandardSchemaV1` (`registry.ts:72`) and cordis validates against it via the vendor-neutral
Standard Schema protocol (`fiber.ts:34-46`), while the shipped plugins use `schemastery`, which
carries UI role hints and i18n (`hmr/src/index.ts:389-402`: `z.natural().role('ms')`, `.i18n({...})`).
So a plugin's config is machine-describable and renderable — which is exactly the substrate you would
need to expose a plugin's configuration surface to a model. cordis just never takes that step.

**Implication for Aleph:** there is nothing to steal here for the agent/tool layer. Aleph's
`AgentPluginAPI` shape (`install / enable / disable / uninstall / inspect`) is its own invention and
has no cordis referent.

---

## 10. The single best idea, and the single worst

### Best: the epoch string + inertia lock (`fiber.ts:385-458`)

Sixty lines that make dependency changes *reactive* rather than *manual*, and do it correctly under
concurrent transitions. Three properties fall out of one design choice — encoding a fiber's
dependency state as a string of provider `uid`s:

1. **Identity, not just presence.** Replacing `db` with a different instance changes the epoch, so
   dependents rebuild against the new one. Presence-checking alone would miss this.
2. **Free coalescing.** `if (epoch === oldEpoch) return` — a remove/re-add of the same instance in
   one tick does zero work.
3. **Serialized transitions.** `if (this.inertia) return` plus the re-check at the end of
   `_reload`/`_unload` means a fiber can never be half-loaded and half-unloaded, no matter how the
   dependency graph thrashes during a slow setup.

Aleph explicitly deferred this (`kernel.py:1-9`: "The four-state fiber machine and reactive
re-resolution are deferred until something actually needs a provider swapped under a running
consumer"). That deferral was defensible — but Aleph's product thesis *is* "swap a provider under a
running consumer", so the day is coming.

### Worst: the shadow/traceable Proxy machinery (`utils.ts:110-217`, `reflect.ts:62-133`)

~200 lines of `Proxy` handlers, prototype splicing (`joinPrototype`, `utils.ts:88-95`), callable-object
synthesis (`createCallable`, `utils.ts:219-226`), and receiver rewriting that together implement
implicit caller attribution. It is:

- **The performance floor.** Every cross-plugin access allocates 2-3 proxies and, because of
  `reflect.ts:71`, an `Error` with a captured stack. ~1.4 µs measured (§3).
- **The bug surface.** Four of the last ten commits fix it.
- **Unteachable.** Nothing in the code says "a method you call on someone else's service creates
  effects on *your* fiber" — you discover it from `shadow.spec.ts`.

The underlying goal (an effect created on your behalf dies with you) is right. The mechanism —
implicit, reflective, invisible — is wrong. **Make it explicit instead**: pass the caller's scope as
an argument, or return a handle the caller must attach.

Runner-up worst: `evaluate = new Function('ctx','expr','with (ctx) { return eval(expr) }')`
(`loader/src/config/utils.ts:4-8`). A config file is arbitrary code.

---

## Comparison: what Aleph got right, wrong, and invented

### Faithful and correct

| cordis | Aleph | verdict |
|---|---|---|
| `ctx.effect(function*(){ yield undo })`, `fiber.ts:275-340` | `EffectScope.drive`, `effects.py:70-95` | **Faithful, and improved.** Aleph guards *every* step; cordis guards only the async branch (`fiber.ts:248-255` vs `:256-268`). Aleph's docstring claim about this is accurate — I verified it. |
| LIFO unwind, one failing disposer doesn't strand the rest (`fiber.ts:437-448`) | `EffectScope.unwind`, `effects.py:97-113` | Faithful. Aleph aggregates into an `ExceptionGroup`; cordis logs. Aleph's is better. |
| Effect registered at the point of the change | `capabilities.py` yields each inverse next to its setup | Faithful, and the real payoff: `packages/aleph-runtime/src/aleph_runtime/capabilities.py:1-20` documents two concrete production bugs this structurally eliminated. |
| `ctx.provide` registers its own removal (`reflect.ts:195-201`) | `Context.provide`, `context.py:126-139` | Faithful. |
| `ctx.isolate(name)` → realm indirection (`context.ts:65-69`, `reflect.ts:184-190`) | `Context.isolate` + `(realm, key)` store, `context.py:38-64`, `145-159` | Faithful in shape. Aleph's root-realm fallback (`context.py:59-60`) makes isolation additive; cordis's `Object.create` chain does the same thing through prototypes. |
| Declared dependencies (`inject`) | `CapabilitySpec.requires` | Faithful, and Aleph's is a *stronger* boundary (§7). |

### Where Aleph diverged, correctly

- **Access denial is absolute.** `Context.get` (`context.py:101-114`) refuses any undeclared key.
  cordis lets a plugin reach an ancestor's provisions without declaring them. Aleph's is right.
- **No proxy tax.** `ctx.db` returns the object, not a proxy. Measured 138 ns vs cordis's ~1,400 ns,
  and hoisting takes Aleph to zero overhead where it takes cordis to ~190 ns.
- **Mandatory probes.** `CapabilitySpec.probe` has no default and `__post_init__` rejects a
  non-callable one (`spec.py:88`, `102-107`); `_activate` unwinds on probe failure
  (`kernel.py:304-318`). cordis has a *voluntary* `[Service.check]` hook
  (`service.ts:33`, used only by `Loader`, `loader/src/index.ts:133-137`) and nothing gates
  activation on it. **This is Aleph's single biggest genuine improvement.** It directly targets the
  failure mode named all over `CLAUDE.md` — a component that loads successfully and reads nothing.
- **Support-set / blast radius.** `support.py` computes the greatest fixed point of "still supported
  after retiring X" as a *pure function*, so a removal can be refused before it happens
  (`kernel.py:404-434`). cordis has the declarations and never computes anything from them. This is
  the load-bearing invention.
- **Protected core is unnameable, not merely refused.** `PluginId` is minted only by
  `register_dynamic`; manifest-mounted capabilities never get one (`kernel.py:46-54`, `83-103`). A
  capability-as-unforgeable-token design, and much better than a policy check. cordis has no
  equivalent.
- **Quarantine instead of abort.** `_plan` (`kernel.py:191-214`) marks unresolvable plugins `FAILED`
  with a reason and proceeds. cordis's unresolvable plugin sits silently `PENDING` forever with no
  diagnostic. Aleph's `unactivatable()` read path (`kernel.py:159-172`) is the fix for exactly that.
- **`reprobe`** (`kernel.py:376-402`) — re-verify a live capability and retire it if it has decayed.
  No cordis analogue. Good idea.

### Where Aleph got it wrong, or thinner

1. **No reactivity. This is the big one.** Aleph's own docstring is candid (`kernel.py:1-9`), but the
   consequence is that Aleph implements *spatial* and *temporal* composability and calls the result
   an implementation of the paper, while the **reactive** third — a dependent noticing its provider
   changed and rebuilding itself — is absent. There is no `notify`, no epoch, no state machine.
   `Context.get` re-reads the store each time, so a *replaced value* is picked up lazily on next
   read, but a capability holding a cached handle never learns, and nothing re-runs `setup`. Aleph's
   `replace()` (`kernel.py:327-374`) tears down and re-activates *the one capability*; its dependents
   are neither torn down nor rebuilt, and are left holding whatever they cached from the old
   instance. **That is a real defect, not a simplification.** cordis's `_refresh`/`notify` pair is
   ~40 lines and is the fix.

2. **Global lock vs per-fiber inertia.** `asyncio.Lock` around every composition change
   (`kernel.py:79`) is simpler and safer, but it means a slow teardown blocks all composition
   process-wide, and it does not compose with the reactivity in (1) — when a provider swap must
   cascade to N dependents, you need per-dependent transition serialization, which is what `inertia`
   is.

3. **`provides` is static in Aleph, dynamic in cordis — and Aleph never checks the two agree.**
   `CapabilitySpec.provides` is a declaration; `Context.provide` is the act. Nothing asserts that a
   capability actually provided everything it declared. A capability that declares
   `provides={"db.engine"}` and forgets to call `ctx.provide` still passes `_activate` (unless its
   own probe catches it), and dependents then hit `InactiveAccess` at first use. cordis avoids this
   by not having a static declaration at all. A three-line post-setup assertion in `_activate` would
   close it.

4. **The agent-facing half is unreachable.** Confirmed by grep: nothing outside
   `packages/aleph-kernel` imports `AgentPluginAPI`, `register_dynamic`, `ast_gate`, `spawn_ledger`
   or the kernel's `skills` module. The only external importers are
   `apps/api/src/aleph_api/lifespan.py:32-33`, `apps/workers/src/aleph_workers/arq.py:10-11` and
   `packages/aleph-runtime/src/aleph_runtime/capabilities.py:33` — all boot-path. cordis by contrast
   ships the *whole* dynamic path in production: the loader, groups, isolation and HMR are how you
   run it, not an optional extra. **The lesson is not "build the agent API"; it is "make the dynamic
   path the way the system boots, so it cannot rot."** cordis's own core boots via
   `ctx.plugin(Loader)` (`bin.js:10`) — the dynamic mechanism *is* the boot mechanism.

5. **No hot-reload of code, only of spec objects.** `Kernel.replace` swaps a `CapabilitySpec`
   already constructed in-process. There is no module-cache invalidation, no re-import, no
   equivalent of `hmr/src/index.ts`. In Python that is `importlib.reload` plus a dependent-closure
   walk, and it is genuinely hard — but "the agent authors a plugin and it takes effect" requires it,
   or requires a process bounce.

### Invented by Aleph, absent in cordis

Blast radius / support set · mandatory probes · `reprobe` · protected-unnameable core · quarantine
with a readable reason · the AST gate · the spawn ledger and probation · the boot manifest with a
`protected` flag settable in exactly one place · `unregister` freeing a name so an
author→refuse→fix→retry loop can close.

Of these, **support set + mandatory probe + unnameable core** are the three worth keeping under any
redesign. They are the answer to the owner's "guardrails" requirement, and cordis genuinely has
nothing like them.

---

## Worth stealing for Aleph

1. **The epoch string.** Encode a dependent's dependency state as a tuple of provider *instance
   identities*, not a set of names. Compare on every change; do nothing when equal.
   `fiber.ts:385-397`. ~15 lines. This is what makes provider swap safe.

2. **The inertia lock.** One in-flight transition per capability; a change arriving mid-transition
   sets the target and is applied when the current one finishes, by re-checking the epoch at the
   end. `fiber.ts:399-458`. Replaces Aleph's process-wide lock with something that actually scales to
   cascading rebuilds. Aleph should pin the equivalent of `fiber.spec.ts:7-63`.

3. **Removal cascades instead of dangling.** When a provider goes, dependents *tear down* — they do
   not keep a stale handle. `reflect.ts:195-201` + `fiber.ts:437-458`. Combine this with Aleph's
   blast radius: cordis's cascade with Aleph's *refusal* is strictly better than either.

4. **Provider removal ordering.** Drop from the global store → notify → `await` every dependent's
   teardown → only then drop your own handle (`reflect.ts:195-201`). Aleph's `Store.drop` runs as a
   plain inverse with no such barrier, so a dependent's inverse can fail to resolve the service it is
   releasing.

5. **`mixin` — a plugin extending the shared vocabulary.** `reflect.ts:239-265`; `TimerService` puts
   `ctx.timeout()` on every context (`timer/src/index.ts:14`). This is the structural reason cordis
   has no fixed-extension-point problem, and it is exactly what the owner means by "everything is a
   plugin". Aleph's dotted key namespace (`aleph_runtime/capabilities.py:62-78`, `db.engine`,
   `models.gateway_catalog`) is a weaker version of the same instinct — good, keep it, and consider
   letting a capability contribute *methods*, not just values.

6. **Config as a validated, describable schema attached to the plugin.** `Config?: StandardSchemaV1`
   (`registry.ts:72`, validated at `fiber.ts:34-46`), with role/i18n hints
   (`hmr/src/index.ts:389-402`). Aleph's `ManifestEntry.config` is an untyped `dict[str, Any]`
   (`manifest.py:47-56`). A schema per capability gives you: validation at mount, a UI, and — the
   one that matters — **a description a model can read to author a config**.

7. **`[Service.check]` as an availability predicate.** A service can report itself *unavailable*
   without being torn down (`service.ts:33`, `fiber.ts:371-383`, used at
   `loader/src/index.ts:133-137` to hide the loader while entries are still settling). This is
   `reprobe` as a continuous signal rather than a one-shot, and it is how you express "up but not
   ready".

8. **Boot through the dynamic path.** `bin.js:1-16` — the framework's own startup is
   `ctx.plugin(Loader)` and one config entry. Aleph should consider making its boot manifest load
   through the same code path an agent-authored plugin uses (with `protected: true` set by the
   loader), so the dynamic path is exercised on every single boot and cannot become the dead code it
   currently is.

9. **The label + `getEffects()` tree.** Every effect carries a human label (`ctx.provide("db")`,
   `ctx.on("custom-event")`) and nests (`fiber.ts:296-306`, `342-346`;
   `dispose.spec.ts:37-74` shows the tree). An operator can ask a live plugin "what have you done to
   the world". Aleph's `EffectScope` stores bare callables — adding a label is nearly free and gives
   you an inspectable teardown plan.

---

## Worth avoiding

1. **Do not allocate an `Error` on the service-access fast path.** `reflect.ts:71` costs ~820 ns per
   access and rises with stack depth. If Aleph ever wants rich composition-time stacks, build them
   **lazily** — capture only when the access is about to fail.

2. **Do not return a fresh wrapper per access.** `getTraceable` allocates a new `Proxy` per read and
   `createShadowMethod` another per method (`utils.ts:110-118`, `148-155`, `157-212`). Aleph's
   `Context.get` returns the object; hoisting it costs literally zero. Keep that.

3. **Do not implement caller attribution implicitly.** The shadow machinery is the codebase's bug
   magnet and its performance floor (§10). If Aleph wants "effects created on your behalf die with
   you", pass the scope explicitly.

4. **Never `eval` config.** `loader/src/config/utils.ts:4-8`. Aleph's manifest is TOML naming
   `module:callable` factories (`manifest.py:47-56`) with no directory scan — that is the right
   posture and the docstring's reasoning is sound. Do not add expression interpolation later.

5. **Do not build hot-reload on private runtime internals.** `loader/src/internal.ts:96-122` calls
   `requireBuiltin('internal/modules/esm/loader')` and maintains two incompatible interface shims for
   Node 22 vs 24 (`internal.ts:50-92`). Python's `importlib` is public API; use it, and accept that
   reload semantics for already-bound references are a design problem, not an implementation one.

6. **Do not let `inject` double as access control if you want a real boundary.** cordis's ancestor
   fallthrough (`reflect.ts:80-94`) means undeclared access often succeeds. Aleph's absolute refusal
   (`context.py:108-109`) is the better rule — keep it, and resist the "but it's convenient"
   pressure.

7. **Do not let `emit` short-circuit on the first throwing listener** (`events.ts:96-99`) while
   `parallel` aggregates (`events.ts:89-94`). Two dispatch modes with different failure semantics and
   no naming cue is a trap. Pick aggregate-always.

8. **Do not confuse "transitions are serialized" with "in-flight work is drained."** cordis's inertia
   lock does the former only; a request executing inside a plugin keeps running against a
   torn-down context after HMR swaps it. Aleph, which will have DB transactions and streaming SSE
   inside plugins, needs an explicit drain/grace phase that cordis does not have.

9. **Do not ship a dynamic API nobody calls.** cordis's dynamic path is the only path; Aleph's is
   dead code (§"Where Aleph got it wrong", 4). Either wire `AgentPluginAPI` into the running app or
   delete it — an unreachable guardrail is a `CLAUDE.md`-grade "write path with no read path".

---

## License and copy safety

MIT, © 2021-present Shigma (`LICENSE`). Reimplementation of the *ideas* is unrestricted; verbatim
copying requires carrying the notice. Per Aleph's standing constraint, reimplement — do not add
`cordis` or any `@cordisjs/*` package as a runtime dependency.

Specific files that would be **unsafe to port verbatim** regardless of license:

- `packages/loader/src/config/utils.ts:4-8` — `new Function` + `with` + `eval`.
- `packages/loader/src/internal.ts:96-122` and `packages/hmr/src/index.ts:290-318` — Node private
  internals and cache surgery; no Python analogue, and the version-sniffing is a maintenance
  liability even in JS.
- `packages/core/src/utils.ts:110-217` — the shadow machinery; JS-`Proxy`-specific and, per §10, the
  thing to redesign rather than translate.

Anything Aleph *does* port should carry a `NOTICE` recording upstream, license and per-file lineage,
per `CLAUDE.md`. Note that `packages/aleph-kernel` currently has **no `NOTICE` file** while
`effects.py:20-22`, `context.py:8-12` and `support.py:14-17` cite cordis and the paper by
file and line — that lineage should be recorded formally the way `packages/aleph-belief/NOTICE` is.
