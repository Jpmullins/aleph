# The Generative UI Spectrum — and what it means for per-plugin catalogs

Research pass, 20 Aug 2026. Area: **the UI side**. Read-only; nothing in the tree was changed.

Sources, in order of authority: the official CopilotKit skills installed at
`.agents/skills/` (react-core, a2ui-renderer, runtime — pinned at library version **1.68.2**),
the CopilotKit source itself (via the `copilotkit-mcp` code search), the packages installed in this
repo's `node_modules`, `docs.copilotkit.ai`, and the two marketing pages the owner cited.

---

## In one paragraph

"Generative UI" means letting the agent decide what appears on screen. There are four ways to do
that, and they trade the same thing off against each other: **how much you decide in advance versus
how much the agent invents**. At one end you write a React component and the agent only picks it and
fills in the data — perfectly predictable, but you had to build it. At the other end the agent writes
raw HTML and you drop it in a locked-down iframe — infinitely flexible, slower, more expensive, and
sometimes wrong. In between sit two middle options: the agent assembles a layout out of a **catalog**
of building blocks you registered (this is A2UI, the one Aleph already uses), or a third party ships
both the tool *and* its interface and you sandbox the whole thing (MCP Apps). The decisive question
for Aleph — *can each plugin publish its own catalog, and can catalogs be added and removed while the
app is running?* — has a clear answer: **yes, and it is a build, not a rewrite.** The underlying A2UI
protocol was designed for many catalogs at once, Aleph's own right-hand panel already goes through
the multi-catalog code path, and CopilotKit already ships a per-component enable/disable switch that
takes a component out of both the picture *and* the agent's prompt in one move. The one genuine
limit is physical, not architectural: a plugin's *renderers* are React code, and React code has to be
in the browser bundle. Everything above that line is data and can move at runtime.

---

## 0. Three corrections to the brief before we start

The task brief carried three factual claims about Aleph that the tree does not support. They matter,
because two of them make the problem look harder than it is.

