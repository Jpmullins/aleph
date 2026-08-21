# Plugin UI design — mapping the generative-UI spectrum onto Aleph's plugins

Research pass, 20 Aug 2026. Area: **how a plugin's interface arrives with the plugin.**
Read-only; nothing in the tree was changed.

Sources, in order of authority: the installed packages themselves (`@a2ui/web_core@0.10.0`,
`@a2ui/react@0.10.0`, `@copilotkit/a2ui-renderer@1.58.0`, `@copilotkit/react-core@1.58.0`,
`@ag-ui/a2ui-middleware@0.0.10`, `@ag-ui/a2ui-toolkit@0.0.4`, `@ag-ui/mcp-apps-middleware@0.0.3`,
`@ag-ui/client@0.0.57`, `@jetbrains/websandbox@1.2.1`); the official CopilotKit skills at
`.agents/skills/`; Aleph's own tree; and the live `copilotkit.ai/generative-ui-spectrum` page.

Sibling documents: `docs/research/generative-ui-spectrum.md` (the UI-side survey this builds on),
`copilotkit-runtime-surface.md`, `copilotkit-integration-fit.md`, `copilotkit-intelligence.md`.

---

## In one paragraph

The owner wants every plugin to bring its own interface: install an ability, and its screen and its
settings page appear with it, without anyone editing the app. There are four ways for an agent to put
something on screen, and they differ in **who writes the pixels** — you, the agent, a third party, or
the model inventing HTML on the spot. This document places each kind of plugin UI in one of those
four, and the answer is boring in a good way: **almost everything a plugin needs is the second one**,
where the plugin names building blocks that Aleph already trusts and Aleph draws them. The underlying
A2UI protocol was designed for exactly this — a client can hold **many catalogs at once**, each with
its own name, and every screen says which catalog it belongs to. Aleph is already on that code path;
it just passes a list of one. So the owner's design is a **build, not a rewrite**. The one thing that
genuinely cannot move at runtime is a plugin's *own React code*, because React code has to be in the
browser bundle — and that limit is precisely what pushes genuinely-new pixels into a sandboxed iframe.
The settings card is the piece to build first: it needs no new React at all, no language model, and no
decision about the Node service, because A2UI's standard building blocks already include text fields,
checkboxes, sliders, dropdowns and date pickers with two-way data binding, and Aleph already merges
all of them into its catalog. It just never tells the agent they exist.

---

## 0. What I verified, including where earlier passes were wrong

Everything below is checked against the installed code, not against docs or memory.

**Right, and load-bearing:**

- A2UI supports many catalogs natively. `MessageProcessor(catalogs: Catalog<T>[])`, and
  `processCreateSurfaceMessage` does `this.catalogs.find(c => c.id === catalogId)`.
- Aleph already calls that constructor — twice (`A2UISurfaceView.tsx:105,180`,
  `SurfaceStreamProvider.tsx:91`), each time with an array of one.
- Aleph does build a real, spec-conformant A2UI catalog in `aleph-catalog-v09.tsx`.
- `openGenerativeUI` is available on the **installed** react-core 1.58 provider props, client-side
  only, needing no Node runtime and no upgrade.
- MCP Apps is Node-runtime-only. `@ag-ui/mcp-apps-middleware@0.0.3` is installed under
  `apps/copilot-runtime/node_modules`; there is no Python path.

**Wrong in the prior pass, and it changes a recommendation:**

- **`filterCatalog`, `setCatalogComponents` and `isCatalogComponentEnabled` are NOT installed.**
  `generative-ui-spectrum.md` §2.5 calls them "already built… installed transitively at 1.58" and
  ranks adopting them #2. I grepped every `dist` file of `@copilotkit/a2ui-renderer@1.58.0`,
  `@copilotkit/react-core@1.58.0` and `@copilotkit/core@1.58.0`: **zero hits.** They are 1.68
  features. The prior pass read them out of the CopilotKit *source at 1.68*, which the skills pin,
  not out of the tree.
  *Why it matters:* "activate/deactivate a plugin at runtime, already implemented" is not something
  Aleph can adopt today. **But it also does not need it** — see §2.4. `filterCatalog` is the tool you
  need when you have merged everything into one catalog. Per-plugin catalogs make it unnecessary:
  deactivating a plugin means not passing its catalog, which is a stronger guarantee than filtering
  components out of a merged one.

**New, and not in any prior pass:**

- **Aleph has two catalogs with the same id that disagree about functions**, and the chat pane has the
  broken one. Details in §6, R0. This is a live defect, not a tidiness issue.
- **The agent is never told that any input component exists.** The agent-facing catalog
  (`catalog.generated.ts`) lists 19 components. The rendering catalog holds 39. Among the 20 missing
  are `TextField`, `CheckBox`, `ChoicePicker`, `Slider` and `DateTimeInput` — every input primitive
  A2UI ships. §4 explains why this is the single most consequential piece of drift in the tree.
- **MCP Apps has weaker browser isolation than open-ended generative UI**, at the installed versions,
  which inverts the intuitive ordering. Evidence and numbers in §5.
- **Aleph's kernel already computes the guardrail CopilotKit does not have.**
  `aleph_kernel.support.support_set` / `dependent_closure` / `BlastRadius`, and
  `AgentPluginAPI.disable` refusing with `DependentsWouldBreak`. §3 and §5 wire catalogs into it.
- **Aleph already ships an MCP server** exposing its catalog and surface builders
  (`packages/aleph-a2ui/src/aleph_a2ui/mcp_server.py`). It is the server half of the MCP-Apps pattern
  minus the UI-resource half.

---

## 1. Which band does each kind of plugin UI belong in?

### 1.1 The four bands, in one line each

Verbatim from the live spectrum page, with what each is *best for* in its own words:

| Band | API | Who writes the pixels | Best for (their words) |
|---|---|---|---|
| **Controlled** | `useComponent()` | you, in React | "the few highest-traffic, brand-critical surfaces" |
| **Declarative** | `a2ui={{ catalog }}` | you write the pieces, the agent arranges them | "the long tail of internal tools, reports, and contextual UIs" |
| **MCP Apps** | `CopilotRuntime({ mcpApps })` | a third party, in an iframe | "third-party integrations and one-off experiences" |
| **Open-Ended** | `openGenerativeUI={…}` | the model, per response, into a sandbox | "one-off visualizations where 'good enough and surprising' beats 'pixel-perfect'" |

And, also verbatim: *"A single chat session can render a Controlled dashboard component, a Declarative
ad-hoc report, and an Open MCP App in sequence."* The choice is **per surface**, not per product.
Aleph currently uses one band for everything.

### 1.2 The placement

