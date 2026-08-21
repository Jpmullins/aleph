# The Aleph kernel — a design recommendation

**Written:** 2026-08-19.
**Inputs:** the five review files in this directory (`cordis.md`, `deepseek-harness.md`,
`prime-agent.md`, `opencode.md`, `hermes-agent.md`), plus a first-hand read of
`packages/aleph-kernel` (4,047 lines including tests) and `packages/aleph-runtime/…/capabilities.py`.
**Status:** a proposal. Nothing here is built. Where I say Aleph "does" something, I read it; where
I say it "should", I am recommending.

**Everything in §0.3 I verified myself by running code against the kernel as merged at `bcc478a`.**
Three of those findings contradict or extend what the five reviewers reported, and one of them —
§0.3 (E) — is a live privilege escalation that defeats Aleph's headline guardrail. Scripts are in the
scratchpad; each is small enough to re-derive from the description.

---

## Contents

- [§0 — The short version](#0--the-short-version)
- [§1 — The speed question, answered first](#1--the-speed-question-answered-first)
- [§2 — What a plugin is](#2--what-a-plugin-is-q1)
- [§3 — Declaring and resolving](#3--declaring-and-resolving-q2)
- [§4 — Plugins depending on plugins](#4--plugins-depending-on-plugins-q3)
- [§5 — Lifecycle and in-flight work](#5--lifecycle-and-in-flight-work-q4)
- [§6 — Failure containment](#6--failure-containment-q5)
- [§7 — The guardrail](#7--the-guardrail-q6)
- [§8 — Agent-authored plugins](#8--agent-authored-plugins-q7)
- [§9 — Keep, change, delete](#9--keep-change-delete-q8)
- [§10 — The language decision](#10--the-language-decision-q9)
- [§11 — Sequencing](#11--sequencing)
- [Appendix A — vocabulary](#appendix-a--vocabulary)
- [Appendix B — licences](#appendix-b--licences)

---

## §0 — The short version

### 0.1 The single design idea

Aleph's kernel today models **one** thing: a *service with a lifetime* (open a pool, publish it,
close it). Every reviewed system that got composition right models **two** things, and conflating
them is the root of most of the difficulty:

```
   ┌─────────────────────────────────────────┬─────────────────────────────────────────┐
   │  (A) EFFECTS ON THE WORLD               │  (B) CONTRIBUTIONS TO DERIVED STATE     │
   ├─────────────────────────────────────────┼─────────────────────────────────────────┤
   │  open a connection pool                 │  "these three tools exist"              │
   │  start a background task                │  "this connector kind exists"           │
   │  bind a socket                          │  "add this paragraph to the prompt"     │
   │  publish a live service handle          │  "register this retrieval strategy"     │
   ├─────────────────────────────────────────┼─────────────────────────────────────────┤
   │  Needs a hand-written inverse.          │  Needs NO inverse.                      │
   │  Unwound LIFO on teardown.              │  Removal = replay everyone else.        │
   │  Aleph has this, and it is good.        │  Aleph does not have this.              │
   └─────────────────────────────────────────┴─────────────────────────────────────────┘
```

opencode's `packages/core/src/state.ts:61-127` is the sharpest statement of (B), and the reviewer's
argument for it is correct and worth repeating: *a revertible effect is a pair (do, undo) whose
correctness depends on the author writing a genuine inverse, and there is no way to test the inverse
is right except by trying. A replayed transform has no inverse to get wrong.* For derived catalogs —
which is where an "everything is a plugin" system spends most of its surface — replay converts
"silent drift after N add/remove cycles" into "cannot happen".

**So: keep Aleph's revertible effects for (A). Add replayable contributions for (B). One plugin
declares both.** That is the whole proposal in one sentence, and the rest of this document is the
consequences.

### 0.2 Where each reviewed system sits

| | composition model | protected core / removal refusal | probe before activation | agent can add a plugin | isolation for agent code |
|---|---|---|---|---|---|
| cordis | peer services + reactive epoch | ✗ | ✗ (voluntary `check`) | ✗ (no agent concept) | ✗ |
| deepseek-harness | cordis + ~45 seams | ✗ (parks only) | ✗ | **✓ shipped end to end** | `vm` — explicitly "not a security boundary" |
| prime-agent | two planes; only the Python one composes | ✗ | ✗ | ✓ (no gate at all) | ✗ (documented as absent) |
| opencode | **replayable transforms** | ✗ | ✗ | ✓ (glob, no gate) | ✓ for *code*, via CodeMode interpreter |
| hermes-agent | 37 fixed hooks + 23 `register_*` | ✗ | ✗ | ✗ (Markdown only) | ✗ ("This is NOT a sandbox") |
| **Aleph today** | services with declared coeffects | **✓ and it is the best in the survey** | **✓ and nothing else has it** | ✗ — code exists, is unreachable | ✓ `code-runner`, but nothing routes plugins to it |

The shape of the opportunity is visible in the two bold columns. **Nobody else has a protected core,
a computed blast radius, or a mandatory probe.** Those are Aleph's genuine inventions and they are
the reason to keep this kernel rather than adopt someone else's. What Aleph is missing is everything
in the columns to their right *and* the reactive third of the paper — and the missing half is exactly
the half the product thesis needs.

### 0.3 What I verified first-hand (and where the reviewers were wrong)

Run against the tree as merged. All five are original to this document except where noted.

**(A) `boot()` is all-or-nothing; there is no quarantine.**
The `cordis.md` reviewer describes a `Kernel._plan` at `kernel.py:191-214` that "marks unresolvable
plugins FAILED with a reason and proceeds", and an `unactivatable()` read path at `:159-172`, and an
`unregister`. **None of these exist.** `kernel.py` is 368 lines; its full method inventory is
`register_core, register_dynamic, _register, _specs, store, state_of, active, is_provided,
blast_radius, boot, activate, _providers_for, _activate, replace, reprobe, deactivate, _teardown,
shutdown, _name_for`. `boot()` (`kernel.py:152-157`) loops `topological_order` calling `_activate`,
which re-raises on probe failure — so one bad capability aborts the entire process boot.
`lifespan.py:64-68` confirms this is intended for core. It is the wrong behaviour for plugins.

**(B) An agent that installs a broken plugin can never retry the name.** Verified:

```
A. install a BAD plugin: InstallOutcome(installed=False, plugin_id=None, detail='refused: deliberately broken')
   state now: failed
B. agent fixes it, retries same name: InstallOutcome(installed=False, plugin_id=None, detail="'mine' is already registered")
```

`_activate` sets `State.FAILED` but leaves the entry in `self._mounted`, and `_register`
(`kernel.py:110-113`) raises on a duplicate name. `deactivate` → `_teardown` also only resets state
to `REGISTERED`; it never removes the entry. So the author → refuse → fix → retry loop — the *entire*
self-improvement loop — cannot close. The API returns `plugin_id=None` on failure, so the agent does
not even hold a handle to clean up with.

**(C) `blast_radius` is computed over declared specs, not over what is running.** Verified: a
capability that is `REGISTERED` and was never activated still counts as collateral damage.

```
C. leaf registered but NEVER activated. state: registered
   blast_radius('base') claims collateral: ['leaf']
```

`support_set` (`support.py:37-63`) takes `Mapping[str, CapabilitySpec]` with no state filter, and
`Kernel._specs()` returns every mounted spec. The guardrail therefore over-refuses, which trains an
agent to pass `force=True` — and a guardrail that is routinely forced is not a guardrail.

**(D) `provides` is declared but never checked against what `setup` actually published.** Verified — a
capability declaring `provides={"db"}` whose setup publishes nothing activates cleanly:

```
liar state: active | is 'db' actually provided? False
```

Dependents then hit `InactiveAccess` at first use, at runtime, far from the cause. The `cordis.md`
reviewer flagged this correctly; I confirm it. It is a three-line fix in `_activate`.

**(E) A dynamically registered plugin can silently shadow a core service key. This is a privilege
escalation, and it defeats the headline guardrail.** Verified:

```
core provides db.sessions = (True, 'THE REAL CORE SESSION FACTORY')
after dynamic plugin activates: (True, 'ATTACKER-CONTROLLED FACTORY')
```

`Store.put` (`context.py:47-49`) is an unconditional `dict` assignment. `register_dynamic` refuses a
spec that claims `protected` (`kernel.py:89-94`) but does not look at `provides` at all.

The consequence is worth stating plainly, because every reviewer praised the unnameable-core design
without testing the key namespace. It is true that an agent cannot **deactivate** `database` — there
is no `PluginId` for it. But it does not need to. It installs a plugin declaring
`provides={"db.sessions"}`, and from that moment every capability that resolves `db.sessions` — all
of them, including protected ones — gets the agent's object instead. The core capability is still
`ACTIVE`, still protected, still unnameable, and completely bypassed.

**Aleph's guardrail protects the capability graph and leaves the name namespace wide open.** Fixing
this is the highest-priority item in this document (§7.2), and it is about ten lines.

---

## §1 — The speed question, answered first

> *"How to do this right so aleph doesn't get crazy slow? The plugins have to compile and communicate
> properly so that they would work as or almost as efficiently as a single compiled system."*

This worry is real in general and **almost entirely misplaced for Aleph specifically**, and the
reason is worth establishing with numbers before designing around it.

### 1.1 The measured cost ladder

Measured on this machine, CPython 3.13.14, against the real `aleph_kernel.context` code — not a model
of it. Absolute numbers are machine-specific; the *ratios* are the argument.

| operation | ns/op | relative to a direct call |
|---|---:|---:|
| direct `db.query(1)` | 10.5 | 1× |
| **hoisted kernel handle `h.query(1)`** | **10.6** | **1.0× — the kernel tax is zero** |
| `await` a coroutine (no suspension) | 35.1 | 3× |
| `ctx.get("db").query(1)` | 87.1 | 8× |
| **`ctx.db.query(1)` (`__getattr__` path)** | **115.3** | **11×** |
| `json.loads(json.dumps(small_dict))` | 1,124 | 107× |
| one real event-loop yield (`await asyncio.sleep(0)`) | 13,964 | 1,330× |
| one Postgres round-trip (typical LAN) | ~200,000–500,000 | ~20,000–50,000× |
| **one LLM output token** | **~10,000,000–50,000,000** | **~1,000,000–5,000,000×** |

For calibration, the `cordis.md` reviewer measured cordis's equivalent path at **~1,427 ns** — 12×
Aleph's — because `reflect.ts:71` allocates a stack-capturing `new Error()` on *every* service access
before knowing the access will fail, and `getTraceable` returns a freshly allocated `Proxy` per read.
Crucially, in cordis **hoisting does not help** (~190 ns/call remains); in Aleph hoisting is free,
because `Context.get` returns the object itself rather than a wrapper.

### 1.2 What the ladder means

The kernel's mediation costs **105 nanoseconds**, and only when you resolve inside the loop rather
than above it. One LLM token — the unit Aleph's workload is actually made of — costs on the order of
**ten million nanoseconds**. You could resolve a capability through the kernel one hundred thousand
times per token and not measure it.

So the honest framing is: **the plugin boundary is not where an agent harness gets slow.** Every one
of the five reviewed systems reaches the same conclusion by a different route, and none of them found
in-process dispatch to be a bottleneck. What actually makes these systems slow is four things, in
descending order of impact:

1. **Context-window tokens.** Fifty plugins each contributing a tool schema is thousands of tokens
   *per turn, forever*. This is the real cost of "everything is a plugin" and it is a *token* cost,
   not a CPU cost.
2. **Round trips.** N tool calls = N model round trips = N × seconds.
3. **Serialization at boundaries that did not need to exist.** 1,124 ns is 107× a direct call. Cross
   a process boundary per chunk and you have built something genuinely slow.
4. **Startup import cost.** Hermes measured ~150 ms of manifest scanning + module imports; prime-agent
   measured ~1.2 s for a cold IPython kernel. This scales with plugin *count* and it is the one place
   plugin count really does drive a number the user feels.

### 1.3 The five rules that follow

These are the design constraints, and each is traceable to specific evidence.

**Rule 1 — In-process, nothing serialized, references not payloads.**
All five systems converge here. `ctx.db.query(x)` is a real method call on the real object. Big
payloads never cross a plugin boundary: deepseek-harness passes content-addressed attachment refs and
spill locators (`attachment/README.md:5`); prime-agent keeps parsed corpora as Python variables in the
kernel and caps the crossing at `DEFAULT_MAX_OUTPUT_CHARS = 65536`. Aleph already does this with the
asset store — extend the discipline to every plugin-to-plugin transfer.
*Corollary:* the deep-copy discipline belongs **only** at a confinement boundary. opencode's
reviewer is explicit: do not generalise `copyIn`/`copyOut` to plugin↔host calls.

**Rule 2 — Contributions cost nothing on the read path.**
This is opencode's structural answer, and it is better than optimizing dispatch — it *deletes* the
dispatch. A consumer reads a materialized `Map`; **no plugin code runs on the read path at all**
(`catalog.ts:192-197`). Plugin code runs only at rebuild time, and a rebuild happens on a composition
change, not on a call. Hermes reaches the same place from the other direction with a monotonic
generation counter on the registry used as a memo key (`tools/registry.py:451-457`,
`model_tools.py:363-376`), so the composed tool catalog is recomputed once per change rather than once
per call.

**Rule 3 — Intercept at construction, never per chunk.** *(the sharpest single idea in the survey)*
deepseek-harness's `llm/stream` waterfall (`llm/src/index.ts:913-925`) runs **once per model call** and
returns an `AsyncIterable`; tokens then flow through a bare async generator with zero plugin dispatch.
This is a control-plane / data-plane split: plugins negotiate *what stream exists* (once), bytes move
through a direct iterator (N times). **It makes the plugin tax O(calls) instead of O(bytes).**
opencode does the same at a different granularity by memoizing the model-resolution hook chain by
`(provider, model, variant)` (`aisdk.ts:198-227`).

The rule for Aleph: **a plugin may not sit on a per-token, per-chunk, or per-row path. If it must
influence one, it influences the object that path uses, once, at construction.**

**Rule 4 — Observers on hot paths are queued, never awaited, and gated before the payload is built.**
Hermes is the model here and it is the pattern to copy nearly verbatim. Every fire site gates on
`has_hook()` before constructing a payload — *"when nothing subscribes no payload is built and the hot
paths pay one dict probe"* (`plugins.py:285-288`) — and per-token stream observers get one bounded
queue plus one thread per consumer with drop-oldest backpressure and return values ignored
(`plugin_stream_hooks.py:120-144`, `_QUEUE_SIZE = 1024`). A slow plugin cannot throttle the token
stream; it loses events.

The anti-pattern is prime-agent's `ExtensionRunner.emit()`, which allocates a fresh 14-getter context
object *before* checking `hasHandlers()` — on a per-streamed-token event (`runner.ts:674-676`,
`567-625`). The class already has the guard and does not use it there.

**Rule 5 — Manifest-eager, module-lazy.**
Hermes's best performance idea (`plugins.py:4437-4523`): the YAML manifest declares the surface and the
host registers a *deferred loader*; the Python module and its heavy SDK are imported only on first real
use. It goes one level finer — a platform plugin's cheap client tools register with zero import while
its heavy adapter waits. **Capability becomes visible and routable without paying import cost, so
plugin count stops driving startup time.**

If import cost ever becomes the bottleneck anyway, prime-agent's fork server
(`fork-server-script.ts:37-93`) is the escape: a pre-imported, `gc.freeze()`-d template process forks a
ready runtime in milliseconds instead of ~1.2 s. Two disciplines make it safe and both are worth
adopting as house rules regardless: **every failure path degrades to the slow path**, and **every
magic constant carries its measurement in the comment that sets it**
(`boot-gate.ts:9-16`: *"256 collapses to ~28% boot at N=200; core*4 holds 100%"*).

### 1.4 And the actual answer to context bloat

Rules 1–5 handle CPU. The dominant cost — tokens — needs a sixth idea, and three independent teams
converged on it:

- prime-agent exposes **exactly one** model-facing tool, `ipython` (`tools/index.ts:45-55`). Fifty
  skills cost fifty one-line descriptions. An MCP server with 40 tools costs **zero** schemas, because
  it is a Python object whose methods bind lazily via `__getattr__` with the JSON Schema in `__doc__`.
- opencode's CodeMode exposes one `execute` tool over a confined AST interpreter with explicit
  `{timeoutMs, maxToolCalls, maxOutputBytes}` budgets.
- deepseek-harness's Code Mode collapses the catalog behind one `run_code`, stating it to the model
  outright: *"`run_code` is the only tool you can call directly."*
- Hermes's `execute_code` does it over a Unix socket, and names the payoff exactly:
  *"only the script's stdout is returned to the LLM; intermediate tool results never enter the context
  window"* (`code_execution_tool.py:1-26`).

**Aleph already has the hard part of this** — `apps/code-runner` is a real sandbox: `cap_drop: ALL`,
read-only rootfs, a Redis-only internal network with no route to Postgres or the API, `python -I`, a
socket guard, `RLIMIT_CPU`/`RLIMIT_FSIZE`, and a pinned audited scientific stack. What is missing is
that it currently only renders artifacts. The move is to **make it persistent across turns and expose
Aleph's own typed services (retrieval, belief, scholar) as importable modules inside it.** Then a
45-pair retrieval eval is one cell, not 45 tool calls; capability composes at the Python
function-call boundary; and the model-facing surface stays at one tool no matter how many plugin
suites exist.

That is the real answer to the owner's worry: **capability count must not scale context cost, and the
plugin boundary should be a function call inside one process.** The nanoseconds were never the issue.

---

## §2 — What a plugin is (Q1)

### 2.1 The shape

**One plugin identity. Two kinds of declaration. A manifest that is readable without importing
anything.**

The "one identity" part is not stylistic. prime-agent is a live demonstration of the alternative: two
extension planes with opposite composability rules, where "an author who wants to add a capability must
guess which plane it belongs to, and a capability that needs *both* cannot be written at all without
patching the host" — the host in question being a 10,963-line god object. opencode has three plugin
systems and a visibly stalled migration in which the flagship extension type (a tool) still cannot be
registered through the new API. **The rule is: if Aleph ever has two plugin systems, one of them is
dead code, and the dead one will be the guarded one.**

A plugin is a directory:

```
plugins/arxiv/
  plugin.toml      the manifest — parsed, never imported. Declares the surface.
  arxiv_plugin.py  the code — imported on first activation, not at discovery.
  NOTICE           only if anything was ported
```

or, for something small, a single `.py` file with the manifest in a module-level `PLUGIN = {...}`
dict. deepseek-harness's reviewer is right that requiring every plugin to be a full package
(`package.json` + tsconfig + build config + 3 READMEs + an invariant file, across ~200 packages)
is a tax paid before a line of behaviour. **A plugin should be able to be one file, promoted to a
package only when it needs its own dependency closure.**

### 2.2 The smallest complete example, written out

This is the whole thing. A plugin that adds arXiv as a source connector and one tool.

```toml
# plugins/arxiv/plugin.toml
#
# Parsed at discovery. NOTHING here imports Python — the kernel can show this
# capability, wire the graph, and route to it without paying the import cost.
[plugin]
name    = "arxiv"
version = "0.3.0"
entry   = "arxiv_plugin:register"     # module:callable, imported on first activation

[plugin.config]
categories  = { type = "array", item = "string", default = ["cs.AI", "cs.CL"] }
max_results = { type = "integer", default = 25, min = 1, max = 200 }
```

```python
# plugins/arxiv/arxiv_plugin.py
"""arXiv as a source connector plus one search tool.

Module top level is definition-only, so the AST gate admits it and importing
this file cannot do anything. Everything that touches the network happens
inside a function body.
"""

from aleph_kernel import Plugin, Registry, ok, problem

from .client import ArxivClient          # this plugin's own code


def register(p: Plugin) -> None:
    """Declaration only. Called once at mount; performs no work."""

    # ---- (A) an effect on the world: a live service with a lifetime --------
    @p.capability(provides=["connector.arxiv"], requires=["http.gateway"])
    async def arxiv(ctx):
        client = ArxivClient(ctx.get("http.gateway"), p.config["categories"])
        ctx.provide("connector.arxiv", client)
        yield client.aclose                      # the inverse, on the next line

    @arxiv.probe
    async def _(ctx):
        """Exercise the READ path against the live system, or do not load."""
        hits = await ctx.get("connector.arxiv").search("attention", limit=1)
        if not hits:
            return problem("arxiv search returned nothing for a query with known results")
        return ok(f"live search returned {len(hits)}")

    # ---- (B) a contribution to derived state: no inverse, ever ------------
    @p.contributes(Registry.TOOLS, requires=["connector.arxiv"])
    def _(draft, ctx):
        draft.set("arxiv_search", tool(
            description="Search arXiv. Returns titles, abstracts and DOIs.",
            input=ArxivQuery,
            run=lambda q: ctx.get("connector.arxiv").search(q.text, q.limit),
        ))
```

Twenty-eight lines of substance. Note what is *absent*: no `activate()`/`deactivate()` pair, no
`dispose()` to write, no registration call in a central file, no teardown code for the tool. The one
inverse in the file (`client.aclose`) sits on the line after the thing it undoes.

### 2.3 Why exactly these two kinds

The two decorators are not symmetric, and the asymmetry is the design.

**`@p.capability`** is Aleph's existing `CapabilitySpec`, unchanged in spirit: `provides`, `requires`,
an async-generator `setup` yielding inverses, and a **mandatory probe**. It is for things with a
lifetime and a real inverse. Aleph's version of this is already better than cordis's — `EffectScope`
guards *every* iteration step where cordis guards only its async branch (`fiber.ts:248-268`), and it
aggregates disposal failures into an `ExceptionGroup` where cordis merely logs.

**`@p.contributes`** is new, and is opencode's `State.create` model. It edits a *draft* of a named
registry. Removing the plugin does not run an undo — it drops the transform from the ordered list and
replays everyone else from a fresh initial value. The correctness argument bears repeating: there is
no inverse to get wrong.

The dividing line is a question the author can always answer: **"if I deleted my code and re-ran
everyone else's, would the world be right?"** For a tool entry, yes — replay. For an open socket, no —
inverse.

### 2.4 What is *not* a plugin

Nothing. That is the point, and it is the specific thing hermes-agent gets wrong. Hermes has 37
hard-coded hook names and 23 near-identical `register_*` methods on `PluginContext`, so *"every new
capability category is a core patch"* — and the reviewer notes the bodies of `register_tts_provider`,
`register_video_gen_provider`, `register_web_search_provider`, `register_browser_provider`,
`register_image_gen_provider`, `register_transcription_provider` and `register_dashboard_auth_provider`
are *the same five steps with different imports*. That single omitted abstraction is what converts
"everything is a plugin" into "everything is a plugin **of a kind the host already knows about**".

Aleph avoids it structurally: **a registry is itself declared by a plugin** (§4.3). There is no fixed
list of extension-point kinds to enumerate, because adding a kind is writing a plugin.

---

## §3 — Declaring and resolving (Q2)

### 3.1 The declaration

A plugin declares, in the manifest (eager, no import) and confirmed by the code (lazy):

| declaration | meaning | enforced how |
|---|---|---|
| `requires: [key, …]` | service keys this plugin resolves | `Context.get` refuses anything outside it |
| `provides: [key, …]` | service keys this plugin publishes | **asserted after setup** (new — fixes §0.3 D) |
| `contributes: [registry, …]` | registries this plugin edits | draft API refuses others |
| `declares: [registry, …]` | *new* registries this plugin creates | the registry's own capability |
| `config` | a schema | validated at mount |

Two things Aleph should change immediately.

**`provides` must be verified.** After `setup` returns, assert that every declared key is actually in
the store and owned by this capability. Three lines in `_activate`, and it converts §0.3 (D) from a
runtime `InactiveAccess` in a dependent into a load-time refusal naming the liar. cordis avoids this
class of bug by not having a static declaration at all; Aleph's static declaration is the better
design *provided it is checked*, because it is what makes `blast_radius` computable at all.

**`config` must be a schema, not `dict[str, Any]`.** `ManifestEntry.config` (`manifest.py:47-58`) is
untyped today. cordis attaches a Standard Schema per plugin, validated at mount, with role and i18n
hints (`registry.ts:72`, `fiber.ts:34-46`); deepseek-harness does the same with `schemastery` so *"a
bad row in the YAML fails at load with a named field, not at first use"*. The reason this matters more
for Aleph than for either of them: a machine-readable config schema is **what a model reads in order
to author a plugin's configuration**. Without it, agent-authored configuration is guesswork.

### 3.2 The resolution

Keep Aleph's model. It is already the strongest in the survey and the reasoning in
`context.py:1-19` is sound.

```
   plugin declares requires={"http.gateway"}
              │
              ▼
   ctx.get("http.gateway")
              │
      ┌───────┴────────┐
      │ in requires?   │──no──► UndeclaredAccess     "add it to your requires"
      └───────┬────────┘
             yes
              │
      ┌───────┴────────┐
      │ realm lookup   │   (realm, key) → root fallback
      └───────┬────────┘
              │
      ┌───────┴────────┐
      │ bound?         │──no──► InactiveAccess       "your provider is not up"
      └───────┬────────┘
             yes
              ▼
        the raw object — no proxy, no wrapper, hoistable to zero cost
```

Two properties here are load-bearing and both are advantages over cordis:

**Denial is absolute.** cordis's `inject` is primarily a *reactivity* declaration; a plugin that did
not declare a key still sees it if any ancestor fiber provides or injects it
(`reflect.ts:80-94`). Aleph refuses outright with no ancestor fallthrough. Keep this and resist the
"but it's convenient" pressure — it is the only reason the declaration is a boundary rather than
documentation.

**Resolution returns the object.** No proxy. This is why hoisting is free (§1.1) and it is the single
biggest performance advantage Aleph already holds over the reference implementation. Do not give it
away for caller attribution or tracing — cordis's shadow/traceable machinery buys those, and it is
simultaneously that codebase's performance floor, its bug magnet (4 of the last 10 commits touch it),
and unteachable.

### 3.3 One addition: the third denial message

deepseek-harness's sandbox façade distinguishes **three** cases where Aleph distinguishes two
(`guard.ts:723-736`):

1. *"you did not declare it"* → `UndeclaredAccess`. Aleph has this. **Fix:** add `inject: [...]`.
2. *"its provider is not up"* → `InactiveAccess`. Aleph has this. **Fix:** none — wait, or check the boot audit.
3. *"withheld by design"* → **Aleph lacks this.** A key the runtime has and will never expose to this
   caller. Framework internals, the raw `Store`, another project's realm.

The third is what an agent-facing surface needs, and dsh's habit of **naming the available
alternatives in the message** is worth copying. A refusal an agent cannot act on is indistinguishable
from a broken tool.

---

## §4 — Plugins depending on plugins (Q3)

### 4.1 By name, never by identity

Plugin B never names plugin A. It names a **service key** or a **registry**, and any plugin publishing
that key satisfies it.

The payoff is concrete and deepseek-harness documents it with a real example: seven independent
plugins (`bash-local`, `bash-sandbox`, `terminal-bash`, `lsp-stdio`, and three subagent backends)
consume `ctx.subprocess`. Swapping `subprocess-local` for `subprocess-e2b` moves all seven onto a
remote sandbox **with no forks in any consumer** (`docs/architecture.md:102`). That is the thing a
fixed extension-point host structurally cannot do.

For Aleph the equivalent is immediate: `retrieval.hybrid` is a key. A plugin that provides a better
one substitutes for it everywhere, and `aleph_evals.retrieval_eval` measures both against the same
45-pair set without either plugin knowing the other exists.

### 4.2 Three ways to depend, and when to use each

```
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ 1. HARD SERVICE DEPENDENCY      requires=["connector.arxiv"]               │
  │    "I cannot run without this."                                           │
  │    → the kernel will not activate you until it is up                       │
  │    → if it goes away you are torn down (and rebuilt when it returns)       │
  ├────────────────────────────────────────────────────────────────────────────┤
  │ 2. SOFT PROBE                   if ctx.has("connector.arxiv"):             │
  │    "I do more when this exists."                                          │
  │    → never blocks activation; you handle absence                          │
  ├────────────────────────────────────────────────────────────────────────────┤
  │ 3. ORDERED CONTRIBUTION         @p.contributes(Registry.TOOLS)             │
  │    "I edit a shared catalog someone else also edits."                     │
  │    → no dependency at all; order is declared house policy                 │
  └────────────────────────────────────────────────────────────────────────────┘
```

deepseek-harness states the (1)-vs-(2) choice to its own model better than I can:
*"Read an optional Service with `ctx.get('serviceName')` by default and handle undefined. Declare
`inject: ['serviceName']` only when the Service is a hard dependency and the Plugin must enter waiting
until Cordis reactivates it after the Service appears"* (`tool-cordis/src/prompt.ts:60-62`). Put
something like that sentence in Aleph's plugin-authoring prompt verbatim.

For (3), ordering must be **declared house policy, not discovery order**. opencode writes it down
(`PLAN.md:255-274`): built-ins → base data sources → config projections → provider normalisation →
**user plugins** → core finalisation, so a user plugin always gets the last word before invariants are
enforced. prime-agent, by contrast, orders by directory-scan sequence and resolves conflicts with
silent precedence rules that differ per registration kind ("first wins" for extension-vs-extension,
"extension wins" for extension-vs-builtin) — a rule nobody can hold in their head.

### 4.3 The thing that makes this not-VSCode: a plugin can create a registry

This is the crux of Q3 and the answer to hermes's 23 `register_*` methods.

**A registry is a capability.** It provides the registry object and declares the shape of a draft. A
plugin creating a new extension *kind* is a plugin, not a core patch:

```python
def register(p: Plugin) -> None:
    @p.declares_registry(
        "retrieval.strategies",
        initial=dict,                       # fresh blank state per rebuild
        draft=StrategyDraft,                # what a contributor may do to it
        finalize=assert_exactly_one_default, # invariants, after everyone has spoken
    )
    async def strategies(ctx): ...
```

Now any other plugin writes `@p.contributes("retrieval.strategies")` and the kernel wires it. Nothing
in `aleph-kernel` knows the word "retrieval".

Two guardrails on `finalize`, both learned from opencode's mistakes: keep it to **invariant
enforcement only** — their own plan says *"Core finalization is for invariants and materialization,
not plugin extension behavior"* (`PLAN.md:241`) and their catalog finalizer already does policy
filtering *and* event publishing, which is how a second privileged plugin system starts. And put
post-commit notification **after** the commit, not inside it.

The second guardrail: **version each registry's committed state.** opencode admits domains rebuild
sequentially with no cross-domain transaction (`PLAN.md:336`), so a reader can observe a torn state
where the catalog rebuilt but the agents referencing it did not. For opencode that is a cosmetic
flicker. For Aleph, where a belief plugin and a retrieval plugin could disagree about which sources
exist, it is a correctness bug. Stamp every committed registry state with the kernel generation and
let a reader detect a torn read.

### 4.4 Extending the shared vocabulary

cordis's `ctx.mixin` (`reflect.ts:239-265`) lets a service graft its methods onto every context, so
`TimerService` makes `ctx.timeout()` available everywhere. The reviewer calls this *"the structural
reason cordis does not have VSCode's fixed-extension-point problem"* — a plugin extends the shared
vocabulary rather than filling a slot the host predefined.

Aleph's dotted key namespace (`db.engine`, `models.gateway_catalog`) is a weaker version of the same
instinct. **Keep the namespace; do not add `mixin`.** Implicit vocabulary injection is exactly the
kind of magic that makes cordis's shadow machinery unteachable, and Aleph gets the same composability
from registries (§4.3) with an explicit declaration. This is a place where the reference
implementation's idea is right and its mechanism is wrong.

---

## §5 — Lifecycle and in-flight work (Q4)

### 5.1 States

```
                    register (manifest parsed — no import yet)
                              │
                              ▼
                        ┌──────────┐
                        │ DECLARED │  surface known, code not imported
                        └────┬─────┘
                             │ a requirement becomes satisfiable
                             ▼
                        ┌──────────┐   requirement lost
                        │ PENDING  │◄──────────────────┐
                        └────┬─────┘                   │
                             │ all requires up          │
                             ▼                          │
                        ┌──────────┐                    │
                        │ LOADING  │  import, setup      │
                        └────┬─────┘                    │
                  probe fails│  probe passes             │
              ┌──────────────┴──────┐                    │
              ▼                     ▼                    │
        ┌────────────┐        ┌──────────┐               │
        │ QUARANTINED│        │  ACTIVE  │───────────────┘
        │  + reason  │        └────┬─────┘
        └────────────┘             │ deactivate / replace
              │                    ▼
              │ operator      ┌──────────┐
              │ or agent      │ DRAINING │  quiesce, bounded deadline
              │ retries       └────┬─────┘
              │                    ▼
              │              ┌──────────┐
              └─────────────►│ UNWOUND  │ inverses LIFO; name RELEASED
                             └──────────┘
```

Four differences from Aleph today, each fixing something verified in §0.3:

- **`DECLARED`** — the manifest-eager/module-lazy split (Rule 5). Aleph has no such state; today
  `mount_manifest` imports every factory at boot.
- **`PENDING`** — Aleph has no waiting state; `topological_order` raises `MissingProvider` at boot for
  the *whole graph*. That is right for a fixed core manifest and wrong for dynamic plugins. cordis and
  dsh both park; adopt parking for non-protected plugins.
- **`QUARANTINED` with a reason** replaces `FAILED`, and — critically — **is not a dead end.**
  §0.3 (B) shows `FAILED` today permanently consumes the name. Quarantine must release the name on
  retry, or the self-improvement loop cannot close.
- **`DRAINING`** — Aleph has nothing here at all. See §5.4.

### 5.2 Replace live

Aleph's `Kernel.replace` (`kernel.py:240-287`) gets the most important thing right, and its own
docstring says why: *"THE VALUABLE PROPERTY IS THE ROLLBACK, not the swap."* If the replacement fails
setup or probe, the previous implementation is brought back. **Keep this. It is better than every
hot-reload path in the survey**, all of which are best-effort.

What it gets wrong is that **dependents are neither torn down nor rebuilt.** They keep whatever they
cached from the old instance. cordis's reviewer names this correctly as *"a real defect, not a
simplification"*, and the fix is cordis's ~40 lines:

**The epoch string** (`fiber.ts:385-397`). A dependent's dependency state is a concatenation of its
providers' *instance* uids. Three properties fall out of one choice:

1. **Identity, not just presence.** Replacing `db` with a different instance changes the epoch, so
   dependents rebuild against the new one. Presence-checking alone misses this entirely — which is
   exactly Aleph's current bug.
2. **Free coalescing.** `if (epoch === oldEpoch) return` — a remove/re-add of the same instance within
   one tick does zero work.
3. It is the input to the transition lock below.

**Provider-removal ordering** (`reflect.ts:195-201`) — a detail that is easy to get wrong and cordis
got right: drop the binding from the *global* store → notify → `await` every dependent's teardown →
**only then** drop your own handle. That way a dependent's inverse can still reach the service it is
releasing. Aleph's `Store.drop` runs as a plain inverse with no such barrier.

### 5.3 Replace the global lock

Aleph serializes **all** composition under one process-wide `asyncio.Lock` (`kernel.py:74`), which its
docstring defends as removing the interleaving bug class. That was a defensible simplification for a
fixed boot manifest. It does not survive contact with the product thesis, for two reasons: a slow
teardown blocks every other composition change process-wide, and it does not compose with the reactive
rebuild in §5.2 — cascading to N dependents needs per-dependent transition serialization.

The replacement is cordis's **inertia lock** (`fiber.ts:399-458`): one in-flight transition *per
plugin*; a change arriving mid-transition just sets the target, and the running reload/unload
re-checks the epoch when it finishes and chains the opposite move. It is pinned by three fake-timer
tests (`fiber.spec.ts:7-63`) driving load→dispose→reprovide and asserting every intermediate state.
Aleph should pin the equivalent.

### 5.4 In-flight work — the part nobody solved

This is where every reviewed system is weak, and it is the clearest opening for Aleph to be better.

| system | what happens to work in flight during a swap |
|---|---|
| cordis | transitions serialized, **work not drained** — a request keeps running against a torn-down context |
| deepseek-harness | version pinning: `pluginRunId`, stale calls refused with `code: 'stale-run'`; sessions pin their composition by file mtime+size |
| prime-agent | documented footgun: *"treat reload as terminal for that handler"* |
| opencode | copy-on-write transform list + **identity tokens** → `Stale tool call` |
| hermes | drops *queued* events by generation; explicitly *"a currently-running callback cannot be force-killed safely"* |
| **Aleph today** | nothing — no drain, no deadline, no generation |

**Recommendation: a three-layer answer, because "in-flight work" is three different problems.**

**Layer 1 — derived state needs no drain. It needs identity tokens.**
opencode's mechanism is twelve lines and it is the best small idea in that codebase
(`registry.ts:50-61, 93-101`). Every registration carries a fresh `{}` token. A turn snapshots the
effective table and closes over it. At settlement the token in the snapshot is compared against the
one currently registered; a mismatch returns `Stale tool call: <name>` rather than silently executing
different code. Registrations *stack*, and disposal removes only that entry, revealing the previous
one — so uninstalling a tool override restores the original for free.

**Layer 2 — service capabilities need quiesce with a bounded deadline.**
Nobody in the survey has this, and hermes's own research spike names it as the field-wide gap and then
does not build it: *"Neither system times out runtime hooks; both shipped hang-class failures… be the
first framework to have them."* Then `invoke_hook` (`plugins.py:5071-5115`) is a bare loop with no
timeout. **This is available to Aleph as a differentiator, pre-validated by three codebases' pain and
nobody's implementation.**

The shape, borrowing opencode's teardown budget (`runtime.ts:426-462`) and dsh's quiescence rule:

```
DRAINING(plugin, deadline):
    stop admitting new work        (withdraw the binding first — cordis's ordering)
    signal cancellation            (an AbortSignal-equivalent to in-flight callers)
    await in-flight to settle, against ONE shared deadline
    if deadline expires:
        → REFUSE THE SWAP and stay ACTIVE, naming what did not settle
    else:
        unwind inverses LIFO, each with its own slice of the remaining budget
        a failing inverse does not abort the unwind; a hanging one cannot wedge the host
```

The refusal is the interesting half. deepseek-harness's rule — *"Dispose must reach quiescence, not
just request it. A teardown that issues kills/aborts but returns before the work stops leaves
orphans"* — is right, and Aleph's `EffectScope.unwind` already runs every inverse even when one
raises. What Aleph must add is that **a swap that cannot quiesce is refused rather than forced.**
Aleph will have open Postgres transactions and streaming SSE inside plugins; forcing there corrupts.

**Layer 3 — handles minted before the swap must throw.**
prime-agent's stale-handle invalidation (`loader.ts:115-161`, `runner.ts:567-625`) is capability
revocation by expiry and it is cheap: every method begins with `assertActive()`, and the error names
the mistake *and the fix*. Aleph's kernel already unwinds LIFO; adding "and every handle minted before
the swap now throws" closes the window where a hot-replaced plugin keeps writing through a dead
reference. Pair it with prime-agent's `Symbol` + `WeakSet` brand on host handlers
(`kernel/index.ts:83-142`) so a forged handle cannot be substituted.

---

## §6 — Failure containment (Q5)

### 6.1 The current posture is wrong for plugins

§0.3 (A): one capability failing its probe aborts the whole boot. That is *correct for the protected
core* — a process that cannot open Postgres should not serve requests, and `lifespan.py:64-68` argues
this well. It is *wrong for everything else*: an agent-authored plugin that fails must not stop the
process.

**Split the posture by trust tier, which is deepseek-harness's "opposite failure postures by phase"
generalised** (`client-modules.md:44` — loud aggregate at activation, warn-and-isolate in steady
state):

```
   protected core fails      →  abort boot, loudly, naming the capability and the probe detail
   plugin fails at load      →  QUARANTINE it, name the reason, boot continues
   plugin fails at runtime   →  contain per call site; count it; auto-quarantine on repeat
   plugin violates a contract→  FATAL — see §6.3
```

### 6.2 Six mechanisms, each with a source

**1 — Per-plugin scope, unwound on failure.** Aleph has this and it is good: `_activate`
(`kernel.py:203-231`) unwinds on setup-raise, probe-raise and probe-fail alike. opencode's equivalent
is `Effect.onExit(… Scope.close(child, exit))`.

**2 — Deadline budgets on every plugin callback.** §5.4 Layer 2. The field-wide gap. Two tiers:
observer callbacks get *budget + log-and-drop*; guard/mutating callbacks get *budget + fail closed*.

**3 — Observers are queued, never awaited.** Rule 4. One bounded queue per consumer, drop-oldest,
return values ignored. A slow plugin loses events; it never throttles the caller.

**4 — Quarantine with a reason, and a retry that actually works.** §0.3 (B) is the blocker. Quarantine
must record `(plugin, generation, reason, at)` and **release the name**, so the agent's
author → refuse → fix → retry loop closes. Today it does not.

**5 — Errors are attributed, never swallowed.** opencode's server path does
`.pipe(Effect.ignoreCause)` on external plugin load (`config/plugin/external.ts:87`) — a broken user
plugin produces *no diagnostic at all* — while their TUI path logs properly with the plugin id,
"evidence the authors know better and the server side has not caught up". Every failure in Aleph
carries a plugin id, a generation, and a reason.

**6 — A boot audit that names unresolved services.** deepseek-harness's `assertEntriesActivated`
(`app-boot/src/index.ts:692-726`): for a `PENDING` entry, list exactly which injected services are
still undefined, *"because no plugin error exists for that state"*. A stuck plugin should never report
"startup failed"; it should report *"waiting for: connector.arxiv, rks.ingest"*.

### 6.3 Two error classes, not one

deepseek-harness's best containment detail (`settings/src/index.ts:772-799`) is that it splits errors
in two:

```python
except Exception as exc:
    if getattr(exc, "code", None) == "INVARIANT":
        invariant_failure = invariant_failure or exc   # fatal, but AFTER every listener ran
        continue
    warn_listener_failure(registration.plugin, exc)     # contained
...
if invariant_failure is not None:
    raise invariant_failure
```

**A contract violation is fatal; an ordinary bug is contained.** That is a much better default than
"contain everything" (which hides real corruption) or "crash on everything" (which lets one bad plugin
take the process down). For Aleph, the natural `INVARIANT` class is: a ledger write that did not
happen, a claim written without a citation, a capability that provided a key it did not declare.

### 6.4 The `./invariant` companion — steal this whole

The single best idea in deepseek-harness, and it is aimed precisely at the failure `CLAUDE.md`
documents at length (*"the previous version of this file asserted invariants that were false in code
and CI enforcement that did not exist"*).

Every package publishes an `invariant` module registering **runtime** checks over its own event
streams or mutable data. **184 of 219 are deliberately empty**, and each empty one carries a comment
beginning `No runtime invariant:` with a package-specific explanation of why nothing is checkable —
e.g. *"this package exposes no independent event sequence or mutable data relation"*. A script
*"mechanically rejects generated markers, unexplained empty installers, non-empty installers that omit
or ignore the reporter, incorrect registration names, and incomplete export, publication, dependency,
or bundle wiring."*

This is **institutionalized honesty about coverage**. "We checked and there is nothing to check,
because —" becomes a first-class, mechanically-verified artifact. Aleph's `docs/acceptance.md` is the
nearest analogue but it is centralized; this is package-owned and it scales with the plugin count.

Pair it with hermes's rule: *"we deliberately do not mint capability ids without an enforcing gate"*
(`plugin_capabilities.py:33-36`). **Every capability in Aleph's registry should name the code that
refuses when it is absent, or it should not exist.**

---

## §7 — The guardrail (Q6)

> *refuse to remove load-bearing capability, without that refusal being trivially bypassable*

### 7.1 The mechanism Aleph already has is the right one

**Nobody else in the survey has this.** All four other systems will remove anything you ask them to;
their best story is dsh's graceful *parking*, which the reviewer correctly calls *"a graceful
degradation story, not a safety story"*. hermes's `plugins disable A` succeeds even when B declares
`requires_plugins: [A]`. prime-agent's only refusals in ~150k lines are 16 reserved keybindings and
"the base system prompt is not editable".

Aleph's design is better than a policy check, and the reason is worth stating precisely: **the primary
guard is not a check, it is the absence of an addressable name.** `PluginId` is minted only by
`register_dynamic`; manifest-mounted capabilities never receive one; `deactivate` accepts nothing else.
Deactivating core capability is not *refused after deliberation* — it is **unexpressible**, because no
argument value the agent can construct names it. `ProtectedCapability` exists only to catch a
kernel-internal mistake.

That is a capability-as-unforgeable-token design and it is not bypassable by argument, because there is
no argument to make. **Keep it exactly as it is.**

Layered on top, `blast_radius` (`support.py:93-112`) computes — as a *pure function*, changing nothing
— what else stops if you retire something. Because it is pure, showing it to an agent is free, and
`AgentPluginAPI.inspect` correctly precomputes it for every capability. That matters as much as the
refusal: *an agent that cannot see the blast radius before acting will keep proposing removals that get
refused, and a refusal it cannot predict is indistinguishable from a broken tool.*

### 7.2 Three holes, and how to close them

**Hole 1 — the name namespace is unguarded. This is the serious one.** §0.3 (E): a dynamic plugin
declaring `provides={"db.sessions"}` silently overwrites the core binding. The guardrail protects the
*capability graph* and leaves the *key namespace* wide open, so the agent never needs to deactivate
anything — it shadows.

The fix is structural, not a check, and hermes supplies the exact pattern. Their lazy-install directory
is *appended* to `sys.path`, never prepended — *"a package installed this way can only ADD new
importable modules; it can never shadow, downgrade, or break a module the core already ships… This is
the structural guarantee that a lazily installed package cannot brick Hermes"* (`lazy_deps.py:34-46`).

Generalised for Aleph, roughly ten lines:

```python
def put(self, realm, key, value, owner, *, owner_is_core: bool) -> None:
    held_by = self._owners.get((realm, key))
    if held_by is not None and self._core_owned.get((realm, key)) and not owner_is_core:
        raise ProtectedKey(key, held_by, owner)   # a dynamic plugin may never shadow core
    ...
```

Refuse at `register_dynamic` too — before setup runs — so the agent gets a load-time diagnostic naming
the conflict, not a mid-boot surprise. **An agent-authored plugin should be structurally incapable of
shadowing a protected-core key, rather than being checked for it.**

**Hole 2 — the blast radius is computed over the wrong set.** §0.3 (C): `support_set` walks every
mounted spec regardless of state, so a `DECLARED`, `PENDING` or `QUARANTINED` plugin is reported as
collateral. Filter to the ACTIVE set. This matters more than it sounds: a guardrail that over-refuses
trains the agent to reach for `force=True`, and a guardrail that is routinely forced has stopped being
one.

**Hole 3 — the guardrail is unreachable.** Nothing outside `packages/aleph-kernel` imports
`AgentPluginAPI`, `register_dynamic`, `ast_gate` or `spawn_ledger`. The only external importers are
`lifespan.py:32-33`, `arq.py:10-11` and `capabilities.py:33` — all boot path. (The single reference in
`scripts/_acceptance/kernel_boot.py:77` is an acceptance check, not the running app.)

This is `CLAUDE.md`'s own dominant defect class — a write path with no read path — applied to the
guardrail itself. And cordis's reviewer identifies the structural cause precisely: **cordis's dynamic
path is the only path.** Its own CLI is `ctx.plugin(Loader)` plus one config entry (`bin.js:1-16`); the
mechanism an agent would use *is* the mechanism that starts the process, so it cannot rot.

**Recommendation: boot Aleph's manifest through the dynamic path.** `mount_manifest` should call the
same `install` an agent calls, with `protected=true` and `core=true` set *by the loader*. Then every
boot exercises the agent-facing path, and §0.3 (A)–(E) would all have been caught by the existing
integration tests.

### 7.3 What `force` may and may not do

Keep the current asymmetry (`kernel.py:331-339`) — it is correct. `force` lets an operator accept
breaking their own plugins. It can never reach protected capability, and not because a check says so:
protected capability has no id to pass in the first place.

Add one thing: **`force` must be an operator verb, not an agent verb.** `AgentPluginAPI.disable`
currently takes `force: bool = False` and passes it straight through. An agent should be able to
*request* a forced removal and have it appear as an approval prompt, not perform one.

### 7.4 The honest limit

State it in the docs, in hermes's words (`plugin_capabilities.py:6-12`), because it is true of Aleph
too and pretending otherwise is worse than the gap:

> **This is NOT a sandbox.** In-process Python plugins remain trusted code — a malicious plugin can
> import anything, monkey-patch core, and ignore all of this. Capabilities govern the *host API
> surfaces* Hermes hands out and give the user an honest consent + audit trail. Actual isolation is a
> separate research track.

The kernel's guardrail is a **composition** guardrail: it governs what the *kernel* will do on request.
It is not a containment boundary against code that has already begun executing in-process. §8 is about
what to do with that fact.

---

## §8 — Agent-authored plugins (Q7)

### 8.1 Can static inspection ever be sufficient? No. Not partially, not with more rules.

This is the question that most deserves a straight answer, so: **no, and it cannot be fixed by making
the gate stricter.** Three independent reasons, in increasing order of finality.

**(a) The gate's own scope excludes everything that matters.** `ast_gate.py` is honest about this and
its docstring should be read as the specification it is: *"a module's top level must be
definition-only… **WHAT THIS IS NOT.** It is not a sandbox and not a capability check. A gated module
can still do anything Python can do once its functions are called."* The gate deliberately does not
analyse function bodies — that is the entire point of the rule. It buys exactly one property, and that
property is genuinely valuable: **loading is not running.** A plugin can be admitted, inspected and
rejected without its code ever having had a turn. Do not confuse that with safety.

**(b) Name-based analysis is defeated by three characters.** `_FORBIDDEN_IMPORTS` blocks `ctypes`,
`subprocess`, `pickle` and friends *at the top level*. Inside a function body — which the gate does not
look at — `__import__("subpro" + "cess")`, `getattr(__builtins__, "eval")`, or a lookup through any
object already in scope all reach the same place. This is not a bug in the implementation; any
syntactic gate over a language with dynamic attribute access has this property.

**(c) The general question is undecidable.** "Does this program do something harmful?" is a
non-trivial semantic property of program behaviour, and by Rice's theorem no analyser decides it for
all programs. Every practical gate is therefore a *heuristic*, and heuristics have a failure mode worse
than being wrong: hermes documents it. Their skill scanner, reused unmodified on plugins, flagged every
legitimate plugin — because plugins are *expected* to read their own API keys from the environment —
so they had to tune an exemption family (`plugin_guard.py:16-30`). **A gate calibrated on the wrong
artifact class flags everything and gets disabled within a week.** A gate that is turned off provides
zero protection and negative information, because everyone believes it is on.

**What static inspection *is* good for**, and why Aleph should keep the gate anyway:
- *loading is not running* — a real, useful, decidable property;
- a **compile-only precheck before an id exists**, so a syntax error is refused with nothing to roll
  back (deepseek-harness's `precheckCode`, `sandbox.ts:206-214`);
- **teaching errors.** dsh's precheck surfaces the offending line and caret and detects the two
  mistakes a model actually makes — a TypeScript `as` annotation, and `});` closing a call that was
  never opened. Aleph's gate should report the caret line, not a class name.

Everything else requires real isolation.

### 8.2 The three-tier trust model

Draw the boundary explicitly. Aleph is unusually well-placed here because `apps/code-runner` already
exists and is a *real* boundary — not a `vm` façade, not an interpreter, but a container with
`cap_drop: ALL`, a read-only rootfs, a Redis-only internal network with no route to Postgres or the
API, `python -I`, a socket guard, `RLIMIT_CPU`/`RLIMIT_FSIZE`, and a pinned audited dependency set.

```
┌─ TIER 0 ── DECLARATIVE ────────────────────────────────────────────────────┐
│  manifest + config + prompt/skill text + registry contributions built      │
│  from declared data. NO agent-authored Python executes in-process.         │
│                                                                            │
│  Trust: safe by construction — there is no code.                          │
│  Gate:  schema validation.                                                 │
│  → THE DEFAULT for agent-authored capability.                             │
└────────────────────────────────────────────────────────────────────────────┘
┌─ TIER 1 ── SANDBOX-BODIED ─────────────────────────────────────────────────┐
│  A Tier-0 manifest whose capability's *implementation* is an RPC into      │
│  code-runner. The in-process part is a kernel-authored shim the agent      │
│  never writes. Agent code runs where it is already contained.              │
│                                                                            │
│  Trust: bounded by the container. CPU/mem within caps, no credentials,     │
│         no network beyond Redis, no route to Postgres.                     │
│  Gate:  AST gate + budgets + the probe.                                    │
│  → THE DEFAULT for agent-authored COMPUTE.                                 │
└────────────────────────────────────────────────────────────────────────────┘
┌─ TIER 2 ── IN-PROCESS ─────────────────────────────────────────────────────┐
│  Agent-authored Python running in the API/worker process.                  │
│                                                                            │
│  Trust: NONE. Equivalent to shell access. Say so in those words.          │
│  Gate:  AST gate + disposable-scope trial + HUMAN APPROVAL, per version.  │
│  → RARE, and never automatic.                                             │
└────────────────────────────────────────────────────────────────────────────┘
```

Tier 1 is the recommendation that distinguishes Aleph. deepseek-harness ships the most complete
agent-authored-plugin path in the survey and is admirably honest that *"the vm sandbox isolates globals
but is not a security boundary… **Treat a dynamic package like bash access**"* — with the consequence
that enabling their preset means treating the whole session as shell access. Its own reviewer draws
the conclusion: *"If Aleph wants agent-authored plugins to be safer than bash rather than equivalent to
it, the boundary has to be a process/container with a capability-mediated syscall surface, not a `vm`
façade."*

**Aleph already built that container. Route agent-authored plugin bodies through it.**

### 8.3 The full path, end to end

```
  1. INSPECT        agent reads the live capability/registry catalog — GENERATED
                    from the same declarations the runtime binds, never hand-written
                         │   (dsh: cordis_inspect_list / _query / _self)
                         ▼
  2. AUTHOR         agent writes plugin.toml + body. Tier declared in the manifest.
                         │
                         ▼
  3. PRECHECK       parse-only. Refused BEFORE an id exists → nothing to roll back.
                    Errors carry the caret line.        (dsh sandbox.ts:206-214)
                         │
                         ▼
  4. GATE           AST gate: top level is definition-only. Loading is not running.
                    Honest label: an admission filter, not a boundary.
                         │
                         ▼
  5. TRIAL          Mount in a DISPOSABLE kernel scope: scratch project_id, network
                    denied, real setup(), observe which capabilities it CLAIMS,
                    then unwind via the effect scope.   (hermes plugin_dev.py:34-60)
                         │
                         │  ── "don't try to prove it safe statically; run it in a
                         │      disposable scope and see what it claims"
                         ▼
  6. CONSENT        capability set hashed (sha256 over the sorted set). Stored
                    consent = what the human actually saw. A later version
                    declaring MORE leaves the additions ungranted until re-consent.
                                                  (hermes plugin_capabilities.py:181)
                    Tier 2 additionally requires explicit human approval, per version.
                         │
                         ▼
  7. INSTALL        register_dynamic → activate → PROBE against the live system.
                    Fail ⇒ full unwind + a diagnosis. Immutable version id;
                    changing code means a NEW version, never an overwrite.
                         │
                         ▼
  8. PROBATION      re-probe on a schedule; auto-quarantine on failure. Budget and
                    lineage recorded in the hash-chained ActionLedger — not a side
                    structure. Rollback by version id.
```

Steps 5 and 6 are the two Aleph is missing and both are cheap given what already exists.

**Step 5** is nearly free: Aleph already has scoped capabilities, a disposable `EffectScope`, and LIFO
unwind. Mounting a candidate in a throwaway scope and observing what it claims is the second filter the
AST gate cannot be. hermes's version denies network by patching, creates a temp home, runs `register()`
for real, and unwinds the ledger.

**Step 6 (consent hashing)** is the mechanism for a failure that only exists once an agent is the
author: **capability drift on revision.** v1 declares `requires=["http.gateway"]`; a human approves;
v2 quietly adds `["db.sessions"]`. Hashing the declared set makes catching it mechanical rather than
dependent on someone re-reading a diff. Log an `evidence` field on every capability decision naming
*why* it was allowed.

**One thing to steal from the model-facing side and one to refuse.** Steal dsh's inspect protocol —
the agent must be able to query the live catalog before writing code. Refuse dsh's implementation of
it: `tool-cordis/src/api-catalog.ts` is **4,761 hand-maintained lines** describing other packages'
APIs to the model — a second copy of facts that live in the source. Aleph already does this right for
A2UI (`scripts/gen_catalog.py`, with `check-catalog-generated.sh` failing on drift); extend the same
pattern to the kernel's capability surface.

### 8.4 Bounded self-modification

prime-agent's `/refine` (`refinement.ts:135-151, 707-833`) is the right shape and Aleph can beat it:
supplemental state only, **the base system prompt is immutable**, every edit records before/after
snapshots, history is append-only, any refinement is revertible by id.

Aleph's advantage is that it already has a **hash-chained, append-only `ActionLedgerEvent`**.
prime-agent keeps refinement history in a side JSONL; Aleph should put plugin authorship, capability
grants, consent hashes, spawn lineage and budget in the ledger, where it is tamper-evident and joins
the rest of the audit trail. This is also the argument for deleting `spawn_ledger.py` (§9.3).

---

## §9 — Keep, change, delete (Q8)

### 9.1 Keep — and do not let a redesign erode these

| what | where | why |
|---|---|---|
| `EffectScope.drive` / `unwind` | `effects.py:70-113` | Guards **every** iteration step (cordis guards only its async branch, `fiber.ts:248-268`); aggregates disposal failures into an `ExceptionGroup` rather than logging. Genuinely better than the reference implementation. |
| Absolute declared-access refusal | `context.py:101-114` | cordis lets a plugin reach an ancestor's provisions undeclared. Aleph's refusal is the only reason `requires` is a boundary rather than a comment. |
| Resolution returns the raw object | `context.py:89-114` | Measured: hoisting costs **zero** (10.6 ns vs 10.5 ns direct). cordis's proxy-per-access cannot be hoisted away. Do not trade this for tracing. |
| **Mandatory probe** | `spec.py:74-111`, `kernel.py:214-231` | **Nothing else in the survey has this.** No default, `__post_init__` rejects a non-callable, activation rolls back on failure. It structurally targets `CLAUDE.md`'s dominant defect — a write path with no read path. The docstring's warning that *"a probe that cannot fail is worse than no probe"* is the right standard. |
| **`support_set` / `blast_radius`** | `support.py` | **Nothing else in the survey has this.** A pure function answering "what stops if this goes away" before anything is torn down. The load-bearing invention. |
| **`PluginId` as an unforgeable token** | `kernel.py:41-49` | Better than a policy check: core is *unnameable*, not refused. |
| Manifest with no directory-scan fallback | `manifest.py:18-22` | The reasoning is exactly right — a scan hands anything that can write a file the ability to mount code at core trust. Do not add a scan later. opencode's `.opencode/plugin/*.ts` glob is the counter-example. |
| `reprobe` | `kernel.py:289-315` | Catches decay, not just birth defects. No analogue anywhere in the survey. |
| Rollback-on-failed-replace | `kernel.py:276-283` | *"THE VALUABLE PROPERTY IS THE ROLLBACK, not the swap."* Correct, and better than every hot-reload in the survey. |

### 9.2 Change

Ordered by severity. The first three are the §0.3 findings.

**1 — `Store.put` must refuse to shadow a core-owned key.** (§0.3 E, §7.2.) A live privilege
escalation that nullifies the headline guardrail. ~10 lines in `context.py` plus a refusal in
`register_dynamic` before setup runs. **Do this first.**

**2 — Quarantine, and release the name.** (§0.3 A/B.) `FAILED` is a permanent name grave, so the
agent's fix-and-retry loop cannot close. Add `unregister`, and make `boot()` quarantine non-protected
capabilities rather than aborting.

**3 — Compute `blast_radius` over the ACTIVE set.** (§0.3 C.) A one-line filter in `_specs()` or a
parameter to `support_set`. Over-refusal trains the agent to force.

**4 — Assert `provides` after setup.** (§0.3 D.) Three lines in `_activate`.

**5 — Reactive rebuild on provider change.** (§5.2.) `replace()` leaves dependents holding stale
handles. Add the epoch string (`fiber.ts:385-397`) and the notify/refresh pair. cordis's reviewer:
*"a real defect, not a simplification."*

**6 — Per-plugin inertia lock instead of the process-wide `asyncio.Lock`.** (§5.3.)

**7 — Provider-removal ordering.** Drop from the store → notify → await dependents' teardown → drop
your own handle (`reflect.ts:195-201`). Aleph's `Store.drop` has no such barrier.

**8 — Add the `DRAINING` phase with a bounded deadline, and refuse a swap that cannot quiesce.**
(§5.4.) The field-wide gap; available as a differentiator.

**9 — Boot the manifest through the dynamic path.** (§7.2 Hole 3.) The only durable fix for
"the agent-facing half is unreachable" is to make it the half that boots the process.

**10 — Typed config schema.** `ManifestEntry.config: dict[str, Any] | None` → a validated schema, so a
model can read it in order to author one.

**11 — Labelled effects.** `EffectScope` stores bare callables. cordis labels every effect
(`ctx.provide("db")`, `ctx.on("custom-event")`) and nests them, so an operator can ask a live plugin
*"what have you done to the world"*. Nearly free; gives an inspectable teardown plan.

**12 — Precompute callback signatures at registration.** Hermes calls `inspect.signature()` on every
hook invocation (`plugins.py:5044-5069`) with no cache — tens of microseconds of pure reflection per
call. The *idea* (payloads evolve additively; old callbacks receive only what they declare) is good;
do the reflection once and store the accepted-kwargs frozenset on the registration.

**13 — Propagate the capability context across every concurrency boundary through one audited
helper.** Hermes documents two security bugs caused by a `ContextVar` silently emptying in a worker
thread — approvals fell through to auto-approve (`thread_context.py:1-31`). Aleph's scoped capability
access has the identical hazard the moment work fans out to a thread or a task group.

**14 — Write `packages/aleph-kernel/NOTICE`.** There is none today, while `effects.py:20-22`,
`context.py:8-12` and `support.py:14-17` cite cordis and the paper by file and line.
`packages/aleph-belief/NOTICE` is the house pattern.

### 9.3 Delete

Unsentimentally. Every item below is a write path with no read path — the exact class `CLAUDE.md`
names as the dominant defect.

**`spawn_ledger.py` (198 lines) — delete outright.** In-memory, per-supervisor, zero importers outside
its own tests. Its three brakes (depth, fan-out, budget-deducted-from-parent) are genuinely good ideas
and the budget one is the only brake that really bounds cost — but they belong in the **hash-chained
`ActionLedgerEvent`**, which is durable, tamper-evident, joins the cost ledger, and is already the
house mechanism. Its own docstring concedes it: *"Durable lineage belongs in the ActionLedger; this is
the live structure the supervisor enforces against."* There is no supervisor. Move the three checks to
the service that spawns agents and delete the module. Acceptance D4 currently passes against a
structure nothing uses.

**`skills.py::skill_capability` (the wrapper, ~40 lines) — delete.** A skill is not a service with a
lifetime; it is a **contribution to a registry** (§2.3). Wrapping it as a bespoke `CapabilitySpec` that
provides `skill.<name>` gives it a probe that regex-scans the instructions for
`` `helper(` `` and compares against a hardcoded `_PYTHON_BUILTINS` set of ten names — a heuristic that
will produce false rejections the first time an instruction mentions `sorted(` or `enumerate(`.
**Keep** `load_skill` / `skill_from_source` — namespace-`exec` instead of `sys.modules` import is
exactly right (see §10.4) — and have them produce a contribution to `Registry.SKILLS` instead.

**`AgentPluginAPI.check_health` — delete the method.** Probation as something the agent calls on
itself. Probation must be a scheduled kernel task, not an agent verb. The docstring already worries
about the DoS surface of letting an untrusted loop trigger live reads; the answer is that it should not
be on that surface at all.

**The `force` parameter on `AgentPluginAPI.disable` — delete from the agent surface.** Keep it on
`Kernel.deactivate` for operators. (§7.3.)

**The claim that the AST gate is part of the trust model — delete from the docs.** Keep the code; it
buys "loading is not running", which is real. `ast_gate.py:18-23` is already honest; make sure
`acceptance.md` D1 and any prose that cites it are equally honest, because D1 currently reads as
though gating were the security story.

**`testing.py` — keep, but it is the only kernel module with no design debt, so it needs no work.**

---

## §10 — The language decision (Q9)

### 10.1 Recommendation: Python. Keep it. Do not rewrite the kernel.

This is not a default-to-the-status-quo answer; it follows from the measured workload, and I would
give the same answer on a greenfield.

### 10.2 The argument from the workload

**The kernel is not on any hot path, and the numbers are not close.** §1.1: kernel mediation costs
**105 ns**, and **zero** if you hoist. The cheapest real operation the kernel mediates — a Postgres
round-trip — is ~2,000–5,000× that. One LLM output token is ~100,000× that. Aleph's workload is
LLM-bound orchestration over Postgres. A language whose function-call overhead is 10× lower would
change total latency by an amount that cannot be measured over network jitter.

**The "hot retrieval loops" are not in Python.** `search_corpus` fuses a pgvector cosine ranking and a
`ts_rank` lexical ranking with RRF at k=60. The dense and lexical rankings execute **inside Postgres**;
the RRF fusion is a merge over two short ranked lists. Python is the orchestrator, not the inner loop.
If a genuine CPU-bound loop appears later — a local reranker, a tokenizer — the answer is deepseek-
harness's *"escape to threads and processes at named seams"*: a native extension or an out-of-process
worker **for that loop**, not a kernel rewrite. Aleph already has the seam (`code-runner`, arq
workers).

### 10.3 The argument from the plugin boundary

This is the decisive one, and it is the strongest cross-codebase agreement in the whole survey.

All five reviewed systems keep plugins **in-process, with nothing serialized**, and all five reviewers
identify that as the reason the model is affordable. Measured: a JSON round-trip of a *small* dict is
1,124 ns — **107× a direct call**, and 10× the kernel's whole mediation cost. Cross a process boundary
per plugin call and you have built exactly the slow system the owner is worried about.

Now: **the research plugins are bound to Postgres and Python's scientific stack.** asyncpg,
SQLAlchemy, pgvector, numpy, pandas, matplotlib, altair — the pinned audited stack is right there in
`apps/code-runner/Dockerfile`. Those do not move. So a non-Python kernel forces one of two things:

```
  Rust/Go/TS kernel + Python plugins
        │
        ├── via IPC ──────────► ~1,124 ns per small payload, and it DEFEATS
        │                        "pass handles, not payloads" — the one rule
        │                        every reviewed system agrees on. Fatal.
        │
        └── via PyO3/FFI ─────► works, costs a build toolchain, a second type
                                 system, and a debugging story — and buys a
                                 105 ns saving on a path that runs at LLM speed.
```

Neither trade makes sense. And the thing a systems language would *seem* to buy — real isolation for
agent-authored code — **it does not buy**. Isolation comes from the OS: a container, dropped
capabilities, seccomp/Landlock, a partitioned network. Aleph already has that in `code-runner`. A Rust
kernel with an in-process agent-authored plugin is exactly as unsafe as a Python one.

### 10.4 The three honest objections

**"Python has the GIL."** The workload is async I/O, not parallel CPU. Free-threaded 3.13 exists but
is not needed. The reviewed systems reach the same conclusion from the other side: cordis, dsh,
opencode and prime-agent are all single-threaded cooperative event loops and none of them found that
limiting — they escape to threads/processes at named seams, which Aleph already does.

**"Hot code reload is harder in Python than in Node."** True, and it is the one real cost. But note
what it took cordis to do it in Node: `requireBuiltin('internal/modules/esm/loader')`, two
hand-maintained shims for Node 22 vs 24, and manual surgery on both the ESM `loadCache` and the CJS
`require.cache` — a standing maintenance liability its own reviewer flags. Hermes shows the Python
version of the problem is tractable (`sys.modules` hygiene: evict the package *and every submodule*
before re-exec, and again if `exec_module` raises — `plugins.py:4988-5017`, `5568-5599`) but ugly.

**Aleph should sidestep it entirely.** `skills.py` already does the right thing and should become the
rule for all plugin bodies: `exec` into a **fresh namespace dict**, never `import` into `sys.modules`.
Then a plugin cannot shadow a real module, two generations coexist, and unloading is dropping a
reference. Python's `importlib` is public API, unlike Node's module internals — but the better answer
is not to need it.

**"A gradual type system is weaker than a real one."** Aleph runs pyright strict at 0 errors, which is
stronger in practice than most TypeScript codebases (which do not enable `strict` everywhere and lean
on `any`). deepseek-harness's actual rule is a *policy*, not a language feature, and it transfers
directly: *"Trust the type checker at typed same-process boundaries. Do not add runtime validation for
values the static interface requires; validate at parser/config, queued, model/tool JSON,
durable/file, worker, process, and wire boundaries."* Seven named validation boundaries; everything
between them is a plain typed call. **Write Aleph's list down** — HTTP/wire, DB/durable,
model-tool JSON, config/manifest, subprocess, sandbox, queue — and stop defending in between. That is
the single biggest lever on plugin tax in a typed system and it costs nothing to adopt.

### 10.5 The one place a second language is correct

`apps/web` and `apps/copilot-runtime` are TypeScript and should stay. That is not a second plugin
plane — it is a **client**. The rule is *one plugin identity per process*, not one language in the
repo. prime-agent's failure was two plugin planes **inside one product** with opposite composability
rules; a browser talking HTTP/SSE to a Python kernel is not that.

If plugin-contributed UI is ever wanted, the answer is not a second plugin runtime (opencode has one
and it re-implements scoping, activation, install and teardown with *different* semantics). It is a
Tier-0 declarative contribution to a UI registry, rendered by the existing A2UI catalog — which is
already generated from one editable source with a drift check.

### 10.6 Verdict

> **Python 3.13 for the kernel and for every plugin plane in the server process.** The kernel is
> ~4,000 lines mediating operations that cost 1,000–100,000× what the mediation costs; its language is
> not a performance variable. The plugin boundary must be an in-process call with nothing serialized,
> and the research suite is immovably bound to Postgres and Python's scientific stack — so any other
> kernel language either imposes a serialization boundary (fatal) or an FFI boundary (all cost, no
> benefit). Isolation, the one thing worth changing languages for, comes from the OS and Aleph already
> has it. Escape to another language at a **named seam** — a native extension for a proven CPU hot
> loop — never for the kernel.
>
> D5 can be closed.

---

## §11 — Sequencing

Ordered by (risk closed) ÷ (effort). Each item names the acceptance check that should carry it.

**Now — the guardrail is not currently a guardrail.**
1. `Store.put` refuses to shadow a core-owned key; `register_dynamic` refuses a conflicting
   `provides` before setup. *(§0.3 E — a live escalation. Test: a dynamic plugin declaring
   `provides={"db.sessions"}` is refused at registration, and the core binding is unchanged.)*
2. `blast_radius` over the ACTIVE set. *(§0.3 C. Test: an inactive dependent is not reported as
   collateral.)*
3. Assert `provides` after setup. *(§0.3 D. Test: the "liar" capability fails to activate.)*
4. `unregister` + quarantine, so retry works. *(§0.3 B. Test: install-bad → fix → install-same-name
   succeeds.)*

**Next — make the guarded path the only path.**
5. Boot the manifest through `install` with `protected`/`core` set by the loader. Every boot then
   exercises the agent-facing path. *(§7.2. This is what stops steps 1–4 from regressing.)*
6. Quarantine on plugin load failure; abort only for protected core. Boot audit names unresolved
   services. *(§6.2.)*
7. Deadline budgets on every plugin callback; observers queued, never awaited. *(§6.2, §5.4.)*

**Then — the composition model the thesis needs.**
8. `@p.contributes` + registries-as-capabilities + `@p.declares_registry`. *(§2, §4.3. The largest
   single piece of work here, and the one that makes "everything is a plugin" true.)*
9. Epoch + per-plugin inertia lock + reactive rebuild + provider-removal ordering. *(§5.2, §5.3.)*
10. `DRAINING` with a bounded deadline; identity tokens on registrations; stale-handle invalidation.
    *(§5.4 — the field-wide gap.)*

**Then — the agent-facing loop.**
11. Tier 0 declarative plugins end to end: author → precheck → trial in a disposable scope → consent
    hash → install → probe → probation in the ledger. *(§8.3.)*
12. Tier 1: route agent-authored bodies to `code-runner`; make it persistent across turns and expose
    Aleph's typed services as importable modules inside it. *(§1.4, §8.2 — this is simultaneously the
    isolation answer and the context-cost answer.)*
13. Generated model-facing capability catalog, with a drift check, following `scripts/gen_catalog.py`.
    *(§8.3 — never hand-maintain it.)*

**Continuously.**
14. Per-package `invariant` modules with mechanically-verified explained emptiness. *(§6.4.)*
15. `scripts/bench.sh` with the "the measurement is in the comment that sets the constant" discipline.
    The composability claim needs numbers the way the retrieval claim has recall@1 = 0.91.
16. Delete `spawn_ledger.py`, `skill_capability`, `check_health`, and the agent-facing `force`. *(§9.3.)*
17. Write `packages/aleph-kernel/NOTICE`.

---

## Appendix A — vocabulary

Defined once, in the order they first matter.

- **capability** — a named service with a lifetime, declared with what it needs (`requires`), what it
  publishes (`provides`), how to set itself up, and how to prove it works.
- **effect / inverse** — a change to the world paired with the function that undoes it. Registered
  together, on adjacent lines, so a write path cannot be authored without its undo.
- **coeffect** — what a computation *requires from its environment*, as opposed to what it does to it.
  Aleph's `requires` is a coeffect declaration; the kernel enforces it at access time.
- **probe** — a function that exercises a capability's **read** path against the live system after
  setup. Mandatory in Aleph. A probe that cannot fail is worse than no probe.
- **realm** — a namespace for a service binding, so two projects can resolve one key to different
  objects. Aleph falls back to the root realm, making isolation additive.
- **support set** — the set of capabilities still satisfiable after some are retired, computed as a
  greatest fixed point. **Blast radius** is the difference before and after.
- **registry** — a named piece of derived state (the tool catalog, the connector table) rebuilt by
  replaying ordered contributions.
- **contribution / transform / draft** — a plugin's edit to a registry's draft. Removing the plugin
  drops the transform and replays; there is no inverse.
- **epoch** — a string naming the exact *instance identities* of a plugin's dependencies. When it
  changes, the plugin rebuilds. Detects a swapped provider, which presence-checking cannot.
- **inertia lock** — one in-flight transition per plugin. A change arriving mid-transition sets the
  target; the running transition re-checks the epoch when it finishes.
- **quiesce** — wait until in-flight work has actually stopped, as opposed to having been asked to.
- **fiber** — cordis's word for one live instance of a plugin. Aleph has no equivalent term; a mounted
  capability is the nearest thing.
- **parking** — a dependent returning to `PENDING` when its provider disappears, rather than crashing.
  A graceful-degradation story, not a safety story.

---

## Appendix B — licences

All five reviewed projects are **MIT**. Ideas are free to reimplement; verbatim copying carries the
notice. Per Aleph's standing constraint: **reimplement, never vendor**, and anything ported carries a
`NOTICE` with upstream, licence and per-file lineage.

| project | holder | note |
|---|---|---|
| cordis | © 2021-present Shigma | `4.0.0-rc.8`; its README warns the API is unstable. |
| deepseek-harness | © 2026 DeepSeek | **`vendor/*` is rescoped upstream Cordis, not DeepSeek's code** — copying from it copies upstream under its own authorship. `native/landlock-run` has a separate `LICENSE`; `THIRD_PARTY_NOTICES.md` is 16KB. |
| prime-agent | © 2025 Mario Zechner; © 2026 Prime Intellect | fork of `pi`; still published as `@earendil-works/pi-*`. |
| opencode | © 2025 opencode | 17 `patchedDependencies` against `@ai-sdk/*`, `effect`, `@modelcontextprotocol/sdk` — behaviours depend on upstream forks that will not reproduce from calling code alone. |
| hermes-agent | © 2025 Nous Research | `plugins/` wraps third-party SDKs and is **not** uniformly MIT-clean for redistribution. |

**Unsafe to port verbatim regardless of licence:**

- cordis `loader/src/config/utils.ts:4-8` — `new Function('ctx','expr','with (ctx) { return eval(expr) }')`
  plus a `!!js` YAML tag makes config files arbitrary code. Aleph's TOML manifest naming
  `module:callable` factories, with no interpolation and no directory scan, is the right posture.
  **Do not add expression interpolation later.**
- cordis `loader/src/internal.ts:96-122` and `hmr/src/index.ts:290-318` — Node private internals and
  module-cache surgery, with version sniffing between Node 22 and 24.
- cordis `core/src/utils.ts:110-217` — the shadow/traceable Proxy machinery. `Proxy`-specific, and the
  thing to redesign rather than translate.
- opencode `packages/codemode/src/interpreter/runtime.ts` — a 3,465-line hand-written JS interpreter.
  A bad reimplementation is a security hole; Aleph's container is the better boundary anyway.
- Anything under deepseek-harness `vendor/` — see above.

**Aleph obligations outstanding:** `packages/aleph-kernel` has **no `NOTICE`** while `effects.py`,
`context.py` and `support.py` cite cordis and the paper by file and line. Add one, following
`packages/aleph-belief/NOTICE`.