**"21 hand-written A2UI card components, and a hand-rolled catalog.json that is NOT actually an A2UI
catalog."** Half right. Aleph *does* build a real, spec-conformant A2UI catalog. In
`apps/web/src/a2ui/aleph-catalog-v09.tsx` each card is registered with `createComponentImplementation`
from `@a2ui/react/v0_9`, its props typed with `CommonSchemas.DynamicString` / `DynamicNumber` /
`Action` (so a prop can be a literal *or* a `{path: "/x/y"}` binding into the surface's data model),
and `apps/web/src/a2ui/A2UISurfaceView.tsx::buildAlephCatalog()` assembles them into
`new Catalog("aleph://v1", [...ALEPH_CARD_IMPLS, ...basicCatalog.components.values()], [])`. That is
exactly what `createCatalog()` produces internally. Aleph did not build a lookalike; it built the
real thing by hand.

**But** `packages/aleph-a2ui/src/aleph_a2ui/catalog.json` is a *separate* artifact — JSON Schema per
component, plus `primitives` and `actions` — from which `scripts/gen_catalog.py` writes two files:
`apps/web/src/a2ui/catalog.ts` (name lists, for TypeScript) and
`apps/copilot-runtime/src/catalog.generated.ts` (the JSON handed to the agent as `a2ui.schema`).
So Aleph maintains **two independent descriptions of the same components**: the zod3 schemas that
actually render, and the JSON Schema the agent is shown. `check-catalog-generated.sh` pins the second
to its source; nothing pins the two to *each other*. That seam is exactly what CopilotKit's
`extractCatalogComponentSchemas(catalog)` removes — it derives the agent-facing schema *from* the
rendering catalog, so there is one source and no drift to police. This is the single highest-value,
lowest-risk change in this whole document.

**"Aleph has `@copilotkit/a2ui-renderer` 1.58.0 installed — is Aleph using it?"** It is installed
*transitively* (pulled in by `@copilotkit/react-core`); `apps/web` does not list or import it
directly. But Aleph **is** using it, indirectly and in the most important place:
`apps/web/src/lib/copilot.tsx` calls `createA2UIMessageRenderer({ theme, catalog: buildAlephCatalog() })`
from `@copilotkit/react-core/v2`, and that function's whole body lives in `a2ui-renderer`
(`A2UIProvider`, `A2UIRenderer`, `initializeDefaultCatalog`, `injectStyles`). So the chat pane's
generative-UI rendering is CopilotKit's, running Aleph's catalog. What Aleph is *not* using from that
package is its authoring side: `createCatalog`, `extractSchema`, `extractCatalogComponentSchemas`,
`buildCatalogContextValue`, `filterCatalog`, `getCustomComponentNames`.

**"The browser-facing protocol is just five documented HTTP routes."** From the UI side this is the
claim I'd push back on hardest, and section 6 explains why: the `/info` handshake is not a route, it
is a **capability negotiation**, and at least six front-end features silently do nothing without it.

---

## 1. The four bands, precisely

The marketing page names four bands; the docs concept page (`/concepts/generative-ui-overview`) names
three and folds MCP Apps into "Open-Ended". Both are describing the same set of primitives. Here is
the merged, verified picture.

| | **Controlled** | **Declarative** | **MCP Apps** | **Open-Ended** |
|---|---|---|---|---|
| Who authors the pixels | You | You (the pieces); agent (the arrangement) | A third party | The model, per response |
| API | `useComponent()` / `useRenderTool()` | `a2ui={{ catalog }}` + `createCatalog()` | `CopilotRuntime({ mcpApps: { servers } })` | `openGenerativeUI={{ … }}` |
| What crosses the wire | a tool call with typed args | A2UI operations (`createSurface` / `updateComponents` / `updateDataModel`) | an activity carrying a URL + an iframe handshake | an activity carrying `css`, `html[]`, `jsFunctions`, `jsExpressions[]` |
| Rendered by | your React component | your catalog renderers, via the A2UI binder | a sandboxed `<iframe>` | a sandboxed `<iframe>` (`@jetbrains/websandbox`) |
| Predictability | total | high — the agent can only name components you registered | none (you didn't write it) | none |
| Latency / cost | one tool call | one tool call; **dynamic-schema mode adds a second LLM** to invent the layout | network round-trips to the MCP server | slowest and most expensive — the model emits a whole document |
| Needs the Node runtime? | no | no (client catalog alone activates it in 1.68) | **yes** | no (the tool is registered client-side) |
| What only it can do | pixel-exact brand surfaces; anything needing real app state, focus, virtualised lists | novel *arrangements* of trusted pieces, with two-way data binding, without shipping code | let a third party ship tool **and** UI together, with zero code in your app | invent a visual form nobody anticipated (a bespoke chart, a one-off mini-app) |

### 1.1 Controlled — `useComponent` / `useRenderTool`

`useComponent` is a thin wrapper over `useFrontendTool`
(`packages/react-core/src/v2/hooks/use-component.tsx`): it registers a **new tool** whose only job is
to render, and auto-prefixes the description with *"Use this tool to display the "X" component in the
chat."* The agent calls it; your component receives the parsed args as props.

The trap the maintainers call out twice: `useComponent` **creates** a tool, it does not decorate an
existing one. If you already have `useFrontendTool({name: "search"})` and then write
`useComponent({name: "search"})`, you get two tools colliding and a console warning. To skin an
existing tool, use `useRenderTool`. To skin *every* un-skinned tool, use `useDefaultRenderTool` —
which, notably, is **off by default**: `use-render-tool-call.tsx` refuses to auto-paint a fallback
card because doing so would "leak internal tool names plus raw args/result JSON into every app's
chat in production."

Status values are `"inProgress" | "executing" | "complete"` (camelCase; hyphenated variants silently
never match), and during `inProgress` the args are `Partial<T>` because they are still streaming.

**What Aleph would get:** Aleph has 21 React card views already sitting in
`apps/web/src/a2ui/components/`. Every one of them could *also* be a Controlled component with about
four lines of glue, giving the agent a way to render a card directly without going through A2UI at
all. More useful: Aleph's *chrome* — the pipeline strip, the pane layout, the rail — is a place the
agent currently cannot reach; `useComponent` is the cheap way to let it.

### 1.2 Declarative — A2UI catalogs

This is Aleph's existing band. The agent emits A2UI operations naming components by string; the
client resolves those names against a registered catalog and paints. Two flavours:

- **Fixed schema** — the surface shape is known ahead of time. Cheaper, faster, deterministic.
- **Dynamic schema** — a *secondary* LLM invents the component tree and the data together, at
  request time. This is what `injectA2UITool` / `get_a2ui_tools` turn on. Docs: *"a secondary LLM
  generates the entire UI (schema, data, and layout) based on the conversation context."* The
  middleware waits for the complete `components` array before painting anything, then streams
  `updateDataModel` per item so cards appear one by one.

The cost is explicit in the docs: *"If the surface is well-known (e.g. a product card, a flight
result), prefer a fixed schema; it's faster, cheaper, and the UI is deterministic."* Aleph's runtime
currently runs `injectA2UITool: true` — i.e. the expensive flavour — for every surface, including the
ones whose shape is perfectly well known.

### 1.3 MCP Apps — the band Aleph cannot have without the Node service

An MCP server exposes a tool *and* a UI resource for it. When the agent calls the tool, CopilotKit
fetches the UI and renders it in a sandboxed iframe. From the docs: *"Zero frontend code — UI
components are served by the MCP server… Thread persistence — MCP Apps are stored in conversation
history and restored on reconnect."*

Two things to be precise about, because the marketing page is misleading here:

1. The page shows `<CopilotKit MCPApps={["excalidraw.mcp.com", …]}>`. **That prop does not exist** in
   the 1.68 `CopilotKitProviderProps` interface. The real configuration is server-side:
   `new CopilotRuntime({ mcpApps: { servers: [{ type: "http", url, serverId, agentId? }] } })`, and
   the runtime auto-applies the middleware to every registered agent. The client only ever sees the
   built-in `MCPAppsActivityRenderer`, which the provider registers unconditionally.
2. **It is Node-only.** I checked the LangGraph integration page specifically: the Python agent has
   no MCP-specific code at all; the Node runtime does the server connection, tool discovery, activity
   emission, UI-resource fetching, and iframe rendering. A Python LangGraph agent *requires* a Node
   `CopilotRuntime` for MCP Apps.

**This is the load-bearing fact for the deletion question, from my area.** MCP Apps is the band where
a third party ships an ability and its interface together and the host app writes no code. That is
the closest thing in the entire ecosystem to the owner's plugin thesis, and it is gated behind
`apps/copilot-runtime`.

### 1.4 Open-Ended — `openGenerativeUI`

The provider registers a client-side frontend tool called `generateSandboxedUi` whose parameters are
`{ initialHeight, placeholderMessages, css, html, jsFunctions, jsExpressions[] }`, and an activity
renderer that pipes them into `@jetbrains/websandbox` — a cross-origin iframe with no same-origin
access. The implementation is more considered than "dump HTML in an iframe":

- **The parameter order is part of the design.** The tool description tells the model to emit
  `initialHeight` + placeholders first, then *all* the CSS, then HTML (which streams live so the user
  watches the UI build), then JS. There is a *preview* sandbox showing partial HTML while it streams,
  destroyed and replaced by the final sandbox when `htmlComplete` arrives.
- **`sandboxFunctions` is the escape hatch, and it is the interesting part.** You declare
  `{ name, description, parameters, handler }`; each is described to the model as agent context and
  exposed inside the iframe as `await Websandbox.connection.remote.<name>(args)`. So the agent's
  invented UI can call back into *your* typed, audited functions — and only those.
- **`designSkill`** is an overridable prompt of visual house rules (the default is a shadcn-flavoured
  style guide). This is where a house style is enforced on generated UI.
- The model is told it *may* pull CDN scripts (Chart.js, D3, Three.js) but must not touch
  `localStorage`, cookies, IndexedDB, or same-origin fetch.

Activation is `!!config.openGenerativeUI || core.openGenerativeUIEnabled` — i.e. **the client can turn
it on by itself**, no runtime cooperation needed. The tool is registered client-side and travels to
the agent as a normal AG-UI frontend tool. Aleph could switch this on today against its Python agent.

The honest cost: the marketing page's own words — *"more error-prone, slower, expensive, less
predictable than leftward bands."*

### 1.5 The bands are meant to be mixed

Directly from the spectrum page: *"A single chat session can render a Controlled dashboard component,
a Declarative ad-hoc report, and an Open MCP App in sequence."* The choice is **per surface, not per
product**. Aleph currently uses exactly one band for everything.

---

## 2. The decisive question: how a catalog is actually defined and registered

This section answers the owner's design question directly and with evidence.

### 2.1 What a catalog *is*

From `@a2ui/web_core/v0_9` (`src/v0_9/catalog/types.d.ts`, version 0.10.0, installed in this repo):

```ts
export declare class Catalog<T extends ComponentApi> {
  readonly id: string;
  readonly components: ReadonlyMap<string, T>;
  readonly functions: ReadonlyMap<string, FunctionImplementation>;
  readonly themeSchema?: z.ZodObject<any>;
  constructor(id: string, components: T[], functions?: FunctionImplementation[], themeSchema?: z.ZodObject<any>);
}
```

A catalog is **an id plus a flat list of components**. It is immutable (the maps are `ReadonlyMap`,
and the class docstring says the map is readonly "to encourage immutable extension patterns"), it is
pure configuration, and it is cheap to construct.

`createCatalog(definitions, renderers, { catalogId, includeBasicCatalog })` from
`@copilotkit/a2ui-renderer` is a **20-line loop** over that constructor: for each definition it builds
`{ name, schema }`, wraps the renderer with `createReactComponent`, and ends with
`new Catalog(catalogId, includeBasic ? [...basic, ...custom] : custom, …)`. `catalogId` defaults to
`"copilotkit://custom-catalog"`. That is the entire mechanism.

Aleph's `buildAlephCatalog()` is that same constructor called by hand. **The two are interchangeable.**

### 2.2 Can several catalogs be merged? — Yes, two different ways

**Merge at construction (concatenate the component arrays).** This is what `includeBasicCatalog: true`
does, and it is what Aleph already does with the basic primitives. For N plugins:

```ts
new Catalog("aleph://v1", [...pluginA.components.values(),
                           ...pluginB.components.values(),
                           ...basicCatalog.components.values()], [])
```

Nothing about this is a hack — it is the documented "immutable extension pattern". The one thing to
watch is **name collision**: `components` is a `Map` keyed by name, so a later plugin silently wins.
A plugin loader must reject or prefix duplicate component names. (See namespacing, below.)

**Keep them separate and pass them all.** This is the better answer, and it is native:

```ts
export declare class MessageProcessor<T extends ComponentApi> {
  constructor(catalogs: Catalog<T>[], actionHandler?: ActionListener);
}
```

**`MessageProcessor` takes an array of catalogs.** Each `createSurface` operation carries a
`catalogId`, and the processor routes the surface to the matching catalog. This is A2UI's native,
protocol-level namespacing — and Aleph is *already on this code path*:
`apps/web/src/a2ui/A2UISurfaceView.tsx` does `new MessageProcessor([catalog])`. Passing
`[coreCatalog, pluginA, pluginB]` there is a one-line change.

### 2.3 Can they be namespaced? — Yes, at the catalog level; component names are flat

`catalogId` is the namespace, and it is a URI by convention (`aleph://v1`,
`https://a2ui.org/specification/v0_9/basic_catalog.json`, `copilotkit://custom-catalog`). A plugin
would naturally claim `aleph://plugin/scholar@1`. Within a catalog, component names are a flat
string map — so if you *merge* into one catalog you must prefix names yourself
(`Scholar.SourceCard`); if you *keep catalogs separate* you don't have to, because the surface names
its catalog.

### 2.4 Can catalogs be registered dynamically at runtime? — Yes on the panel path; with a caveat on the chat path

**On Aleph's own panel path:** trivially. `buildAlephCatalog()` is called inside a `useMemo`; make
its dependency the set of enabled plugins and a new `Catalog` is built when that set changes.
Catalogs are pure config, so rebuilding is nearly free.

**On the CopilotKit chat path:** the provider's `a2ui.catalog` prop is singular (`catalog?: any`) and
`A2UIProvider` constructs its processor once:

```js
if (!processorRef.current) processorRef.current = new MessageProcessor([catalog ?? basicCatalog], …)
```

So the provider will not hot-swap the catalog *inside a mounted surface*. What actually happens when
you change the catalog is that `createA2UIMessageRenderer` is re-created (the provider's
`builtInActivityRenderers` memo depends on the filtered catalog), the renderer's component identity
changes, and React remounts — new processor, correct catalog, **but already-painted surfaces in the
scrollback re-mount**. That is the real constraint, and it is cosmetic rather than architectural.
Two honest ways around it: (a) build the union catalog once per session and use the per-component
switch below for enable/disable, or (b) stop using the provider's single-catalog prop for the chat
pane and drive `MessageProcessor([...catalogs])` yourself, exactly as `A2UISurfaceView` already does.

### 2.5 The piece nobody expected: per-component enable/disable, already built

This is, for Aleph's thesis, the most important discovery in the whole pass.
`packages/a2ui-renderer/src/react-renderer/filter-catalog.ts`:

```ts
export function filterCatalog<T extends ComponentApi>(
  catalog: Catalog<T>, predicate: (name: string) => boolean): Catalog<T>
```

> "Pure: does not mutate the source catalog. The returned catalog preserves the original `id`, all
> `functions`, and the `themeSchema`; only the component set is narrowed. Used by react-core to
> enforce per-component enable/disable on **BOTH the advertisement path (context) and the render
> path**."

And in `CopilotKitProvider.tsx`, the provider registers every catalog component onto core
(`copilotkit.setCatalogComponents(...)`), subscribes to `onCatalogComponentsChanged`, and re-derives
a filtered catalog from `core.isCatalogComponentEnabled(name)`. That filtered catalog is passed to
*both* `createA2UIMessageRenderer` (what can paint) *and* `A2UICatalogContext` (what the model is
told exists). The provider's own comment names the failure mode it is guarding against:

> "…registering its components here would make them toggleable in the inspector while disabling never
> actually removes them from what the model sees — a silent enforcement divergence."

**Read that as an Aleph feature spec.** "Deactivate a plugin at runtime" means *the model stops being
told the plugin exists, and the renderer stops being able to paint it, in the same operation.*
CopilotKit shipped exactly that, with the divergence bug already thought about. Aleph does not have
to invent it.

### 2.6 How the agent learns what it can draw

`packages/react-core/src/v2/a2ui/A2UICatalogContext.tsx` pushes **four** context entries into every
run, via `copilotkit.addContext`, scoped to `copilotkit.a2uiAgents`:

1. `buildCatalogContextValue(catalog)` — human-readable: the catalog id, whether it extends the basic
   catalog, and the JSON Schema of every *custom* component (via `getCustomComponentNames`).
2. `extractCatalogComponentSchemas(catalog)` under the constant `A2UI_SCHEMA_CONTEXT_DESCRIPTION` —
   the full inline catalog in A2UI v0.9 wire format (`allOf` + `$ref common_types.json#/$defs/ComponentCommon`).
   The constant is shared with `@ag-ui/a2ui-middleware` *specifically so a server-side schema can
   overwrite a frontend-provided one*.
3. `A2UI_DEFAULT_GENERATION_GUIDELINES` — protocol rules, path rules, data-model format, two-way binding.
4. `A2UI_DEFAULT_DESIGN_GUIDELINES` — visual rules, component hierarchy, action-handler patterns.

Two consequences for Aleph. First, **the schema the agent sees is derived from the catalog object**,
which eliminates the `catalog.json` ↔ `aleph-catalog-v09.tsx` seam described in section 0. Second,
the frontend→agent direction *is the supported direction* — the client advertises its catalog on
every run, and the server may override. So "the interface appears when the ability is installed"
works by the client re-advertising a changed catalog, not by a deploy.

Confirmed on the Python side too: `sdk-python/copilotkit/copilotkit_lg_middleware.py` has an
`_resolve_a2ui_catalog(state)` that looks for the frontend catalog in either
`state["ag-ui"]["a2ui_schema"]` or a `state["copilotkit"]["context"]` entry, and — its own words —
*"the tool is never advertised when the client can't render A2UI"*, with `catalog_id` bound so
*"BYOC custom catalogs render their own components, not the basic one."* BYOC — bring your own
catalog — is a first-class supported path in the Python SDK.

### 2.7 Verdict: **build, not rewrite**

Per-plugin catalogs need four things, and here is the honest state of each:

| Requirement | State | Work |
|---|---|---|
| A catalog is a portable, composable value | Already true — `Catalog(id, components[])`, immutable | none |
| Several catalogs can coexist, addressed by id | Already true — `MessageProcessor(catalogs[])`; Aleph is on that call site | 1 line |
| A catalog can be swapped at runtime | True on the panel path; chat path remounts on swap | small |
| A component can be turned off in the picture *and* the prompt together | Already built — `filterCatalog` + `setCatalogComponents` + `isCatalogComponentEnabled` | adopt |
| The agent is told what exists, derived from the catalog | Already built — `extractCatalogComponentSchemas`, `buildCatalogContextValue` | adopt, delete `catalog.json` duplication |
| **A plugin's renderers are in the browser bundle** | **Not solvable by any of this** | see below |

The last row is the only hard part, and it is physics, not architecture. A plugin that introduces a
*new visual component* must ship React code, and React code must be loaded by the browser. Three
honest options, in increasing order of ambition:

- **Compose, don't extend.** A plugin declares no new renderers; it composes existing ones (Aleph's
  21 cards + the A2UI primitives) into new arrangements. This needs **zero** new bundle code and is
  the band the Declarative catalog was designed for. Most plugins should live here.