```
        you decide everything  <───────────────────────────>  agent decides everything
        ┌───────────────┬────────────────────┬─────────────┬──────────────────┐
        │  CONTROLLED   │    DECLARATIVE     │  MCP APPS   │    OPEN-ENDED    │
        │ useComponent  │   A2UI catalogs    │  iframe +   │  websandbox +    │
        │               │                    │  MCP server │  generated HTML  │
        ├───────────────┼────────────────────┼─────────────┼──────────────────┤
Aleph:  │ the 21 card   │ ▸ SETTINGS CARDS   │ ▸ third-    │ ▸ agent-authored │
        │ renderers,    │ ▸ PLUGIN MAIN      │   party     │   EXPERIMENTAL   │
        │ the reader,   │   SURFACES         │   integra-  │   VIEWS (escape  │
        │ the shell     │ ▸ agent-authored   │   tions     │   hatch only)    │
        │               │   arrangements     │   (vetted)  │                  │
        └───────────────┴────────────────────┴─────────────┴──────────────────┘
             ▲ needs a                ▲ pure data                ▲ pure data
               bundle change            — moves at runtime         — moves at runtime
```

Three of the four kinds the brief asks about land in **Declarative**. That is not laziness; it is the
only band whose payload is *data*, and data is the only thing that can arrive with a plugin at runtime.

---

### 1.3 Settings cards → **Declarative, fixed schema**

**Placement: Declarative. Generated by a deterministic Python function from the plugin's declared
config schema. No language model in the loop.**

Why not Controlled: Controlled means a React component you wrote. If a plugin introducing a new kind
of setting requires a new React component, then installing a plugin requires a redeploy, which is the
exact thing the design forbids.

Why not Open-Ended: a settings form is the one surface in the product where "surprising" is a defect.
It also handles secrets. Letting a model invent the HTML for the box you type an API key into is
indefensible.

Why not MCP Apps: settings *write host state*. An iframe served by a third party should never be the
thing that collects a credential for Aleph.

Why Declarative works perfectly here: A2UI's basic catalog ships `TextField`, `CheckBox`,
`ChoicePicker`, `Slider` and `DateTimeInput`, each binding two-way to a path in the surface's data
model, plus `Button` whose `action.event.context` resolves paths **to their current values at click
time**. That is a complete form system with no React and no state management. Full construction in §4.

The important qualifier: **fixed schema, not dynamic.** A2UI has two flavours. Dynamic schema
(`injectA2UITool: true`, which Aleph runs globally today) spends a *second* LLM call inventing the
layout. The docs are explicit that fixed schema is "faster, cheaper, and the UI is deterministic."
A settings card's shape is completely determined by the config schema, so there is nothing to invent.

---

### 1.4 A plugin's main surface → **Declarative, server-built, with Controlled components inside it**

**Placement: Declarative for the arrangement; Controlled for anything that needs real browser state.**

Aleph already does this and it is right. A pane is built server-side by `_build_tab_messages` in
`apps/api/src/aleph_api/routes/surfaces.py`, which returns a v0.9 message list
(`createSurface` → `updateComponents` → `updateDataModel`), streamed over one multiplexed SSE
connection and painted against the catalog. A plugin's main surface is one more entry in that table
plus, optionally, its own catalog.

The split that matters, and that a naive "everything declarative" reading gets wrong: **a bound data
model cannot express focus, virtualised lists, keyboard navigation, an embedded PDF, a text editor, or
a canvas.** Those are what Aleph's 21 card renderers are, and they must stay React. So:

```
  plugin's main surface
    = ARRANGEMENT  (declarative: Column / Row / List / Card / Tabs, data-bound)
    + LEAF PIXELS  (React components registered in a catalog — the "Controlled" half,
                    reached by name rather than by tool call)
```

That is the honest reason `NoteEditorCard` and `WikiPageCard` exist as React and `BriefsSurface` is
mostly layout. Keep the split; make the registry that holds it dynamic (§3).

A note on Controlled proper (`useComponent`): it registers a *new tool* whose only job is to render,
so the agent can call a card directly with no A2UI round trip and no secondary LLM. That is a cheap,
genuinely useful addition — a card that the agent renders inline in chat costs one tool call instead
of a whole generation pass. But it is a chat affordance, not a plugin-surface mechanism, because the
component still has to be in the bundle.

---

### 1.5 An agent-authored experimental view → **Declarative first, Open-Ended as the escape hatch**

**Placement: Declarative over a catalog the agent did not choose. Open-Ended only when no arrangement
of existing components can express the thing.**

The agent cannot write React into the bundle. That is physics, not policy. What it *can* do is emit
component names, and this is safer than it sounds:

- Lookup is `surface.catalog.components.get(componentModel.type)` (`@a2ui/react` v0_9, index.js:78).
- A name the catalog does not have renders a red `Unknown component: X` **and the rest of the surface
  still paints.** It does not throw, does not crash the pane, does not execute anything.
- The catalog id itself is not the agent's to choose: `A2UIMiddleware`'s `defaultCatalogId` stamps it
  on every streamed surface, and the toolkit's own comment says the catalog id "is owned by the
  factory, not the subagent — the subagent can't invent a catalog the host hasn't registered."

So a Declarative experimental view is bounded by construction, and the boundary fails visibly rather
than silently. That is the property you want when the author is a language model.

When Declarative genuinely cannot do it — a bespoke chart type, a force-directed graph, an interactive
timeline nobody built a component for — `openGenerativeUI` is the escape hatch, and its isolation is
the strongest in the stack (§5.3). It needs no Node runtime and no upgrade: activation is
`!!config.openGenerativeUI`, the tool is registered client-side and reaches the agent as an ordinary
AG-UI frontend tool.

**Never Controlled.** An agent-authored plugin that could register a React renderer would be running
agent-authored JavaScript in the app origin with full DOM access. That is a different product.

---

### 1.6 A third-party integration → **MCP Apps, with a caveat that changes the recommendation**

**Placement: MCP Apps is the right *shape* and, at the installed versions, the wrong *isolation* for
anything you do not already trust.**

The shape is exactly what the plugin thesis asks for: an MCP server exposes a tool *and* a UI resource
for it (SEP-1865 `uiResourceUri`); the runtime discovers it, injects the tool, emits an `mcp-apps`
activity carrying the resource URI, the client fetches it and renders it in an iframe, and it persists
in thread history. Zero frontend code in the host app. Nothing else in the ecosystem does that.

Three findings that qualify it, all from the installed code:

1. **No per-tool filtering.** The runtime skill's own words: `@ag-ui/mcp-apps-middleware` at the
   pinned `0.0.3` "does not support `includeTools` or `excludeTools`. Supplying either key raises a
   configuration error." Scoping is per *server*, per *agent*, and that is all.
2. **The iframe can call the MCP server directly, bypassing the agent.** `MCPAppsMiddleware`'s own
   docstring on `handleProxiedMCPRequest`: *"Handle a proxied MCP request from the frontend iframe.
   This bypasses the normal agent flow and directly executes the MCP request."* The request is a
   generic `{serverHash, method, params}` — `tools/call`, `resources/read`, anything the server
   exposes. So enabling an MCP App grants its iframe an authenticated channel to its own server that
   Aleph's agent never sees and Aleph's ledger never records.
