# A2UI — current state, and whether it is still the right bet

Research date: **19 August 2026**. Everything below was checked against live sources on that date
(GitHub API, npm registry, PyPI, the project's own `main` branch, and press coverage). Version
numbers and dates are stated so you can re-check them cheaply later.

---

## In one paragraph

A2UI is a **file format that lets an AI agent describe a user interface without writing any code**.
The agent sends JSON that says "a Card, containing a Column, containing a Text bound to
`/claim/summary` and a Button whose action is `approve`". Your app already contains the real
components; A2UI only names them and wires up the data. Because the agent can only name components
you have pre-approved (a list called a **catalog**), it cannot inject scripts or invent widgets, so
this is safe in a way that "let the model write HTML/JS" is not. It is currently at spec **v0.9.1
stable**, with **v1.0 as a release candidate**, lives at `github.com/a2ui-project/a2ui` (16.2k stars,
commits landing daily), and is still overwhelmingly staffed by Google engineers despite being moved
out of the `google` GitHub org. **Aleph is on the current production spec, not a dated one** — but
Aleph's own catalog file is not an A2UI catalog, and that single fact blocks most of the interesting
things A2UI can now do, including the runtime plugin story Aleph is being rebuilt around.

---

## 0. Identity check — is this the same project Aleph depends on?

**Yes. Confirmed, with the chain of evidence.**

Aleph's `apps/web/package.json` depends on `@a2ui/react ^0.10` and `@a2ui/web_core ^0.10`. The
installed `node_modules/@a2ui/react/package.json` (v0.10.0) declares:

- `"license": "Apache-2.0"`
- `"homepage": "https://a2ui.org/"`
- `"repository.url": "git+https://github.com/google/A2UI.git"`, directory `renderers/react`

`github.com/google/A2UI` now **redirects to `github.com/a2ui-project/a2ui`** — the project was moved
into its own GitHub organisation. `a2ui.org` resolves (HTTP 200) and is the same project's docs site.
So the npm packages Aleph installs, the `a2ui.org` docs, and the `a2ui-project/a2ui` repository are
one project.

**Do not confuse A2UI with AG-UI.** They are different things with confusingly similar names, and
they are *complementary, not competing*:

| | **A2UI** (Agent-to-User Interface) | **AG-UI** (Agent-User Interaction Protocol) |
|---|---|---|
| What it is | A **content format** — what the UI *is* | A **transport/event protocol** — how agent output *reaches* the browser |
| Analogy | HTML | HTTP + the event stream |
| Origin | Google (Dec 2025) | CopilotKit |
| npm | `@a2ui/react` 0.10.2 | `@ag-ui/core` 0.0.58 (2026-08-14) |
| Aleph uses | Yes, directly | Yes, indirectly via CopilotKit 1.68.1 |

A2UI's own docs list AG-UI as a **transport** for A2UI, marked "✅ Complete — day-zero
compatibility". Aleph already runs both, and that is the intended combination.

---

## 1. What problem it solves, and what people did before

Before A2UI there were three options for an agent that wanted to show something richer than text:

1. **Pre-built components only.** The agent picks from a fixed menu — `show_chart`, `show_table`.
   Safe, fast, and rigid. Every new shape of answer needs a frontend release.
2. **The agent writes HTML/JavaScript.** Maximally flexible, and a security review blocker: you are
   executing model-authored code in your app's origin. This is what "generative UI" originally
   meant, and it is why most enterprises refused to ship it.
3. **Bespoke server-driven UI.** Every company invented its own JSON-to-widget dialect (Airbnb's
   Ghost Platform, Adaptive Cards, etc.). Works, but is per-company and non-portable — a UI produced
   by one system cannot render in another.

A2UI is option 3 turned into an **open, cross-vendor standard** with option 1's safety properties.
The agent emits a description of intent; your client maps that description onto components you
already own and trust. The same JSON can render in React, Flutter, Angular, Lit, SwiftUI or Jetpack
Compose. The pitch, in the project's own words, is UI that is **"safe like data, but expressive like
code."**

Two design choices matter more than the rest:

- **Flat adjacency list, not a nested tree.** Components are a flat list with ID references
  (`{"id":"btn1","component":"Button","child":"txt1"}`), not deep nesting. This is deliberately
  LLM-friendly: a model can emit components one at a time and the renderer can paint each as it
  arrives, so the UI streams in progressively instead of appearing all at once at the end.
- **Data model separate from component tree.** Props can be *bindings* (`{"path":"/user/name"}`)
  into a shared JSON data model rather than literals. Updating a value is a tiny `updateDataModel`
  message, not a re-emission of the layout. This is what makes live-progress UIs cheap.

---

## 2. Current state as of 19 August 2026

### Specification versions

| Spec version | Status | Notes |
|---|---|---|
| v0.8 | Legacy | The initial public release, announced **15 December 2025** |
| v0.9 | Prior | Feature-complete, legacy support |
| **v0.9.1** | **Current stable** | What production renderers implement |
| **v1.0** | **Release candidate** | Spec is written; **no renderer implements it yet** |

A detail worth knowing because it causes confusion: **v1.0 was drafted under the name "v0.10"** and
renamed. The `specification/v1_0/README.md` says so explicitly. This is *unrelated* to the npm
packages being at 0.10.x — package versions and spec versions are independent number lines, and
conflating them is the easiest mistake to make here.

### Published artifacts (checked against the registries)

| Package | Latest | Published | Notes |
|---|---|---|---|
| `@a2ui/react` (npm) | **0.10.2** | 2026-07-17 | Aleph has 0.10.0 installed |
| `@a2ui/web_core` (npm) | **0.10.6** | 2026-08-03 | Aleph has 0.10.0 installed |
| `@a2ui/angular` (npm) | 0.10.5 | 2026-08-03 | |
| `@a2ui/lit` (npm) | 0.10.3 | 2026-08-03 | |
| `a2ui-agent-sdk` (PyPI) | **0.5.0** | 2026-07-31 | Python agent SDK — Aleph does not use it |
| `a2ui-core` (PyPI) | 0.1.1 | 2026-07-08 | Split out of the agent SDK at 0.3.0 |

All npm renderer packages expose only `./v0_8` and `./v0_9` entry points. **There is no v1.0 web
renderer on npm, and none in the repository either** — `renderers/react/src/`,
`renderers/web_core/src/`, `renderers/lit/src/` and `renderers/angular/src/` all top out at `v0_9`.

### Repository health

| Signal | Value |
|---|---|
| Repo | `a2ui-project/a2ui` (Apache-2.0) |
| Created | 2025-09-24 |
| Last push | **2026-08-19** (today) |
| Stars / forks | 16,155 / 1,266 |
| Open issues | 317 |
| Commits on main | ~1,100 |
| Git tags | only `v0.8`, `v0.9` |
| GitHub Releases | **none published** |

Recent commit stream (sampled 14–19 Aug 2026) shows work on a **Swift renderer**, **Kotlin**
tooling, file-upload payload optimisation, sandboxed-iframe hardening, and a spec change renaming
`callableFrom` → `allowedCallers`. This is a healthy, high-tempo project, not a dormant one.

**Momentum: gaining, but the backing is narrower than it appears.** Top contributors by commit count
are `gspencergoog` (180), `jacobsimionato` (134), `nan-yu` (95), `sugoi-yuzuru` (81), `josemontespg`
(67), `paullewis` (65) — predominantly Google/Flutter engineers. Moving to the `a2ui-project` org is
cosmetic neutrality, not structural neutrality.

**Governance — an important negative finding.** Google's sibling protocol **A2A joined the Linux
Foundation's Agentic AI Foundation (AAIF) in August 2026**. **A2UI has not.** As of today A2UI has no
foundation home, no published governance document, no steering committee, and no tagged releases.
Compare with MCP Apps, which went through a public SEP process and reached **Final on 26 January
2026**. A2UI is an open-source project with Google's hand on the tiller, not yet a neutrally
governed standard. That is the single biggest strategic risk in depending on it.

---

## 3. What it can do today — the full capability surface

Most write-ups cover roughly the first two bullets. The rest is where the leverage is.

**The basics everyone knows**

- **Catalog-constrained components.** The agent may only name components you registered. The
  standard "Basic" catalog has 18: `Text, Image, Icon, Video, AudioPlayer, Row, Column, List, Card,
  Tabs, Modal, Divider, Button, TextField, CheckBox, ChoicePicker, Slider, DateTimeInput`.
- **Data binding.** Props may be JSON-Pointer bindings into a shared data model, updated
  independently of layout.

**Less widely known, and shipping today**

- **Catalog functions.** A catalog declares *functions* the renderer executes locally — not just
  components. The basic catalog ships 14: `required, regex, length, numeric, email, formatString,
  formatNumber, formatCurrency, formatDate, pluralize, openUrl, and, or, not`. Form validation, date
  and currency formatting, and string interpolation all run **client-side with no agent round-trip**.
  Most integrations (Aleph included) never register a single function.
- **Two-way data sync.** Since v0.9 the protocol is bidirectional: the renderer can push data-model
  changes back to the agent. Collaborative editing works without bespoke plumbing.
- **`instructions` embedded in the catalog.** As of v1.0 a catalog carries a Markdown `instructions`
  field with design rules and usage guidance. The catalog becomes **self-documenting to the LLM** —
  the prompt guidance ships with the contract instead of drifting away from it in a separate file.
- **Prompt generation from the catalog.** The agent SDKs expose
  `schema_manager.generate_system_prompt(...)` and `catalog.renderAsLlmInstructions()`, which build
  the system prompt from the catalog schema plus few-shot examples, and can **prune** the schema to
  a subset of components to save tokens. You do not hand-write the "here are your components" prompt.
- **Streaming parsers with JSON healing.** `a2ui-agent-sdk`'s `parser/streaming.py` and
  `payload_fixer.py` incrementally parse half-finished model output and auto-repair the usual LLM
  failures (smart quotes, trailing commas) before validation.
- **A conformance suite.** `conformance/` holds versioned YAML test vectors covering catalog,
  validator, accessibility, parser, streaming parser and inference format, with a TypeScript harness
  (added 2026-08-14). You can prove a custom renderer is compliant.
- **An eval harness.** `eval/` runs on **Inspect AI** and measures whether a given model can actually
  produce valid A2UI for a scenario. Datasets are **encrypted at rest with Transcrypt specifically to
  prevent base-model contamination** — a serious methodological touch. There is also an
  `iterative_format_optimizer` that tunes the wire format for model accuracy.
- **MCP in both directions.** `a2ui_over_mcp.md` covers returning A2UI JSON from MCP tool calls
  (MIME `application/a2ui+json`). `mcp-apps-in-a2ui.md` covers **hosting MCP Apps inside an A2UI
  surface** using a hardened **double-iframe** pattern: an intermediate same-origin `sandbox.html`
  proxy, then an inner `srcdoc` iframe with `sandbox="allow-scripts allow-forms allow-popups
  allow-modals"` and — load-bearing — **never `allow-same-origin`**, because `allow-scripts` plus
  `allow-same-origin` lets a frame remove its own sandbox attribute and escape.
- **Accessibility as a first-class, specified concern.** v1.0 adds WAI-ARIA `live` regions
  (`off`/`polite`/`assertive`) and `hidden` to `AccessibilityAttributes`, with normative prose
  requiring renderers to plumb them and infer defaults from visible text.

**The one nearly nobody knows: A2UI Express**

`specification/inference_formats/express/` and `specification/proposals/express/` define **A2UI
Express**, a compact line-oriented DSL that a model emits *instead of* JSON, which a host-side
compiler turns into A2UI v1.0 wire payloads. It has a real ANTLR grammar (`Express.g4`), a compiler,
a decompiler, and 36 worked examples.

```
<a2ui>
header  = Text("Flight AA100")
status  = Text($/flight/status)
root    = Card(child=Column([header, status]))
</a2ui>
```

The claimed win, from the spec: **55–70% fewer output tokens than native A2UI JSON**, designed so
small local models (it names Gemma 4 E2B/E4B) can produce UI within a tight context budget, and
line-oriented so the host parses and paints line by line as the model streams. A sibling proposal,
**Atom**, explores the same idea differently. This directly targets the two things that make
agent-generated UI feel bad — cost and time-to-first-render.

---

## 4. What changed in the last 6–12 months

**v0.8 → v0.9 (renderer packages published April 2026; publicised mid-2026).** Not a point release.
Per InfoQ and the project's own notes: the core philosophy changed, the JSON structure changed, the
schema changed, and **the protocol became bidirectional**. The "Standard" component set was renamed
**"Basic"** to push people toward their own catalogs rather than the generic one. Client-defined
validation functions, client-to-server data sync, better error handling, and a modular schema
arrived. Transports expanded to MCP, WebSockets, REST, AG-UI and A2A 1.0. A Python agent SDK with
caching and resilient streaming appeared.

**v0.9 → v1.0 (release candidate, spec complete, renderers pending).** From
`specification/v1_0/docs/evolution_guide.md`, the changes that matter for Aleph:

1. **Bidirectional RPC.** `callRendererFunction` (agent → renderer, answered by
   `rendererFunctionResponse`) and `callAgentFunction` (renderer → agent, answered by
   `agentFunctionResponse`). Catalogs declare `allowedCallers` (`rendererOnly` / `agentOnly` /
   `rendererOrAgent`) and `requiresUserActivation`, enforced at runtime; violations return
   `INVALID_FUNCTION_CALL`.
2. **Mixable catalogs — the headline feature for plugin systems.** Multiple catalogs can be combined
   *within a single surface*. `catalogId` becomes an optional property on individual components and
   function calls, with a strict resolution order: component-level `catalogId` → surface default →
   **error, with no fallback**. All mixed catalogs must share a spec version.
3. **`inlineCatalogs`.** A renderer advertises `supportedCatalogIds` *and* may negotiate catalogs
   delivered inline at runtime; agents advertise `acceptsInlineCatalogs` on their Agent Card.
4. **Single-message surfaces.** `createSurface` can now carry `components` and `dataModel` inline —
   an entire UI in one message instead of create-then-update.
5. **Composition constraints.** Catalogs declare `allowedParents` / `allowedChildren`, with
   `"Surface"` as the reserved canonical root. New error codes `UNALLOWED_PARENT` / `UNALLOWED_CHILD`.
6. **Theming removed.** `theme` and `primaryColor` are gone from both catalog and `createSurface`,
   deliberately separating layout from branding.
7. **`updateDataModel.value` is now required**, and setting it to `null` is how you delete a key.
   Omitting `value` is a schema error. **This is a silent breaking change** — code that deleted keys
   by omission will now fail validation.
8. **Terminology renamed globally: client → renderer, server → agent.** Every schema file was
   renamed (`server_to_client.json` → `agent_to_renderer.json`, and so on).
9. **MIME type changed** from `application/json+a2ui` to **`application/a2ui+json`** (IANA-conformant).
10. **`@index`** built-in for list-template iteration; `@` reserved for system functions.
11. **Structured `ValidationResult`** (`valid`, `code`, `message`, `severity`) returned by validation
    functions instead of a bare boolean.
12. **`metadata.extensions`** on components, surfaces and definitions, with the `a2ui_` namespace
    reserved.
13. **UAX #31 identifier rules** enforced on all component, function and argument names.
14. **`protocolVersion` in catalogs**, defaulting to `"0.9"` when absent.

**In flight right now (open PRs, checked today):** `#2257 feat(web_core): v1.0 Zod schemas, version
adapters, and composition constraints` (opened 2026-08-19) and `#2303 feat(python): generate Pydantic
models and basic catalogs for spec v0.8 and v1.0` (2026-08-18). v1.0 conformance vectors landed
2026-08-14 (`#2255`, `#2277`). v1.0 web renderer support looks like weeks-to-months away, not
vapourware.

---

## 5. How it relates to the neighbours

The useful mental model, borrowed from OpenUI's *State of Generative UI* report and confirmed against
each project's docs: **transport and generation are independent choices.**

- **Transport** — where the UI appears: **AG-UI** (into an app you control) vs **MCP Apps** (into
  ChatGPT/Claude/Cursor, which you don't control) vs **A2A** (agent-to-agent meshes).
- **Generation** — what the agent emits: *static* (pick a pre-built component) vs *declarative*
  (compose a spec from your catalog — A2UI, json-render, OpenUI Lang) vs *open-ended* (raw HTML/JS).

| Project | What it is | Status (Aug 2026) | Relationship to A2UI |
|---|---|---|---|
| **AG-UI** | Transport/event protocol, by CopilotKit | `@ag-ui/core` 0.0.58 (2026-08-14); adopted by LangGraph, CrewAI, Mastra, LlamaIndex, Pydantic AI | **Composes.** A2UI's docs mark AG-UI transport "✅ Complete, day-zero". Aleph already uses both. |
| **A2A** | Agent-to-agent protocol | v1.0 March 2026; **joined AAIF Aug 2026** | **Composes.** A2UI defines an A2A extension with Agent Card metadata and two-way data sync. |
| **MCP Apps (SEP-1865)** | Servers ship interactive **HTML** rendered in a host's sandboxed iframe | **Final since 2026-01-26**; first official MCP extension; authored by MCP maintainers at OpenAI + Anthropic with mcp-ui contributors. Adopted by Claude Desktop, ChatGPT enterprise tiers, Cursor | **Competes philosophically, composes technically.** Different security model — sandboxed HTML vs declarative JSON. But A2UI can *host* MCP Apps inside a surface via the double-iframe pattern, and can *be served over* MCP. |
| **Vercel json-render** | Declarative generative UI framework, catalogs defined with Zod | Launched Jan 2026, Apache-2.0, 13k+ stars, 200+ releases; `@json-render/react` 0.20.0 (2026-08-16); renderers for React/Vue/Svelte/Solid/React Native; 36 shadcn components; PDF/email/video/3D targets | **Competes, and interoperates.** Ships an A2UI adapter (`json-render.dev/docs/a2ui`). Framed as a *tool* coupled to one app's components; A2UI as a *protocol* for cross-agent portability. |
| **OpenUI / OpenUI Lang** | Declarative generative UI optimised for token cost | Active 2026 | **Competes on wire efficiency.** Its published benchmark: OpenUI Lang 4,800 tokens vs json-render patches 10,180 for the same scenarios. A2UI's answer is Express (55–70% reduction). |

**Is there a converging standard?** Not yet, and the shape of the split is stable enough to plan
around. **MCP Apps has won the "UI inside somebody else's chat client" slot** — it is Final, it has
OpenAI and Anthropic behind it, and it is already shipping in Claude Desktop and ChatGPT. **A2UI is
the leading contender for "UI inside your own application, portable across frameworks."** They are
not really fighting over the same ground. Aleph is squarely in A2UI's slot: it owns its own frontend.

The competitive pressure A2UI actually faces is from **json-render**, which is younger (Jan 2026) yet
has more releases, more renderers, and a batteries-included shadcn component set — and from OpenUI on
token cost. A2UI's defensible advantage is portability plus the security posture that gets it through
enterprise review, which is exactly the thing json-render's tighter coupling gives up.

---

## 6. The gap: what people use it for vs what it can do

Typical usage is *"agent returns a card in a chat window."* That is maybe a quarter of the surface.
The underused capabilities, with evidence each is real:

1. **Catalog functions instead of round-trips.** Every form validation, currency format, date format
   and string interpolation can execute in the renderer. *Evidence:* 14 functions in
   `specification/v1_0/catalogs/basic/catalog.json`, with `returnType: "validationResult"` and a
   specified `ValidationResult` schema. Most integrations register none — including Aleph.

2. **A2UI Express as the model-facing format.** Have the model emit Express, compile server-side to
   JSON. *Evidence:* `Express.g4` grammar, a compiler and decompiler under
   `specification/proposals/express/scripts/`, 36 examples, and a merged performance PR (`#2131`).
   Claimed 55–70% output-token reduction with line-by-line streaming. **Caveat: this is still under
   `proposals/`, so treat the numbers as the project's own claim, not independently verified.**

3. **Catalog-generated prompts.** `generate_system_prompt(..., allowed_components=[...])` builds
   system instructions from the catalog and prunes to a component subset. Hand-written "here are your
   components" prompt sections are a drift source that this eliminates by construction.

4. **The `instructions` field.** v1.0 lets the catalog carry its own Markdown design rules. The
   contract and the guidance travel together and cannot disagree.

5. **Mixable catalogs + `inlineCatalogs`.** This is a **runtime component-registration mechanism**
   that almost nobody is using, and it is the single most relevant A2UI feature to Aleph's thesis.
   *Evidence:* the v1.0 evolution guide's mixing rules and resolution order; `acceptsInlineCatalogs`
   in `A2uiSchemaManager`; `inlineCatalogs` in `renderer_capabilities.json`.

6. **Bidirectional RPC for approvals and long-running work.** v1.0's `callAgentFunction` lets a
   rendered card call the agent directly and receive a typed response, with `allowedCallers` and
   `requiresUserActivation` enforced at runtime. Approval gates and human-in-the-loop steps stop
   needing a bespoke side-channel API.

7. **The conformance suite and eval harness as your own CI.** Point the conformance vectors at your
   renderer; point the Inspect AI eval at your catalog to measure whether *your* configured model can
   actually produce valid UI against it. For a system that auto-discovers whatever models a gateway
   offers, that second one is close to essential — it turns "does this model work here?" into a
   number.

8. **Multi-agent UI composition.** A remote sub-agent returns a UI payload that renders inside the
   host's chrome. This is the use case A2UI was built for and the one AG-UI and json-render cannot
   match, because it needs a *portable* contract rather than a local component binding.

---

## 7. Honest assessment

### Genuinely good

- **The security model is the real product.** "Declarative data, not executable code, constrained to
  a pre-approved catalog" is the argument that gets generative UI past a security review. Nothing
  else in this space has as clean a story.
- **The streaming/incremental design is correct.** Flat adjacency lists plus a separate data model
  give you progressive rendering and cheap live updates for free, rather than as an optimisation you
  bolt on later.
- **Engineering discipline is above average for a 9-month-old project.** A conformance suite with
  versioned vectors, contamination-resistant encrypted eval datasets, a formal grammar for the DSL
  proposal, and a genuinely detailed migration guide. These are not the artifacts of a demo project.
- **Multi-platform is real, not aspirational.** React, Lit, Angular and Flutter are all stable on
  v0.9.1, with Swift and Kotlin actively landing and community renderers for React Native, Lynx and
  Android.

### Immature or oversold

- **v1.0 is a spec with no implementations.** The RC is complete and detailed; zero renderers ship
  it. Anyone claiming to "use A2UI v1.0" today is using a spec document.
- **The docs contradict themselves.** `concepts/transports.md` lists REST as "📋 Planned" and
  WebSockets/SSE as "💡 Proposed"; `roadmap.md` lists REST, WebSockets *and* MCP as "✅ Complete".
  `README.md`'s roadmap says React support is still to be added while `roadmap.md` calls React
  "✅ Stable — Official React renderer". Several guides contain literal `TODO: Add an example`. Trust
  the code and the spec directory; treat the prose docs as lagging.
- **No releases, no tags, no governance.** Two git tags (`v0.8`, `v0.9`), zero GitHub Releases, no
  foundation, no published governance model. You are pinning npm versions against a moving `main`.
- **The zod v3 lock-in is a live, unresolved wart.** `@a2ui/web_core@0.10.6` depends on `zod ^3.25.76`
  and `@a2ui/react@0.10.2` declares it as a peer dependency. There are **four open upstream issues**
  about it: "Abstract dependency on Zod on web_core" (#open), "Consider making zod a peer dependency",
  "When should the zod dependency of web_core@0.9 be upgraded to version 4+?", and an RFC "Accept
  both zod 3 and 4" that was **closed** on 2026-08-04. Anyone on zod v4 must carry a dual-zod alias.
  Aleph already does, and correctly documents why.
- **Catalogs are Zod objects, not JSON Schema, on the web.** Two open issues want this fixed — #1248
  "Allow developers to define ComponentApis using raw JSON schemas rather than Zod objects" and #1421
  "Add `catalogFromJsonSchema()` utility". Until one lands, **registering a component on the web
  requires writing TypeScript and rebuilding.** This is the central blocker for runtime plugins.
- **The "catalog is restrictive" criticism is fair.** If the catalog lacks a component, the agent
  cannot express the idea at all. That is the price of the security model, and it means catalog
  design is a permanent, ongoing product responsibility rather than a one-time setup task.

### Realistic cost of depending on it

Low-to-moderate, and lower than it first appears — because **the expensive asset is not A2UI, it is
your catalog and your renderer components**, and those are yours regardless.

- **The format is simple.** Aleph's entire server-side wire implementation is **82 lines**
  (`messages.py`) covering three message kinds. Reimplementing the format is days, not months.
- **The renderer is the real dependency.** `@a2ui/web_core` does binding resolution, schema
  behaviour scraping, surface models and message processing. Replacing that is weeks.
- **Migration cost is real but bounded.** v0.9 → v1.0 will require touching the message builders, the
  MIME type, `updateDataModel` semantics, and every schema filename reference.

### Exit path if it stalls

Genuinely good, and worth stating plainly because it de-risks the bet:

1. **Your components survive.** Aleph's card and surface React components take
   `{component: {type,id,props}, onAction}`. They do not import anything from `@a2ui/*`. A different
   protocol re-binds them.
2. **Your catalog survives.** A component/action manifest is protocol-independent.
3. **Adapters exist today.** Vercel's json-render ships an A2UI adapter; `easyops-cn/a2ui-sdk` is an
   independent TypeScript/React implementation; `applegrew/a2ui-rs` is a Rust one. A2UI is no longer
   single-implementation.
4. **The wire format is easy to own.** If upstream stalls, vendor the v0.9 renderer or emit the
   format yourself. Apache-2.0 permits it.

**Verdict: A2UI is still the right bet for Aleph, with one condition** — treat the *format* as the
commitment and the *renderer package* as a replaceable implementation detail. The alternatives are
worse fits: MCP Apps solves a problem Aleph does not have (Aleph owns its frontend, and Aleph's
security posture already forbids agent-authored code in the app context); json-render couples you to
one app's components and gives up the portability that makes multi-agent UI possible; OpenUI is
narrower and less proven.

---

## 8. Fit with Aleph, and an assessment of the current integration

### How Aleph uses A2UI today

**Verified against the tree at `bcc478a`.**

- `apps/web/package.json` → `@a2ui/react ^0.10`, `@a2ui/web_core ^0.10`; `zod 4.4.3` plus a
  `zod3: npm:zod@3.25.76` alias.
- Frontend imports `@a2ui/react/v0_9` and `@a2ui/web_core/v0_9` — **the current stable spec.**
- `packages/aleph-a2ui/src/aleph_a2ui/catalog.json` — 2,093 lines; **21 components, 9 primitives,
  20 actions**; `catalogId: "aleph-v1"`, `agentCatalogId: "aleph://v1"`.
- `scripts/gen_catalog.py` generates `apps/web/src/a2ui/catalog.ts` and
  `apps/copilot-runtime/src/catalog.generated.ts`; `scripts/check-catalog-generated.sh` gates drift.
- `apps/web/src/a2ui/aleph-catalog-v09.tsx` (555 lines) hand-writes a
  `createComponentImplementation` per card using **zod v3** schemas.
- `messages.py` (82 lines) hand-rolls `createSurface` / `updateComponents` / `updateDataModel`.
- `SurfaceStreamProvider.tsx` multiplexes one SSE connection for all panes, with a server-stamped
  monotonic `seq` giving every pane one total order.
- Actions route **out of band** via `POST /v1/projects/{id}/cards/actions` → `ActionRouter`, which
  validates against the catalog, dispatches to a handler, and writes a `CardAction` row plus a ledger
  event in one transaction.
- Chat uses CopilotKit's `injectA2UITool` + `createA2UIMessageRenderer` over AG-UI.
- `mcp_server.py` already serves Aleph's catalog and surfaces over MCP as
  `application/a2ui+json`.

### Is that idiomatic or dated?

**Mostly idiomatic, current on spec, and unusually well-engineered in two places — with three
concrete problems.**

Genuinely ahead of the curve:

- **The SSE multiplexer with monotonic `seq`.** The reasoning in the file header — that independent
  connections have independent sequence spaces and can therefore render mutually inconsistent states
  with nothing detecting it — is correct and is *not* something A2UI gives you. Upstream lists SSE as
  a proposed transport; Aleph built a better version already.
- **`mcp_server.py`.** Serving your own catalog over MCP is a pattern upstream only documented
  recently. Aleph did it early.
- **The single-editable-catalog rule** with a generator and a drift check is exactly the right
  response to the three-hand-maintained-copies bug the codebase already documents.

**Problem 1 — the catalog is not an A2UI catalog.** This is the load-bearing finding.

Aleph's `catalog.json` shape is `components[Name].schema` describing a `{type, id, props}` wrapper.
A2UI's `catalog_definition.json` shape is a components map of JSON Schema definitions with a
`component` const, plus a `functions` map, `instructions`, `allowedParents`/`allowedChildren`, and
(in v1.0) `protocolVersion`. They are not the same document. Consequences:

- The catalog cannot be handed to a renderer as an `inlineCatalog`.
- It cannot be validated by A2UI's conformance suite or validator.
- It cannot generate the agent's system prompt via the SDK.
- `catalogId` is `"aleph-v1"` in the file but `"aleph://v1"` in the code, and **neither is a URL** —
  A2UI catalog IDs are URLs pointing at the catalog definition. Two identifiers for one thing, in a
  codebase whose stated rule is that things which can disagree eventually do.

**Problem 2 — a real divergence between the two catalog builders, of exactly the class the codebase
already burned a work package on.**

```
apps/web/src/a2ui/A2UISurfaceView.tsx:57      new Catalog(ALEPH_V09_CATALOG_ID, [...impls], [])
apps/web/src/a2ui/SurfaceStreamProvider.tsx:35 new Catalog(ALEPH_V09_CATALOG_ID, [...impls],
                                                 [...basicCatalog.functions.values()])
```

Same catalog ID, **different function sets**. `apps/web/src/lib/copilot.tsx:37` builds the chat
renderer from `buildAlephCatalog` — the function-less one. So a surface using `formatString`,
`required` or `email` renders correctly in a pane and fails in chat, under one shared identifier.
`ALEPH_V09_CATALOG_ID = "aleph://v1"` is also declared twice, once in each file. Nothing detects
either.

**Problem 3 — the plugin thesis and the current integration are incompatible.**

Aleph's premise is that an agent authors plugins for itself and activates them at runtime. Today,
adding one component to Aleph's UI requires: editing `catalog.json`, running `gen_catalog.py`,
hand-writing a `createComponentImplementation` with a zod v3 schema in `aleph-catalog-v09.tsx`,
writing the React view, and **rebuilding and redeploying the frontend**. An agent cannot do that at
runtime. This is not a criticism of Aleph's code — it is the only thing the v0.9 renderer supports.
It is the constraint to design against, and A2UI v1.0 plus upstream issues #1248/#1421 are exactly
the mechanism that removes it.

### The performance worry

The owner's concern that a plugin architecture will be slow does not really apply to this layer, and
A2UI helps more than it hurts:

- **Rendering is not the bottleneck.** Resolving a flat adjacency list and a data model is cheap.
  There is no per-plugin process, no IPC, no sandbox on the A2UI path. The single closed performance
  issue in the repo (#421, Jan 2026) is not evidence of a systemic problem.
- **The real cost is model output tokens and time-to-first-render.** That is what Express targets
  (claimed 55–70% reduction) and what the flat-list design targets (paint as it streams).
- **Multiple catalogs cost essentially nothing at render time.** Catalog resolution in v1.0 is a map
  lookup on `catalogId` with a defined fallback order. Ten plugin catalogs are ten map entries.
- **The one thing that would be slow** is validating every message against a JSON Schema on the hot
  path — which is why upstream cached `A2uiValidator` on `A2uiCatalog.validator` (PR #1972,
  2026-07-27). Cache the validator per catalog and this disappears.

---

## What Aleph should do

1. **Stay on A2UI. Treat the format as the commitment and `@a2ui/*` as replaceable.** Keep card
   components free of `@a2ui/*` imports (they already are) so the exit path stays open.

2. **Fix the catalog divergence now — it is the same bug class the repo already documents.** Delete
   one of the two `buildCatalog` functions, export one `ALEPH_V09_CATALOG_ID` from one module, and
   add a test asserting the chat catalog and the pane catalog have identical component *and* function
   sets. Two builders under one catalog ID that disagree about functions is a shipped defect today.

3. **Make `catalog.json` a real A2UI catalog definition.** Restructure it to
   `catalog_definition.json` shape: components as JSON Schema with a `component` const, a `functions`
   map, `instructions`, `protocolVersion`, and a **URL** `catalogId`
   (`https://aleph.research/a2ui/catalog/v1.json`) used identically everywhere. This one change
   unlocks conformance validation, SDK prompt generation, and — critically — inline catalog delivery.

4. **Register catalog functions.** Start with the basic catalog's 14 in *both* builders. Validation,
   formatting and interpolation should not cost an agent round-trip.

5. **Move the agent's prompt into the catalog.** Use the `instructions` field and
   `generate_system_prompt(allowed_components=[...])` rather than hand-written prompt sections. The
   contract and its guidance should be one artifact that cannot drift.

6. **Design the plugin system around v1.0's mixable catalogs, and build for it now.** One plugin =
   one catalog + its component implementations. Per-component `catalogId` with strict resolution and
   no fallback is exactly the isolation a plugin system needs, and the `UNALLOWED_PARENT` /
   `UNALLOWED_CHILD` codes give you composition guardrails for free. Write the v0.9 message builders
   so the v1.0 migration is a swap, not a rewrite.

7. **Solve runtime component registration explicitly, and track upstream issues #1248 and #1421.**
   Until `catalogFromJsonSchema()` exists, a runtime-authored plugin cannot add a *new component
   type* to the web renderer. Two viable bridges: (a) build the zod schema at runtime from the
   plugin's JSON Schema yourself — the same thing #1421 proposes, and a contribution worth upstreaming;
   (b) ship a small set of **generic, highly parameterised** components (Aleph's `ChartCard`,
   `TableCard`, `FormCard`, `HtmlFrameCard` already are these) that runtime plugins configure rather
   than extend. Do (b) now; do (a) when you need genuinely new component types.

8. **Evaluate A2UI Express before v1.0 lands.** Aleph connects to arbitrary gateways including small
   local models. A 55–70% output-token reduction with line-by-line streaming is worth measuring
   against Aleph's own surfaces. It is a proposal, so measure it rather than believing the number.

9. **Adopt the upstream conformance suite and eval harness.** Run the conformance vectors against
   Aleph's renderer in CI. Run the Inspect AI eval against Aleph's catalog for each newly discovered
   model — this turns "can this gateway's model produce valid Aleph UI?" into a number, and it fits
   the existing `aleph-evals` pattern and the `ModelProfile` autoconfigure flow exactly.

10. **Adopt the double-iframe pattern for `HtmlFrameCard` and code-runner artifacts.** Aleph already
    renders agent artifacts in a `sandbox` iframe. The upstream pattern — same-origin proxy frame,
    inner `srcdoc` frame, **never `allow-same-origin`**, no `allow-top-navigation`, intercept anchor
    clicks — is a hardened version of that. Check Aleph's current iframe attributes against it.

11. **Plan the v1.0 migration as a discrete work package.** Wire version `"v1.0"`; MIME
    `application/a2ui+json`; **`updateDataModel.value` becomes required and `null` means delete**;
    `theme` removed; schema file renames; `client`/`server` → `renderer`/`agent` in every name and
    comment. Sequence it behind PR #2257 landing a v1.0 web renderer.

12. **Pin exact `@a2ui/*` versions, not `^0.10`.** A project with no releases, no tags and daily
    commits to `main` should not be tracked with a caret range. Aleph has 0.10.0 installed while
    0.10.2 / 0.10.6 are current — decide that deliberately rather than by lockfile accident.

---

## What Aleph should avoid

1. **Do not upgrade `@a2ui/*` expecting the zod v3 pin to have gone away.** It has not, in any
   published version, and the "accept both zod 3 and 4" RFC was **closed** on 2026-08-04. Keep the
   `zod3` alias and keep the excellent comment in `aleph-catalog-v09.tsx` explaining why. Re-check
   only when issue "Abstract dependency on Zod on web_core" closes.

2. **Do not adopt v1.0 before a renderer ships it.** The spec is complete; the implementations are
   not. Track PR #2257 (`web_core` v1.0 Zod schemas, opened 2026-08-19). Read the evolution guide now,
   migrate later.

3. **Do not confuse A2UI versions with `@a2ui/*` package versions.** `@a2ui/react@0.10.2` implements
   spec **v0.9**, not "v0.10". Spec v1.0 was *drafted* as "v0.10" and renamed. Any code, comment or
   doc that treats `^0.10` as meaning "spec 0.10" is wrong.

4. **Do not build a second bespoke UI-description format alongside A2UI.** Aleph's `catalog.json` is
   already halfway to one. Converge it onto the A2UI catalog definition rather than growing a private
   dialect that upstream tooling can never touch.

5. **Do not assume A2UI is a neutrally governed standard.** It is not, today. A2A joined AAIF in
   August 2026; A2UI did not. No foundation, no governance doc, no releases, no tags, and a
   contributor list dominated by Google. Keep the exit path real: components must not import
   `@a2ui/*`, and the wire builders must stay small enough to own.

6. **Do not chase MCP Apps as a replacement.** It is Final and well-backed, but it solves "render UI
   inside a chat client you don't control." Aleph owns its frontend, and shipping model-authored HTML
   into the app context contradicts Aleph's own stated security posture. Use MCP Apps only to *host*
   third-party tools inside an Aleph surface, via the double-iframe pattern — not as the primary UI
   layer.

7. **Do not let plugin catalogs share one `catalogId`.** v1.0's isolation is entirely keyed on
   `catalogId` with **no fallback** on resolution failure. One shared ID across plugins throws away
   the isolation and reintroduces the drift problem at plugin scale.

8. **Do not skip the consumer.** The house rule applies with full force here: a catalog entry with no
   registered renderer implementation is an empty pane, and a renderer implementation with no producer
   is dead code. The generated `COMPONENT_NAMES` type contract makes a missing renderer a compile
   error — preserve that property through any restructuring, because it is the thing currently doing
   the most work.

---

## Sources

- [a2ui-project/a2ui](https://github.com/a2ui-project/a2ui) — repo, spec directory, conformance
  suite, eval harness, roadmap, evolution guide (read from `main`, 2026-08-19)
- [A2UI docs](https://a2ui.org/)
- [Introducing A2UI — Google Developers Blog](https://developers.googleblog.com/introducing-a2ui-an-open-project-for-agent-driven-interfaces/) (2025-12-15)
- [Google Releases A2UI v0.9 — InfoQ](https://www.infoq.com/news/2026/07/google-a2ui-genui/)
- [SEP-1865: MCP Apps — Model Context Protocol](https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp)
- [The 2026-07-28 MCP Specification Release Candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)
- [A2A joins AAIF — Agentic AI Foundation](https://aaif.io/blog/a2a-joins-aaif)
- [Exclusive: AI agents inch toward interoperability — Axios](https://www.axios.com/2026/08/17/a2a-agentic-ai-foundation-open-ai-standards)
- [Vercel Releases JSON-Render — InfoQ](https://www.infoq.com/news/2026/03/vercel-json-render/)
- [vercel-labs/json-render](https://github.com/vercel-labs/json-render)
- [The State of Generative UI in 2026 — OpenUI](https://www.openui.com/blog/state-of-generative-ui-report)
- [Agent UI Standards Multiply: MCP Apps and Google's A2UI — The New Stack](https://thenewstack.io/agent-ui-standards-multiply-mcp-apps-and-googles-a2ui/)
- [AG-UI docs](https://docs.ag-ui.com/introduction)
- npm registry (`@a2ui/*`, `@ag-ui/*`, `@copilotkit/*`, `@json-render/react`) and PyPI
  (`a2ui-agent-sdk`, `a2ui-core`), queried 2026-08-19