- **Sandbox the new pixels.** A plugin that genuinely needs a new visual form emits it through
  Open-Ended generative UI (or ships an MCP App) — the iframe is the isolation boundary, so no
  bundle change and no trust extension.
- **Dynamic module loading.** A plugin ships an ES module that the app imports at runtime and whose
  default export is a `Catalog`. This is a real option (`Catalog` is just a value), but it means
  loading third-party JS into the app origin with full DOM access, which is a trust decision, not a
  build decision. Do not do this for anything the agent authored.

That mapping — compose / sandbox / load — is a **three-tier trust model that falls out of the
spectrum itself.** See section 5.

---

## 3. The react-core hooks Aleph is not using

`@copilotkit/react-core@1.58.0`'s `/v2` entry point exports ~25 hooks and components. Aleph imports
**six**: `CopilotKitProvider`, `createA2UIMessageRenderer`, `a2uiDefaultTheme`, `CopilotChat`,
`useAgentContext`, `useFrontendTool`, plus `useAgent`/`UseAgentUpdate` in `ActivityCard.tsx`.

| Hook | What it does | What it would give Aleph |
|---|---|---|
| **`useCapabilities`** | Reads the agent's declared `AgentCapabilities` from the `/info` handshake. Returns `undefined` until the handshake lands (treating that as "no capabilities" is the documented #1 bug). | **The runtime-discovery answer.** See section 4 — this is the "interface appears with the ability" primitive. |
| `useAgent` | Imperative handle on an agent: `messages`, `isRunning`, `addMessage`, `abortRun`, `setMessages`, per-thread clones, selective subscription (`OnMessagesChanged` / `OnStateChanged` / `OnRunStatusChanged`) with `throttleMs`. | Aleph uses it only for an activity card. It is also the way to drive chat from *outside* the chat pane — "explain this claim" from a card, with the answer landing in the dock. |
| `useAgentContext` | Pushes app state into every run, `JSON.stringify`'d. **Global, not per-agent** — `agentId` is silently dropped (`context-store.ts:26-31`). | Aleph uses it correctly. Worth knowing the scoping limit before building a per-plugin context story on it. |
| `useThreads` | List / paginate / rename / archive / delete durable threads. | **Intelligence-mode only.** Errors with "Runtime URL is not configured" against a plain SSE runtime. Aleph has its own project/thread model; this is a "don't build it twice" note, not a gap. |
| `useFrontendTool` | Browser-side tools the agent can call. Re-registers only on `name` / `available` / explicit `deps` change (stale-closure trap). `followUp` **defaults to true** — a pure side-effect tool that omits `followUp: false` re-invokes the agent in a loop. | Aleph has three (`focus_tab`, `open_page`, `highlight_claim`). This is the natural registration point for a **plugin's** browser-side verbs, and `agentId` scoping means two plugins can both own a tool named `save`. |
| `useRenderTool` | Skins an existing tool's progress/result UI. | Aleph's tool calls currently render as CopilotKit defaults. Every Aleph tool could show a real card while it runs. |
| `useComponent` | Registers a render-only tool. | The cheapest possible "agent, draw this exact Aleph component" path — no A2UI round trip, no secondary LLM. |
| `useDefaultRenderTool` | Sanctioned wildcard fallback; **off unless you call it**, deliberately, so raw tool JSON never leaks into production chat. | One line gives every unskinned Aleph tool a consistent expandable card. |
| **`useHumanInTheLoop`** | `useFrontendTool` minus the handler, plus a `render` that receives `respond`. The synthesized handler returns a Promise that only resolves when `respond()` is called. | **Aleph has an `ApprovalCard` and an ActionRouter but no protocol-level gate.** This is the mechanism that makes the agent *wait* for the analyst. Two sharp edges: never calling `respond` (including on reject) hangs the run and leaves the thread locked server-side; and unmounting mid-`executing` abandons the promise, so the HITL UI must live above route changes or abort the run on unmount. |
| `useAttachments` | Drag/drop/paste/click file intake, with `onUpload` to redirect to your own backend (returns an `Attachment.source`). | Aleph ingests PDFs today through a separate upload path. This would let an analyst drop a paper into chat and have it ingested, with the asset store as the `onUpload` target. |
| `useConfigureSuggestions` / `useSuggestions` | Static or LLM-generated suggestion pills; `reloadSuggestions` / `clearSuggestions`. Defaults: min 1, max 3. | Context-aware next steps ("verify this claim", "find contradicting sources") derived from the open pane. Note the caller must guard `reloadSuggestions` against `agent.isRunning` — the internal auto-reload guards, the manual call does not. |
| `useRenderActivityMessage` + `renderActivityMessages` | Renders non-chat activity events. Resolution order: `(type, agentId)` → `(type, unscoped)` → `"*"` → null; user renderers are evaluated **before** built-ins, so they can override MCP Apps / Open-GenUI. | This is the extension point Aleph's A2UI renderer already occupies. It is also how a plugin could ship its *own* activity type (e.g. `aleph://ingest-progress`) without touching chat. |
| `useRenderCustomMessages` | Inject UI before/after specific messages; receives `stateSnapshot` for the run. **First non-null wins** — two renderers targeting the same slot means one silently never fires. | Per-message provenance affordances: "cite this", "open the ledger event for this turn", a state-snapshot inspector. |
| `useCopilotKit().copilotkit` | The core object: `setHeaders`, `setProperties`, `addContext`/`removeContext`, `runTool({ followUp })`, `subscribe({ onAgentsChanged, onCatalogComponentsChanged })`, `setCatalogComponents`, `isCatalogComponentEnabled`. | The imperative surface a plugin *loader* would drive. `setHeaders` with a `null` value is also the documented fix for rotating auth tokens — relevant to Aleph's OIDC gap. |
| `showDevConsole` / `CopilotKitInspector` | Lazy-loaded web inspector: agents, context entries, tools, and the **catalog-component toggles** that drive `filterCatalog`. | A working plugin-toggle UI, for free, before Aleph builds its own. |