3. **The frame is not origin-isolated.** The renderer sets `iframe.srcdoc = buildSandboxHTML(...)`
   with `sandbox="allow-scripts allow-same-origin allow-forms"`. `srcdoc` + `allow-same-origin` means
   the framed document inherits the *host* origin. There is a CSP meta inside and a second, inner
   frame — but the inner frame's sandbox string is taken from the MCP server's own message
   (`if (typeof sandbox === "string") inner.setAttribute("sandbox", sandbox)`).

**Recommendation:** MCP Apps for **first-party and human-vetted** servers — where it is excellent, and
where Aleph's existing `aleph_a2ui.mcp_server` is already 80% of the server half. For a third-party
integration whose UI you have not read, route it to Open-Ended's websandbox or to Aleph's existing
sandboxed-artifact iframe instead. Do not treat "MCP Apps" and "safe for arbitrary third parties" as
the same statement; at these versions they are not.

This also slightly discounts — it does not erase — the strategic argument in
`generative-ui-spectrum.md` §6 for keeping the Node service. MCP Apps remains the one band only that
service can provide, and it remains worth a fifteen-minute experiment. But its value at the pinned
versions is "first-party tools that ship their own panel", not "an open plugin marketplace."

---

## 2. Can A2UI catalogs be registered dynamically and merged at runtime?

**Yes. It is a build, not a rewrite.** Four claims, each with the code that proves it.

### 2.1 A catalog is a value, not a configuration step

`@a2ui/web_core@0.10.0`, `src/v0_9/catalog/types.js`:

```js
export class Catalog {
  constructor(id, components, functions = [], themeSchema) {
    this.id = id;
    const compMap = new Map();
    for (const comp of components) compMap.set(comp.name, comp);   // ← last wins, silently
    this.components = compMap;
    const funcMap = new Map();
    for (const fn of functions) funcMap.set(fn.name, fn);
    this.functions = funcMap;
    ...
  }
}
```

An id, a component list, a function list. Immutable, no I/O, no registration, no globals. Building one
costs a `Map` fill. Building the whole set on every change of the enabled-plugin set is free.

`createCatalog()` from `@copilotkit/a2ui-renderer` is a twenty-line loop over that same constructor.
Aleph's `buildAlephCatalog()` calls the constructor by hand. **They are interchangeable.**

### 2.2 Many catalogs at once is the protocol, not a workaround

```
MessageProcessor(catalogs: Catalog<T>[])          ← plural, in the constructor
  │
  ├── processCreateSurfaceMessage(msg):
  │       const { surfaceId, catalogId } = msg.createSurface
  │       const catalog = this.catalogs.find(c => c.id === catalogId)
  │       if (!catalog) throw A2uiStateError(`Catalog not found: ${catalogId}`)
  │       model.addSurface(new SurfaceModel(surfaceId, catalog, theme, sendDataModel))
  │
  └── each SurfaceModel holds ONE catalog for its whole life
          rendering:  surface.catalog.components.get(componentModel.type)
                      → miss renders "Unknown component: X" in red, does not throw
```

And the protocol has a **plural client-side advertisement** built for precisely this
(`src/v0_9/schema/client-capabilities.d.ts`):

```ts
interface A2uiClientCapabilities {
  'v0.9': {
    supportedCatalogIds: string[];     // ← plural
    inlineCatalogs?: InlineCatalog[];  // ← { catalogId, components, functions, theme }
  }
}
```

`MessageProcessor.getClientCapabilities({ includeInlineCatalogs: true })` generates it from whatever
catalogs the processor holds. **A2UI was designed for a client that holds several catalogs and tells
the server about all of them.** That is the owner's design, expressed in the protocol Aleph already
speaks. Aleph calls neither `getClientCapabilities` nor passes more than one catalog.

### 2.3 What this means concretely, path by path

```
                          PANE PATH                         CHAT PATH
                    (SurfaceStreamProvider)           (CopilotKit provider)
                             │                                 │
   who builds the      Aleph's own code                 A2UIProvider, inside
   MessageProcessor    ─────────────────                @copilotkit/a2ui-renderer
                             │                                 │
   current call        new MessageProcessor([catalog])   new MessageProcessor(
                             │                             [catalog ?? basicCatalog], …)
                             │                             built ONCE into a ref
   dynamic catalogs?   ✅ ONE-LINE CHANGE:               ⚠️  the a2ui.catalog prop is
                       new MessageProcessor(                  singular and non-swappable
                         enabledCatalogs)                     in place
```

- **Panes: real today, one line.** `SurfaceStreamProvider.buildCatalog()` becomes a function of the
  enabled-plugin set inside its existing `useMemo`, and `new MessageProcessor([catalog])` becomes
  `new MessageProcessor(catalogs)`. Per-plugin catalogs are live on the pane path immediately.
- **Chat: needs one of two small things.** `A2UIProvider` does
  `if (!processorRef.current) processorRef.current = new MessageProcessor([catalog ?? basicCatalog], …)`
  — a ref, built once, wrapping the single `catalog` prop in a literal one-element array. Passing an
  array of catalogs to `a2ui.catalog` would produce `[[c1, c2]]` and break. Two honest ways out:
  - **(a) Merge, remount.** Build one union catalog per session; when the enabled set changes, the
    renderer identity changes and React remounts the processor. Correct, ~5 lines, and the cost is
    that already-painted surfaces in the scrollback re-mount. Cosmetic.
  - **(b) Own the renderer.** Register your own activity renderer for the `a2ui-surface` activity type
    via `useRenderActivityMessage` and drive `MessageProcessor(catalogs)` yourself — exactly what
    `SurfaceStreamProvider` already does. ~60 lines, no remount, and it is the long-term home. Note
    user renderers are evaluated **before** built-ins, so this overrides cleanly.

### 2.4 What Aleph does *not* need to build

Because catalogs are separable, **deactivating a plugin is "stop passing its catalog."** That is
stronger than `filterCatalog`, which narrows a merged catalog's component set: with separate catalogs,
a surface from a deactivated plugin fails at `createSurface` with a catchable
`A2uiStateError: Catalog not found`, before a single component is resolved. Fail-closed, at the
earliest possible point, for free.

The one thing `filterCatalog` buys that this does not is *sub-plugin* granularity — turning off one
component of a plugin you otherwise keep. That is a real feature and it arrives with 1.68. It is not a
prerequisite for the plugin thesis.

### 2.5 The one genuine limit

**A plugin that introduces a new visual component must ship React, and React must be in the bundle.**
Everything above that line is data. Three options, and they are the trust tiers of §5:

