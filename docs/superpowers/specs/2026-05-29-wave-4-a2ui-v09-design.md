# Wave 4 — A2UI v0.9 protocol + Generic Binder + delta updates (design)

> **⚠️ SUPERSEDED (2026-05-29)** by `2026-05-29-wave-4-a2ui-v09-refresh-design.md`, which is grounded in the actually-installed `@a2ui` 0.10 API (v0_9 subpath, `MessageProcessor`/`A2uiSurface`, the zod3-binder requirement). Wave 4 SHIPPED: one shared v0_9 catalog drives both the right panel and the chat; backend emits v0_9 messages + an SSE delta `SurfaceStreamer`; the homegrown renderer + duplicate chat catalog were retired. Kept for history.

Status: **SUPERSEDED** (originally "planned"). Read "References" first.

## Why this wave

Aleph's right-panel renderer (`apps/web/src/a2ui/register.tsx`) is a homegrown
walk-and-dispatch over a bare-JSON component tree (`A2UIComponent` =
`{type, id, props, children}`). It works, but it is **not** the A2UI v0.9
message protocol: there is no `createSurface` / `updateComponents` /
`updateDataModel` message flow, no Generic Binder (path bindings like
`{path: "/claims/0/confidence"}`), and no incremental/delta updates — every
surface re-emits the whole tree. The installed deps are already v0.10:
`@a2ui/react@^0.10`, `@a2ui/web_core@^0.10` (in `apps/web/package.json`) — but
they are **not used**; only CopilotKit's A2UI renderer is wired (W2, for the
Live chat — see below).

Upgrading unlocks: reactive surfaces (patch one claim's confidence without
re-rendering the panel), agent-streamed partial updates, and schema validation
at the renderer. It's foundational for richer cards but mostly under-the-hood.

## Important interaction with W2 (read before planning)

W2 already wired a **separate** A2UI path for the **Live chat**:
`@copilotkit/a2ui-renderer`'s `createCatalog` + `createA2UIMessageRenderer`
(see `apps/web/src/a2ui/copilot-catalog.tsx`, `lib/copilot.tsx`). That renders
the agent's `render_a2ui` tool output. There are now effectively **two A2UI
renderers**: (a) the homegrown right-panel one, (b) CopilotKit's chat one.

W4 decision to make up front: do we (i) converge both onto upstream
`@a2ui/react` v0.9/0.10, or (ii) leave the chat path on CopilotKit's renderer
and only upgrade the right panel? CopilotKit's `createCatalog` builds a
`Catalog<ReactComponentImplementation>` from `@a2ui/web_core/v0_9`, so the two
are version-adjacent. **Recommend:** upgrade the right panel to upstream v0.9
first (lower risk), keep the chat on CopilotKit, then evaluate convergence.
Note the **zod v3↔v4 boundary**: `@copilotkit/a2ui-renderer` types are built
against zod v3 while the app uses zod 4.4.3 — W2 casts at that boundary
(`copilot-catalog.tsx`). Upstream `@a2ui/react@0.10` peers `zod@^3.23.8`
(install warns). Plan for the version skew.

## What ships

### W4.1 — Replace the right-panel renderer
In `apps/web/src/a2ui/register.tsx`, swap walk-and-dispatch for:
`MessageProcessor` from `@a2ui/web_core` + `<A2uiSurface>` from `@a2ui/react`.
`<A2UIRightPanel>` (`apps/web/src/components/A2UIRightPanel.tsx`) instantiates
a `MessageProcessor`, feeds it the surface messages from the API, and renders
each surface via `<A2uiSurface>` against the Aleph catalog.

### W4.2 — Backend emits v0.9 messages
`packages/aleph-a2ui/src/aleph_a2ui/surface.py` → emit
`createSurface` / `updateComponents` / `updateDataModel` instead of bare JSON.
Add a SurfaceStreamer that emits **delta** `updateDataModel` (e.g. new claim →
patch `/claims/-` rather than re-emit). The surface route is
`apps/api/src/aleph_api/routes/surfaces.py` (currently returns a whole
`{tab, surface}` blob per `GET /surfaces/{tab}`).

### W4.3 — Catalog: Zod schemas + bindings
`apps/web/src/a2ui/catalog.ts` → re-express the 17 components as Zod schemas;
props become literal-or-binding (`{path: "/claims/0/confidence"}`) via the
Generic Binder. Unlocks reactive updates without full re-render.

### W4.4 — Verification
- Existing `05-charts-tables-graphs.spec.ts` (4 viz tests) still pass.
- New spec: backend emits a partial `updateComponents`; assert only the
  targeted node re-renders (render-count probe).

## References — consult these for correct, current APIs

- **A2UI website / spec — the authoritative protocol source:** `a2ui.org`
  (and the v0.9 catalog/spec JSON it links, e.g.
  `https://a2ui.org/specification/v0_9/basic_catalog.json` — this exact URL is
  what the agent stamps as `catalogId` when NOT using a custom catalog; W2 ran
  into this). Read the v0.9 message-protocol section (createSurface /
  updateComponents / updateDataModel) and the Generic Binder section.
- **Local repo — `~/code/A2UI`** (the reference clone; CLAUDE.md "Notable
  references"): A2UI core + React renderer + Python agent SDK. This is the
  source of truth for `MessageProcessor`, `<A2uiSurface>`, catalog format, and
  the binder. We consume from npm/PyPI but the clone is the
  tracked-upstream-latest reference. **Verify the installed `@a2ui/react` /
  `@a2ui/web_core` version (0.10) against this clone before coding** — the repo
  pins `^0.10`, the protocol is labeled v0.9; reconcile the `/v0_9` import
  subpaths.
- **Probe the installed packages** (don't trust memory):
  `cd apps/web && cat node_modules/@a2ui/react/package.json` for the export
  map; `@a2ui/web_core/v0_9` is the subpath CopilotKit imports `Catalog` from.
- **CopilotKit A2UI renderer (the W2 path to reconcile):**
  `@copilotkit/a2ui-renderer@1.58` exports `createCatalog`,
  `createA2UIMessageRenderer`, `A2UIProvider`, `A2uiSurface`, `A2UIRenderer`,
  `extractSchema`. Probe via `npm pack` + read `dist/*.d.mts` (that's how W2's
  API was reverse-engineered — the published docs were ahead of 1.58).
- **MCP — `copilotkit-mcp`** (`mcp__copilotkit-mcp__search-docs`,
  `explore-docs`, `search-ag-ui-docs`): CopilotKit + AG-UI docs. Useful if
  converging the chat + panel renderers. Caveat: docs were *ahead* of the
  published 1.58 package in W2 — always cross-check against the actual
  `node_modules` / `npm pack` d.ts.
- **MCP — `context7`** (`resolve-library-id` → `query-docs`): general
  up-to-date library docs (used this session for LiteLLM config). Use for
  `@a2ui/*` if its docs are indexed.
- **In-repo prior art:** `apps/web/src/a2ui/copilot-catalog.tsx` (how the 17
  Aleph cards are adapted to a catalog of renderers — reuse the adapter shape),
  `apps/web/src/a2ui/components/*` (the cards themselves), `catalog.ts` (the
  current bare component-name contract to replace with Zod).
- **CLAUDE.md rule #8:** A2UI surfaces are declarative; agents request
  components by name + props, renderer validates against JSON Schema (Zod in
  practice). No agent-emitted JS/SQL. The v0.9 upgrade must preserve this.

## Out of scope
Full convergence of the chat (CopilotKit) and right-panel (upstream) renderers
onto a single stack — evaluate after W4.1 lands; may stay split.