Two provider-level notes that bite in practice: array props (`renderActivityMessages`,
`renderCustomMessages`, `renderToolCalls`) go through `useStableArrayProp` and `console.error` if a
new array identity appears each render — memoize or hoist. And `onError` is effectively mandatory:
without it, a bad runtime URL or CORS failure leaves the provider in a provisional state showing
"connecting…" forever. `apps/web/src/lib/copilot.tsx` currently passes neither a memoized array (it
hoists to module scope — fine) nor an `onError` (a real gap).

Also worth flagging for the upgrade: the skill is explicit that `CopilotKit` from
`@copilotkit/react-core/v2` is the correct provider and `CopilotKitProvider` — which is what
`lib/copilot.tsx` uses — is "a subset of the functionality". And in 1.68 the A2UI renderer is
**auto-mounted** from the `a2ui` prop; passing it manually through `renderActivityMessages` (exactly
what Aleph does) is documented as a HIGH-severity mistake that "duplicates the renderer and can race
with the auto-injected one." Aleph's wiring was right for 1.58 and becomes wrong on upgrade.

---

## 4. `useCapabilities` — does the front end discover what the agent can do at runtime?

**Yes, and the shape is richer than the name suggests.** `AgentCapabilities` comes from `@ag-ui/core`
(0.0.53 is installed here) and is a *partial* declaration — every field optional, agents opt in:

- `identity` — `{ name, description, version, provider, documentationUrl, type, metadata }`. The
  type's own docstring: *"Agent identity and metadata for **discovery UIs, marketplaces**, and
  debugging."*
- `tools` — `{ supported, parallelCalls, clientProvided, tools: [{ name, description, parameters, metadata }] }`.
  **The agent can enumerate its own tools, with descriptions and schemas, over the wire.**
- `multiAgent` — `{ supported, delegation, handoffs, subAgents: [{ name, description }] }`.
- `multimodal` — input `{ image, audio, video, pdf, file }`, output `{ image, audio }`.
- `execution` — `{ codeExecution, sandboxed, maxIterations, maxExecutionTime }`.
- `humanInTheLoop` — `{ supported, approvals, interventions, feedback }`.
- `state` — snapshots, deltas, persistence, long-term memory. `transport` — streaming, websocket,
  binary, push, resumable. `reasoning`, `output`.

`useCapabilities(agentId?)` reads it straight off the agent object, populated from the `/info`
handshake. It is synchronous and returns `undefined` until the handshake completes.

**Why this matters more than any hook in section 3.** The owner's design is "when a new ability is
installed its interface appears with it." That requires the front end to *ask* what exists rather
than be *compiled* against it. `AgentCapabilities` is the ask, and it already carries the two fields
a plugin manifest needs: a typed tool list, and an identity block explicitly intended for discovery
UIs. Combined with `a2uiCatalogAvailable` (the client→server capability flag) and
`buildCatalogContextValue` (the client→agent catalog advertisement), you have a **bidirectional
capability handshake**: the agent declares its tools and sub-agents to the UI; the UI declares its
renderable catalog to the agent. Aleph currently uses neither direction — its front end is compiled
against a static component list and its agent's abilities are hard-coded in the graph.