- **Compose.** The plugin declares no renderers; it arranges components Aleph already ships. Zero
  bundle change. Most plugins belong here.
- **Sandbox.** The plugin's novel pixels go through `openGenerativeUI` or an MCP App. The iframe is
  the isolation boundary. Zero bundle change, no trust extension.
- **Load.** The plugin ships an ES module whose default export is a `Catalog`, imported at runtime.
  Technically trivial (`Catalog` is a value). It is third-party JavaScript in the app origin with full
  DOM access — a human-review decision, never an agent one.

---

## 3. Namespacing and versioning across plugin catalogs

Two plugins will both define `Chart`. Here is exactly what the stack gives you and what it does not.

### 3.1 What A2UI/CopilotKit provide

| Provided | Mechanism | Strength |
|---|---|---|
| **A namespace per catalog** | `catalogId`, URI-shaped by convention (`aleph://v1`, `https://a2ui.org/specification/v0_9/basic_catalog.json`, `copilotkit://custom-catalog`); every `createSurface` names one | real, protocol-level |
| **Routing by that namespace** | `this.catalogs.find(c => c.id === catalogId)`, exact string match | real |
| **Fail-closed on unknown namespace** | `throw new A2uiStateError("Catalog not found: …")` | real, catchable |
| **Fail-visible on unknown component** | `Unknown component: X` rendered in red; surface keeps painting | real |
| **A per-catalog theme schema** | `Catalog(id, components, functions, themeSchema)` | real, unused by Aleph |

| **Not** provided | Consequence |
|---|---|
| Component-name namespacing *within* a catalog | `compMap.set(comp.name, comp)` — a duplicate name silently replaces the earlier one, no warning, no error |
| Function-name namespacing | same `Map`, same silent overwrite |
| Any version field on a catalog | `id` is an opaque string; nothing parses it |
| Cross-catalog composition in one surface | a `SurfaceModel` binds to exactly one catalog for life |
| A check that removing a component breaks a live surface | nothing, anywhere |

The last row of the first table and the last row of the second are the two that decide the design.

### 3.2 The decision this forces

Because a surface binds to **one** catalog, you get to pick per surface, and only one of these two:

```
  OPTION A — separate catalogs                  OPTION B — one merged catalog
  ────────────────────────────                  ────────────────────────────
  aleph://plugin/scholar@1  { Chart, … }        aleph://v1 { scholar.Chart,
  aleph://plugin/finance@1  { Chart, … }                     finance.Chart, … }

  ✅ both may define "Chart"                     ✅ one surface can mix plugins
  ✅ deactivate = drop the catalog               ❌ names must be prefixed by hand
  ✅ collision is impossible                     ❌ collisions are SILENT
  ❌ a surface is all-scholar or all-finance     ❌ deactivate needs filterCatalog (1.68)
```

### 3.3 What I recommend Aleph do

**Both, deliberately, with a rule about which is which.**

```
  aleph://core@1                    ← the 21 cards + all 18 A2UI primitives.
    │                                  Stable. Human-owned. Never plugin-authored.
    │
    ├── aleph://plugin/scholar@1    ← core components ∪ scholar's own
    ├── aleph://plugin/finance@1    ← core components ∪ finance's own
    └── aleph://plugin/…@1
```

- Every plugin catalog is **core ∪ its own**. So a plugin's surfaces can always use Aleph's cards and
  the A2UI primitives, and never see another plugin's components.
- Two plugins' `Chart` never meet, because they are never in the same catalog.
- The cost — plugin A's component cannot appear inside plugin B's surface — **is the isolation
  boundary, not a limitation.** A cross-plugin comparison is two panes, which is what panes are for.
- Deactivation is dropping a catalog from the array. Fail-closed at `createSurface`.

**Versioning: put the major in the id.** `aleph://plugin/scholar@1` vs `@2` are different strings,
therefore different catalogs, therefore they coexist in the same `MessageProcessor` array with no
migration. Surfaces created before an upgrade keep painting against the catalog they named until they
are recreated. That is free, protocol-level version tolerance and it costs one naming convention.

### 3.4 What Aleph must add — four things, none large

1. **A collision check at build time.** The `Map` will never tell you. When assembling
   `core ∪ plugin`, assert the intersection of component names is empty and refuse the plugin
   otherwise, naming both sides. Same for functions. ~15 lines, and it converts a silent overwrite
   into a rejected install.