The one caution, from the skill: `BuiltInAgent` **shallow-merges** capabilities at the category
level, so declaring `tools: { supported: true }` replaces the whole `tools` category rather than
merging into the defaults.

---

## 5. Mapping the spectrum onto a three-tier trust model

Aleph's memory already records a three-tier plugin model: *generated settings / composed arrangements
/ sandboxed custom code*. The spectrum maps onto it almost exactly — which is a good sign that the
model is right, not that it should be replaced.

| Aleph tier | Spectrum band | Trust extended | Isolation | Can the agent author it? |
|---|---|---|---|---|
| **Tier 1 — generated settings** | Controlled (`useComponent`) over a small generic set (toggle, select, number, secret), plus a JSON-Schema-driven form | none — the plugin supplies *data*, Aleph renders it | full: no plugin code runs | yes, safely — it's a manifest |
| **Tier 2 — composed arrangements** | Declarative (A2UI catalog) | none new — the plugin names components Aleph already trusts | full: the binder resolves names against a registry; an unknown name simply fails to paint | **yes — this is the sweet spot.** The agent can write a plugin whose entire UI is an A2UI surface, and it cannot escape the catalog |
| **Tier 3 — sandboxed custom code** | Open-Ended (`generateSandboxedUi`) or MCP Apps | none to the app origin; the only reachable host surface is the `sandboxFunctions` you declared | cross-origin iframe, no same-origin access, no `localStorage` / cookies / IndexedDB | yes — with a real, enforced boundary |
| *(off-model)* | Dynamic ES-module catalog loading | **total** — third-party JS in the app origin | none | **no.** Human-reviewed, signed plugins only |

**Is CopilotKit's model better, and should Aleph just adopt it?** Adopt the *mechanisms*, keep your
*model*. Specifically:

- CopilotKit's spectrum has no notion of trust tiers at all — it is organised by control, not by
  privilege. Aleph's model is the more useful frame for a plugin system, because "who wrote this and
  what may it touch" is the question a plugin host has to answer.
- But every mechanism Aleph would otherwise build is already there: `filterCatalog` +
  `setCatalogComponents` (activate/deactivate), `MessageProcessor(catalogs[])` (per-plugin
  namespacing), `extractCatalogComponentSchemas` (advertise to the agent), `sandboxFunctions`
  (the exact capability-scoping primitive tier 3 needs — a declared, typed allow-list of host calls
  reachable from inside the sandbox), `designSkill` (house style for generated UI), and the inspector
  (a toggle UI).
- The one thing Aleph must add that CopilotKit does not provide: **a guardrail against removing
  load-bearing capability.** `filterCatalog` will happily let you disable a component that a pinned
  surface depends on; nothing checks. That is Aleph's kernel problem, and it maps cleanly onto the
  composability model already in `packages/aleph-kernel`.

One extra mechanism worth naming, because it sits exactly at the tier boundary: 1.68's
`createA2UIMessageRenderer({ onAction })` takes an `A2UIActionInterceptor` — it sees every action a
rendered surface dispatches, can rewrite it, can handle it client-side (`return null` to suppress
forwarding), or let it through. Aleph currently intercepts actions by hand inside the `adapt()`
wrapper in `aleph-catalog-v09.tsx`. `onAction` is the sanctioned seam, and it is the natural place to
put a per-plugin action policy: *this plugin's surfaces may dispatch these action kinds and no
others.*

---

## 6. Where the prior recommendation was right, and where it was wrong

**Right:** the *specific job* Aleph's Node service does today has indeed moved into Python. Aleph's
`apps/copilot-runtime/src/server.ts` is 70 lines: an `HttpAgent` pointed at FastAPI, plus
`a2ui: { injectA2UITool: true, schema: ALEPH_A2UI_CATALOG, defaultCatalogId: "aleph://v1" }`.
`ag_ui_langgraph.get_a2ui_tools({ model, default_catalog_id })` plus `A2UIMiddleware` is the
documented Python equivalent, and the docs give the exact snippet. On today's usage, the prior pass's
reading was accurate.

**Wrong, from the UI side, in four ways:**

1. **It priced the service at its current use, not its reachable use.** Everything in sections 1.3,
   3, 4 and 5 is available through that service and unused. Deleting it prices the option at zero.
2. **MCP Apps is Node-only and is lost outright.** Verified against the LangGraph integration page:
   the Python agent contributes nothing; the runtime does server connection, tool discovery, activity
   emission, UI fetching and iframe rendering. For a project whose thesis is "third parties ship
   abilities", removing the one band where a third party ships an ability *with its interface* is a
   strategic loss, not a cleanup.
3. **`/info` is not one of five routes; it is a capability negotiation.** `agent-registry.ts` shows
   what the client reads from it: the agent list, `capabilities` per agent, `a2ui.{enabled,agents}`,
   `openGenerativeUIEnabled`, `threadEndpoints`, `suggestions`, `intelligence` (websocket URL),
   `licenseStatus`, `audioFileTranscriptionEnabled`, `inspectorMetadata`, `mode`, `version`. Six
   client features are gated on those flags — `useCapabilities`, `useThreads`, the auto-mounted A2UI
   renderer, the Open-GenUI renderer, suggestions, and the inspector. FastAPI *can* serve `/info`;
   the point is that it is a contract to implement and keep in sync with each `@copilotkit/core`
   release, not a route to copy.