2. **A single registry: plugin → catalogId → component names → renderers.** Today Aleph has *three*
   partial answers (`ALEPH_CARD_IMPLS`, `catalog.json`, `catalog.ts`'s `COMPONENT_NAMES`) and no
   single one. §6, R1.
3. **Stability within a major.** A component name, once published in `@1`, keeps its meaning. Removing
   or re-typing one is a `@2`. Nothing enforces this but a review rule and a snapshot test.
4. **The guardrail — and Aleph already has the hard part.** Nothing in A2UI or CopilotKit checks that
   removing a catalog breaks a pinned pane. `packages/aleph-kernel` already computes exactly this:

   ```
   CapabilitySpec(name, setup, probe, provides: frozenset, requires: frozenset)
        │
        ├── support_set(specs, retired=[…])   pure, no I/O — "what still runs if this goes"
        ├── dependent_closure / BlastRadius   "deactivating X would take down N dependents"
        └── AgentPluginAPI.disable(plugin_id) → raises DependentsWouldBreak
                 …and core capability is mounted from the boot manifest, so it never
                 receives a PluginId at all — it is unnameable, not merely protected.
   ```

   Wire catalogs in as capabilities:
   `provides = {"ui:catalog:aleph://plugin/scholar@1"}`; a pinned pane or a saved workspace layout
   declares `requires` on it. Then "the agent deactivated a plugin a pinned pane depends on" is
   refused by a pure computation *before* anything happens, and the agent can call `inspect` to see
   the blast radius first — which the kernel's own docstring correctly notes matters as much as the
   refusal, because "a refusal it cannot predict is indistinguishable from a broken tool."

   This is the one place Aleph is genuinely ahead of CopilotKit. Do not build a second, weaker
   version of it in the front end.

---

## 4. The settings card, in detail

**This is the highest-value, lowest-risk first build, and it needs no new React, no LLM, no
CopilotKit upgrade, and no decision about the Node service.**

### 4.1 Why it is nearly free — and the one thing blocking it

The A2UI basic catalog ships eighteen components, and Aleph's rendering catalog already merges all
eighteen in (`...basicCatalog.components.values()`):

```
Text  Image  Icon  Video  AudioPlayer  Row  Column  List  Card  Tabs  Divider  Modal
Button  TextField  CheckBox  ChoicePicker  Slider  DateTimeInput
                   └──────────────── every input a settings form needs ────────────────┘
```

Each input binds two-way: `"value": { "path": "/config/api_key" }`. The client writes user input back
into the data model at the bound path. A `Button` reads paths at click time via
`action.event.context`. That is a complete, stateless form system.

**But the agent is never told any of it exists.** The agent-facing catalog
(`apps/copilot-runtime/src/catalog.generated.ts`, generated from `catalog.json`) lists **19**
components: ten cards plus nine primitives — `Button, Card, Column, Divider, Icon, Image, List, Row,
Text`. Not one input component. Not `TextField`, not `CheckBox`, not `ChoicePicker`, not `Slider`, not
`DateTimeInput`.

That single omission explains a design smell in the tree. `FormCard` — a React component with local
`useState` and four field types (`text | textarea | select | boolean`) — exists because the agent had
no primitives to build a form from. And `FormCard` is the wrong basis for settings for a concrete
reason: `const [values, setValues] = useState<Record<string, unknown>>({})` with no `defaultValue` on
any input. **It cannot show a plugin's current configuration.** Every field opens blank and untouched
fields submit blank. Keep `FormCard` for conversational one-shot input; do not build settings on it.

### 4.2 The mechanism, end to end

```
 ┌─ plugin manifest ────────────────┐
 │ id            = "scholar"        │
 │ catalog       = "aleph://plugin/ │
 │                  scholar@1"      │
 │ config_schema = { JSON Schema }  │   ← draft 2020-12, the same dialect Aleph
 └──────────────┬───────────────────┘     already uses for catalog.json + action params
                │
                ▼   settings_surface(manifest, current_values) — pure Python, no LLM
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │  createSurface   { surfaceId: "settings:scholar", catalogId: "aleph://core@1" }│
 │  updateComponents  root=Card → Column → [ field, field, …, Row[Button] ]     │
 │  updateDataModel   { "/config": { …current values… } }                       │
 └──────────────┬──────────────────────────────────────────────────────────────┘
                │  same SSE pane transport Aleph already runs
                ▼
 ┌─ browser ────────────────────────────────────────────────────────────────────┐
 │  A2UI binder paints it. User edits → written back to /config/<name>.          │
 │  Save clicked → action { event: { name: "plugin.configure",                   │
 │                    context: { plugin_id: "scholar", values: {path:"/config"}}}}│
 │                          └── paths resolved to CURRENT VALUES at click time ──┘
 └──────────────┬──────────────────────────────────────────────────────────────┘
                ▼
 ┌─ POST /v1/projects/{id}/cards/actions ──────────────────────────────────────┐
 │  ActionRouter validates params against the declared action schema            │
 │  → jsonschema.validate(values, manifest.config_schema)                       │
 │  → write config row (project_id, access_scope, created_by)                   │
 │  → ActionLedgerEvent "plugin.configure" IN THE SAME TRANSACTION              │
 │  → recompute the surface; the existing diff machinery emits updateDataModel  │
 └─────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 The generator, field by field

`settings_surface()` is a total function from JSON Schema to A2UI components. The whole mapping:

| JSON Schema | A2UI component | Binding |
|---|---|---|
| `{"type":"string"}` | `TextField` | `value: {path:"/config/<n>"}` |
| `{"type":"string","format":"password"}` | `TextField`, `variant:"obscured"` | write-only, see §4.4 |
| `{"type":"string","maxLength":>200}` | `TextField`, `variant:"longText"` | `value: {path}` |
| `{"type":"string","format":"date-time"}` | `DateTimeInput`, `enableDate/enableTime` | `value: {path}` |
| `{"type":"boolean"}` | `CheckBox` | `value: {path}` |
| `{"type":"number"}` with `minimum`+`maximum` | `Slider`, `min`/`max`/`step` | `value: {path}` |
| `{"type":"number"}` otherwise | `TextField`, `variant:"number"` | `value: {path}` |
| `{"enum":[…]}` | `ChoicePicker`, `variant:"mutuallyExclusive"` | `options:[{label,value}]` |
| `{"type":"array","items":{"enum":[…]}}` | `ChoicePicker`, `variant:"multipleSelection"` | `value: {path}` |
| `description` on any property | `Text`, `variant:"caption"` beneath the field | literal |
| `required` array | `Divider`-separated required block, plus server-side check | — |

Plus a fixed frame: `Card → Column → [Text(h2, plugin title), …fields…, Divider,
Row(justify:"end") → Button(variant:"primary", child: Text("Save"))]`.

It is a pure function, so it is unit-testable against a golden message list, it cannot hallucinate a
field, and it cannot omit one. Roughly 150 lines of Python and one new action kind.

### 4.4 Two traps, named

**Secrets must not ride the data model.** `variant: "obscured"` masks the *display*; the value still
lives in the surface data model and travels back inside the action context. For an API key:

- Seed `/config/api_key` with `""`, never with the stored secret.
- Render a `Text` caption showing `"set"` / `"not set"` derived server-side.
- Treat an empty string on submit as "unchanged", a non-empty string as "replace".
- Store through `ConnectorCredential` (encrypted), per Aleph's existing rule that credentials never
  come from container env — and never as plain config.

**The action vocabulary is closed, and that is the point.** `catalog.json`'s `actions` block is a
declared list with a param schema each, validated by `ActionRouter` before dispatch. `plugin.configure`
must be added there. That is the job `catalog.json` does well and should keep doing (§6, R1) — it is
the one place in the tree where "what a surface may ask the host to do" is written down and enforced.

### 4.5 Why this is the right first build

- **No new React.** Every component used is already in the rendering catalog.
- **No language model.** Deterministic, testable, reviewable.
- **It exercises the entire per-plugin path** — catalog-id routing, two-way data binding, the action
  round-trip, the ledger write — on the *safest possible* surface, where a bug shows up as a wrong
  checkbox rather than a wrong claim.
- **It is the demo.** Install a plugin; its settings page appears. That is the thesis, visible, in one
  screenshot.
- **It is independent of every open question.** It does not need MCP Apps, Intelligence, the 1.68
  upgrade, or a decision on `apps/copilot-runtime`.

Its one prerequisite is R1 in §6: the agent (and the generator's consumers) must be told the input
components exist.

---

## 5. Trust tiers

### 5.1 The mapping

The organising question is not "who wrote this" but **"what can a wrong version of this do?"**

| Tier | What it means | Bands allowed | What actually enforces it |
|---|---|---|---|
| **unverified** | authored, nothing proven — including anything the agent just wrote and has not run | **Open-Ended only**, with `sandboxFunctions` empty. Plus a *settings* surface, because Aleph's generator writes those pixels, not the plugin | websandbox opaque origin; empty allow-list |
| **asserted** | declares a catalog and passes its kernel probe | **Declarative over `aleph://core@1` only** — it may arrange components Aleph already trusts and nothing else. Open-Ended with a read-only `sandboxFunctions` allow-list | catalog-id is host-owned; unknown component paints visibly and does not execute; `CapabilitySpec.probe` is mandatory and must exercise a real read path |
| **earned** | has run under review, has a ledger history, a human has read its renderers | **Declarative with its own catalog** (`aleph://plugin/x@1`) — it may register components, which means it ships React, which means someone read it | catalog-id routing; `FilterToolCallsMiddleware` (installed, `@ag-ui/client@0.0.57`) allow/deny on its tools; kernel `provides`/`requires` + `support_set` |
| **signed** | human-reviewed and signed, or first-party | **all four**, including MCP Apps and non-empty `sandboxFunctions` | nothing automatic — this tier *is* the human step |

```
              unverified      asserted        earned         signed
  Controlled      ✗              ✗              ✗              ✓   (bundle change)
  Declarative     ✗          core catalog   own catalog        ✓
  MCP Apps        ✗              ✗              ✗              ✓   (see §1.6)
  Open-Ended      ✓          ✓ read-only    ✓ scoped       ✓ full
                (no host      sandbox        sandbox
                 functions)   functions      functions
```

### 5.2 Which band may an agent-authored plugin use?

**Declarative, over the core catalog, at `asserted`.** That is the sweet spot and it is genuinely safe:

- The agent emits component *names*. It cannot emit code.
- An unknown name renders `Unknown component: X` in red and the rest of the surface still paints.
- It cannot choose the catalog: `defaultCatalogId` is stamped by the host, and the toolkit's comment
  says so explicitly.
- Its only lever on the host is the action vocabulary, which `ActionRouter` validates against a
  declared param schema and which writes a ledger row per dispatch.

Once it has run under review and a human has read anything it ships, it can be promoted to `earned`
and register its own catalog.

**It may never use MCP Apps.** Enabling an MCP App means pointing the runtime at a URL. The iframe
then gets an authenticated, agent-bypassing channel to that server (§1.6), with no per-tool filter at
`0.0.3`. That is an outbound-network plus tool-execution grant, and an agent must not be able to mint
one for itself.

### 5.3 Where `openGenerativeUI` sits relative to Aleph's sandboxed-artifact iframe

They are two different sandboxes with two different strengths, and Aleph should keep both.

```
  openGenerativeUI                          Aleph's sandboxed artifact
  ────────────────                          ──────────────────────────
  model emits css/html/js in a tool call    code-runner executes agent code server-side
       ↓                                    (credential-less, network-partitioned)
  @jetbrains/websandbox iframe                   ↓
    frame.sandbox = "allow-scripts"         output stored as a VERSIONED artifact
    NO allow-same-origin → opaque origin    in the asset store, with provenance
       ↓                                         ↓
  sandboxFunctions: a typed, declared       rendered in a `sandbox` iframe,
  allow-list reachable ONLY as              citable, exportable, drift-checked
    Websandbox.connection.remote.<name>()   (tests/e2e/test_artifact_drift.py)
       ↓                                         ↓
  ephemeral: lives in the transcript        durable: a research object with a ledger row
```

- **websandbox is the stricter *origin* boundary.** `sandbox="allow-scripts"` and nothing else — no
  `allow-same-origin`, so an opaque origin, no `localStorage`, no cookies, no same-origin fetch.
  Verified in `@jetbrains/websandbox@1.2.1` `createIframe()`. Notably **stricter than MCP Apps'
  `allow-scripts allow-same-origin allow-forms`.**
- **The artifact path is the stricter *provenance* boundary.** The code ran in a controlled executor,
  the output is addressable and versioned, and it can be cited.
- **They compose; they do not compete.** Use `openGenerativeUI` for throwaway, in-chat, "show me this
  shape" visuals. Keep the artifact path for anything that has to be cited, versioned, exported, or
  drift-checked. Do not replace the artifact path with generated HTML — an artifact is a research
  object with a ledger row; a sandboxed generative view is a picture.
- **`sandboxFunctions` is where Aleph's capability model reaches the browser.** Each declared function
  is described to the model as run context and reachable only through the RPC bridge. Grant an
  `asserted` plugin `search_corpus` and `get_claim` — read-only, project-scoped, ledger-writing — and
  nothing else. That is a capability grant with a typed signature and an enforced boundary, which is
  precisely the shape `aleph-kernel` already speaks.

### 5.4 The guardrail CopilotKit does not have

Stated once more because it is the load-bearing asymmetry: **nothing upstream checks that turning a
plugin off breaks something that is still on.** `filterCatalog` will happily remove a component a
pinned surface depends on. Aleph's kernel computes exactly that, purely, before acting
(`support_set`, `dependent_closure`, `BlastRadius`, `AgentPluginAPI.inspect`/`disable`). Route catalog
activation through the kernel and the guardrail comes for free — and it is a *computation*, not a
policy string, so it cannot drift out of date.

---

## 6. What Aleph must fix first, in order

Three problems the brief named, all verified, plus one I found that is worse than any of them.

### R0 — Two catalogs share `aleph://v1` and disagree about functions. Fix this today.

```
apps/web/src/a2ui/A2UISurfaceView.tsx:57
    new Catalog("aleph://v1", [...ALEPH_CARD_IMPLS, ...basicCatalog.components.values()],
                [])                                      ← NO FUNCTIONS

apps/web/src/a2ui/SurfaceStreamProvider.tsx:35
    new Catalog("aleph://v1", [...ALEPH_CARD_IMPLS, ...basicCatalog.components.values()],
                [...basicCatalog.functions.values()])    ← 25 FUNCTIONS
```

`apps/web/src/lib/copilot.tsx:27` imports `buildAlephCatalog` from **`A2UISurfaceView`** — the
function-less one — and hands it to `createA2UIMessageRenderer`. So:

- **Workspace panes** get the 25 basic functions (`formatDate`, `formatCurrency`, `equals`, `add`,
  `openUrl`, `pluralize`, …).
- **The chat pane** gets none. Any surface using an expression function throws
  `A2uiExpressionError: Function not found in catalog 'aleph://v1': formatDate` from
  `Catalog.invoker`.

Same id, different behaviour, no test, no type error. A surface that renders in a pane will break in
chat and the failure names a catalog that "exists".

**Fix:** one `buildAlephCatalog()` in one module, functions included, imported by both. ~10 lines.
While there: `A2UISurfaceView`'s exported view components are now referenced by nothing — the file
survives only as the chat's catalog source. Move the catalog out; delete the dead views.

### R1 — `catalog.json` is two artifacts wearing one name, and the agent is told about half the catalog

**What it actually is (and does well):** a JSON-Schema description of a *card payload envelope*
`{type, id, props, data_bindings, children}` used by `validate_component`, plus the **actions
vocabulary** with a param schema per action. Both jobs are real; `routes/cards.py` and
`a2ui_handlers.py` depend on the first, `ActionRouter` on the second. Keep both.

**What it is being asked to be, and is not:** `a2ui.schema`, an `A2UIInlineCatalogSchema`. The
measurable consequences:

1. **Half the paint gate is dead.** `@ag-ui/a2ui-toolkit`'s validator reads
   `catalog.components[name].required` and `.properties` at the *top level*:

   ```ts
   interface A2UIValidationCatalog {
     components: Record<string, { required?: string[]; properties?: Record<string, unknown>; … }>;
   }
   ```

   Aleph's generated shape nests them one level down under `props`:
   `{ description, props: { type:"object", properties:{…}, required:[…] } }`. So
   `validateA2UIComponents` performs the *membership* check (`unknown_component` works) and its
   `missing_required_prop` check reads `undefined` and silently passes. Aleph believes it has a
   semantic paint gate; it has a name check.

2. **The envelope disagrees with the wire.** `catalog.json` describes `{type, props}`; `messages.py`
   correctly emits A2UI v0.9's `{id, component, …inline props}`. Two shapes, one file, no test.

3. **The agent sees 19 of 39 components.** Missing: all five input primitives (`TextField`,
   `CheckBox`, `ChoicePicker`, `Slider`, `DateTimeInput`), plus `Tabs`, `Modal`, `Video`,
   `AudioPlayer`, and five cards. This is what blocks §4 and it is the single most consequential drift
   in the tree.

**Fix, in order:**

- **(a)** Keep `catalog.json` for the card-payload validator and the actions vocabulary. Rename it in
  the docs so it stops claiming to be the A2UI catalog. Add `plugin.configure` to `actions`.
- **(b)** Derive the agent-facing catalog from the *rendering* catalog:
  `extractCatalogComponentSchemas(buildAlephCatalog())` — available in the **installed** 1.58
  `@copilotkit/a2ui-renderer`, and it emits the correct A2UI v0.9 inline format
  (`allOf: [{$ref: "common_types.json#/$defs/ComponentCommon"}, {properties: {component: {const}, …}}]`)
  from the zod schemas that actually render. One source, no drift, and the required-prop paint gate
  starts working.
- **(c)** Delivery. `extractCatalogComponentSchemas` runs in the browser because it needs the catalog
  object. Two routes: **(i)** let the client advertise it — the provider already pushes it as run
  context under `A2UI_SCHEMA_CONTEXT_DESCRIPTION`, and the middleware reads the client's `catalogId`
  from that entry before overwriting the schema, which is the "bring your own catalog" direction the
  design intends; or **(ii)** run the extraction in a small build-time node script and keep committing
  a generated file, which is what Aleph does now. Take (ii) first because it is a two-line change to
  `gen_catalog.py`'s consumer, then (i) once §2.3's chat path is sorted.
- This deletes `apps/copilot-runtime/src/catalog.generated.ts` and one half of
  `check-catalog-generated.sh`.

### R2 — The pane list is a five-element constant, and a fully built surface is unreachable

```
apps/web/src/lib/workspace-ui.tsx:17
    export const SURFACE_TABS = ["Wiki","Library","Notes","Hypotheses","Briefs"] as const;
    export type SurfaceTab = (typeof SURFACE_TABS)[number];
                    │
                    ├─ openPane(kind: SurfaceTab, …)              ← a pane can only be one of five
                    ├─ Rail.tsx  ICON_FOR: Record<SurfaceTab,…>   ← must stay exhaustive
                    ├─ CopilotChatSurface.tsx  z.enum(SURFACE_TABS) ← the agent's focus_tab tool
                    └─ ContextBar.tsx  cycles through the five

apps/api/src/aleph_api/routes/surfaces.py:145
    _PANE_KINDS = {"wiki","library","artifacts","notes","hypotheses","briefs","grounding"}
                                                                              └── SEVEN
```

`GroundingSurface` is fully built on every layer — a React renderer
(`components/GroundingSurface.tsx`), a registered catalog component (`GroundingSurfaceImpl`), a server
builder (`grounding_surface_v09`), a route branch (`_grounding_messages`), an accepted pane kind — and
**nothing in the web app can open it**, because the type has no member for it. `ArtifactsSurface` is
in the same position, partly masked by "Library" mapping to the `library`/`artifacts` builder.

The irony is on the record: `Rail.tsx`'s own header comment names "a grounding inspector" as an
example of a pane the old tab shell structurally could not do. The new shell still cannot.

**Fix, in order:**

- **(a) Separate "what the rail launches" from "what a pane may be."** Replace the union with a
  registry:

  ```ts
  interface PaneKindEntry {
    id: string;                 // "wiki" | "grounding" | "scholar.timeline"
    title: string;
    icon: IconName;
    launchable: boolean;        // does it get a rail button?
    params?: string[];          // e.g. ["claim_id"]
    catalogId: string;          // which plugin catalog its surfaces name
  }
  const PANE_KINDS = new Map<string, PaneKindEntry>();
  export const SURFACE_TABS = [...PANE_KINDS.values()].filter(e => e.launchable);
  ```

  `Rail` iterates the launchable subset; `ICON_FOR` becomes `entry.icon` and stops needing to be
  exhaustive; `focus_tab`'s enum is derived, not written.
- **(b) Register `grounding` as `launchable: false, params: ["claim_id"]`** and dispatch it from
  `ClaimCard`'s open action — which is what the server's own comment says the design intended.
- **(c) A plugin then adds a pane** by adding a registry entry and a server builder. No union to edit,
  no `Record<…>` to keep exhaustive, no compile error to chase.

This is the change that turns "panes" from a closed set into the plugin surface, and it should land
before any catalog work, because it is what makes a plugin's main surface *reachable at all*.

### The sequence

```
 ┌─────────────────────────────────────────────────────────────────────────────────────┐
 │ needs NO CopilotKit upgrade and NO decision about apps/copilot-runtime              │
 ├──────┬──────────────────────────────────────────────────┬─────────┬─────────────────┤
 │ Step │ What                                             │ Size    │ Unblocks        │
 ├──────┼──────────────────────────────────────────────────┼─────────┼─────────────────┤
 │  0   │ R0 — one catalog object, functions included       │ ~10 LOC │ chat expressions│
 │  1   │ R2 — PaneKind registry; grounding reachable       │ ½ day   │ plugin panes    │
 │  2   │ R1 — agent-facing schema from the render catalog  │ ½ day   │ ALL inputs      │
 │  3   │ Settings-card generator (manifest → A2UI)         │ 1–2 days│ THE DEMO        │
 │  4   │ Per-plugin catalogs: MessageProcessor(list)       │ 1 day   │ namespacing     │
 │      │   panes = 1 line · chat = merge-and-remount       │         │                 │
 │  5   │ Kernel-backed activate/deactivate + blast radius  │ ~2 days │ the guardrail   │
 ├──────┴──────────────────────────────────────────────────┴─────────┴─────────────────┤
 │ needs a decision first                                                              │
 ├──────┬──────────────────────────────────────────────────┬─────────┬─────────────────┤
 │  6   │ openGenerativeUI + read-only sandboxFunctions     │ 1 day   │ tier-3 shape    │
 │      │   (client-side only, no upgrade — but a posture   │         │                 │
 │      │    decision about generated HTML in the product)  │         │                 │
 │  7   │ MCP Apps, first-party servers only                │ ~1 day  │ third-party UI  │
 │      │   (requires apps/copilot-runtime to survive;      │         │                 │
 │      │    read §1.6 on isolation before enabling)        │         │                 │
 │  8   │ Upgrade to 1.68 → filterCatalog, auto-mounted     │ ~1 week │ sub-plugin      │
 │      │   a2ui, onAction interceptor                      │         │ granularity     │
 └──────┴──────────────────────────────────────────────────┴─────────┴─────────────────┘
```

**Steps 0–5 are the owner's design, built, with no upgrade and no runtime decision.** That is the most
useful sentence in this document. The Node-service question, the 1.68 upgrade, and the MCP-Apps
question are all real and all *downstream* of a working per-plugin catalog — none of them gates it.

---

## Appendix — evidence index

| Claim | Verified at |
|---|---|
| `Catalog(id, components[], functions[], themeSchema?)`; duplicate names silently overwrite | `node_modules/.pnpm/@a2ui+web_core@0.10.0/…/src/v0_9/catalog/types.js:35-48` |
| `MessageProcessor(catalogs: Catalog<T>[])` | `…/src/v0_9/processing/message-processor.d.ts` |
| Surface routing: `this.catalogs.find(c => c.id === catalogId)`; throws `Catalog not found` | `…/src/v0_9/processing/message-processor.js:206-218` |
| A surface binds to ONE catalog for life | `…/src/v0_9/state/surface-model.js:37-39` |
| Unknown component paints `Unknown component: X`, does not throw | `@a2ui/react@0.10.0/v0_9/index.js:78-84` |
| `A2uiClientCapabilities { supportedCatalogIds[], inlineCatalogs[] }` | `@a2ui/web_core@0.10.0/src/v0_9/schema/client-capabilities.d.ts` |
| basicCatalog = 18 components incl. all 5 inputs; `BASIC_FUNCTIONS` = 25 | `@a2ui/react@0.10.0/v0_9/index.js` (`var basicComponents`); `@a2ui/web_core/.../basic_functions_api.js` |
| Inputs bind on `value`; `Button.action.event.context` resolves paths at click | `…/basic_catalog/components/basic_components.js`; `@ag-ui/a2ui-toolkit` `DEFAULT_GENERATION_GUIDELINES` |
| `A2UIProvider` builds `new MessageProcessor([catalog ?? basicCatalog], …)` once into a ref | `@copilotkit/a2ui-renderer@1.58.0/dist/react-renderer/core/A2UIProvider.mjs:27` |
| `extractCatalogComponentSchemas` IS installed at 1.58 | `@copilotkit/a2ui-renderer@1.58.0/dist/react-renderer/catalog-utils.d.mts` |
| `filterCatalog` / `setCatalogComponents` / `isCatalogComponentEnabled` are NOT installed | `grep -rl` over the `dist/` of `@copilotkit/{a2ui-renderer,react-core,core}@1.58.0` → zero hits |
| `openGenerativeUI { sandboxFunctions, designSkill }` and `onError` exist on 1.58 provider props | `@copilotkit/react-core@1.58.0/dist/copilotkit-WlmeVijs.d.mts:2196-2330` |
| `a2ui?: { theme, catalog (singular), loadingComponent, includeSchema }` | same file |
| websandbox: `frame.sandbox = "allow-scripts …"`, no `allow-same-origin` | `@jetbrains/websandbox@1.2.1/dist/websandbox.js` `createIframe()` |
| `sandboxFunctions` → `localApi` handed to websandbox | `@copilotkit/react-core@1.58.0/dist/copilotkit-BIn7HE8f.mjs`, `OpenGenerativeUIActivityRendererInner` |
| MCP Apps frame: `srcdoc` + `sandbox="allow-scripts allow-same-origin allow-forms"`; inner sandbox set from the server's message | same file, `buildSandboxHTML` + the sandbox HTML template |
| MCP Apps: iframe→server proxy "bypasses the normal agent flow" | `@ag-ui/mcp-apps-middleware@0.0.3/dist/index.d.mts:81-87` |
| MCP Apps 0.0.3 has no `includeTools`/`excludeTools` | `.agents/skills/runtime/references/wiring-mcp-apps-middleware.md` |
| `A2UIValidationCatalog` reads `required`/`properties` at the top level | `@ag-ui/a2ui-toolkit@0.0.4/dist/index.d.mts:28-37` |
| Middleware `getValidationCatalog()` passes `config.schema.components` straight through | `@ag-ui/a2ui-middleware@0.0.10/dist/index.mjs` |
| Server-side `a2ui.schema` replaces a frontend-provided one; client `catalogId` read first | same file, `injectSchemaContext` and `Ne(t)` |
| `FilterToolCallsMiddleware` IS installed | `@ag-ui/client@0.0.57/dist/index.d.mts:408-416` |
| Aleph's two `aleph://v1` catalogs disagree about functions | `apps/web/src/a2ui/A2UISurfaceView.tsx:57` vs `SurfaceStreamProvider.tsx:35`; consumer at `lib/copilot.tsx:27` |
| Agent-facing catalog = 19 components, no inputs | `apps/copilot-runtime/src/catalog.generated.ts` (parsed) |
| `catalog.json` = `{type,id,props,…}` envelope + actions; `validate_component` consumers | `packages/aleph-a2ui/src/aleph_a2ui/catalog.py:77-98`; `apps/api/src/aleph_api/routes/cards.py:123`, `a2ui_handlers.py:933` |
| `FormCard` has no initial values | `apps/web/src/a2ui/components/FormCard.tsx:19` |
| `SURFACE_TABS` = 5; server `_PANE_KINDS` = 7; grounding unreachable | `apps/web/src/lib/workspace-ui.tsx:17`; `apps/api/src/aleph_api/routes/surfaces.py:136,145` |
| `render_a2ui` appears only in the Python system prompt | `apps/api/src/aleph_api/copilot_agent.py:95,103`; `subagents/viz_builder.py:9` |
| Kernel guardrail: `support_set`, `BlastRadius`, `AgentPluginAPI.disable`, `ProtectedCapability` | `packages/aleph-kernel/src/aleph_kernel/{support,agent_api,errors,spec}.py` |
| Aleph already ships an A2UI-over-MCP server | `packages/aleph-a2ui/src/aleph_a2ui/mcp_server.py` |
| Four bands, their APIs and "best for" | https://www.copilotkit.ai/generative-ui-spectrum (fetched 20 Aug 2026) |