4. **It didn't distinguish "the runtime" from "this runtime's config."** A fair alternative it never
   considered: keep the service, delete its A2UI config (let Python inject the tool), and spend the
   service on the things only it can do — MCP Apps, per-request auth hooks (`hooks.onRequest`, the
   documented fix for the header-forwarding gap in CLAUDE.md's *Known broken*), and a place to stand
   if Intelligence is ever evaluated.

**One thing that is genuinely true and cuts the other way:** the Node service is *not* required for
Declarative or Open-Ended generative UI. `#isA2UIActive()` returns true when the client passes a
catalog, `#isOpenGenerativeUIActive()` returns true when the client passes config, and both the
`generate_a2ui`-equivalent and `generateSandboxedUi` tools reach the agent as ordinary AG-UI frontend
tools. Two of the four bands are client-side facts. So the runtime's value is concentrated in MCP
Apps, `/info`, middleware, and Intelligence — not in generative UI as such.

---

## 7. What I'd actually do, from the UI side

Ordered by value per unit of risk. None of these requires deciding the runtime question first.

1. **Derive the agent-facing schema from the rendering catalog.** Replace the
   `catalog.json → catalog.generated.ts` path with `extractCatalogComponentSchemas(buildAlephCatalog())`.
   Kills a whole class of drift, deletes a generator, and produces the A2UI wire format the middleware
   already expects. (`catalog.json` survives as the *actions* vocabulary and the Python validator's
   source, which is a real job it does well.)
2. **Adopt `setCatalogComponents` + `filterCatalog`.** This is "activate/deactivate a plugin at
   runtime" — the product thesis — already implemented, tested by its authors, and correct on both
   the render path and the prompt path.
3. **Turn `A2UISurfaceView`'s `MessageProcessor([catalog])` into `MessageProcessor(catalogs)`.** One
   line, and per-plugin catalogs become real on the panel path immediately.
4. **Stop paying for dynamic-schema A2UI on surfaces whose shape is known.** Aleph runs
   `injectA2UITool: true` globally; the docs are explicit that fixed schema is faster, cheaper and
   deterministic. Reserve the secondary LLM for genuinely novel arrangements.
5. **Add `useHumanInTheLoop` behind `ApprovalCard`,** so a consequential action actually blocks the
   run instead of racing it. Mind the two hang conditions.
6. **Add `onError` to the provider,** and migrate `CopilotKitProvider` → `CopilotKit`.
7. **Prototype Open-Ended with `sandboxFunctions` scoped to Aleph's read-only query API.** Cheap,
   client-side only, and it is the concrete test of tier 3: can the agent invent a visualisation that
   reaches Aleph's data only through a typed allow-list? If yes, tier 3 has a working shape.
8. **Before deleting `apps/copilot-runtime`, run one MCP App through it.** Point `mcpApps.servers` at
   a public MCP server and see a third-party UI appear in the chat with zero Aleph frontend code.
   That is a fifteen-minute experiment that answers the strategic question with a demo instead of an
   argument.

---

## Appendix — evidence index

| Claim | Where I verified it |
|---|---|
| `Catalog(id, components[], functions?, themeSchema?)`, immutable | `node_modules/.pnpm/@a2ui+web_core@0.10.0/…/src/v0_9/catalog/types.d.ts` |
| `MessageProcessor(catalogs: Catalog<T>[])` | `…/src/v0_9/processing/message-processor.d.ts` |
| `createCatalog` is a loop over that constructor | `@copilotkit/a2ui-renderer@1.58.0/dist/react-renderer/create-catalog.mjs` |
| `A2UIProvider` builds its processor once | `…/dist/react-renderer/core/A2UIProvider.mjs:27` |
| `filterCatalog` + the enforcement-divergence comment | CopilotKit `packages/a2ui-renderer/src/react-renderer/filter-catalog.ts`; `packages/react-core/src/v2/providers/CopilotKitProvider.tsx` (~L400-700) |
| Four A2UI context entries pushed per run | `packages/react-core/src/v2/a2ui/A2UICatalogContext.tsx` |
| `openGenerativeUI` config, `sandboxFunctions`, `designSkill`, tool description | `packages/react-core/src/v2/providers/CopilotKitProvider.tsx:92-175`; `packages/angular/src/lib/open-generative-ui.ts` |
| Websandbox iframe, preview→final swap | `packages/react-core/src/v2/components/OpenGenerativeUIRenderer.tsx` |
| MCP Apps is runtime-level and Node-only | `.agents/skills/runtime/references/wiring-mcp-apps-middleware.md`; docs.copilotkit.ai `/integrations/langgraph/generative-ui/mcp-apps` |
| `/info` payload fields | CopilotKit `packages/core/src/core/agent-registry.ts:~800-880` |
| `AgentCapabilities` shape | `node_modules/.pnpm/@ag-ui+core@0.0.53/…/dist/index.d.ts:3601, 3960-4049` |
| Python BYOC catalog resolution | CopilotKit `sdk-python/copilotkit/copilotkit_lg_middleware.py::_resolve_a2ui_catalog` |
| Aleph builds a real A2UI catalog | `apps/web/src/a2ui/aleph-catalog-v09.tsx`, `apps/web/src/a2ui/A2UISurfaceView.tsx::buildAlephCatalog` |
| Aleph's two schema sources | `packages/aleph-a2ui/src/aleph_a2ui/catalog.json` + `scripts/gen_catalog.py` (writes only `apps/web/src/a2ui/catalog.ts` and `apps/copilot-runtime/src/catalog.generated.ts`) |
| Aleph's runtime is 70 lines | `apps/copilot-runtime/src/server.ts` |
| Aleph uses 6 of ~25 v2 exports | `grep -rn "@copilotkit" apps/web/src` vs `@copilotkit/react-core@1.58.0/dist/v2/index.d.mts` |
