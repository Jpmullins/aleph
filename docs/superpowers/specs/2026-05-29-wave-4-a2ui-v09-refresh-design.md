# Wave 4 (refreshed) — A2UI v0_9 protocol + shared catalog + delta updates

Status: **approved design**. **Supersedes** `2026-05-29-wave-4-a2ui-v09-design.md`,
which was written against the a2ui.org v0.9 protocol docs and names APIs
(`createSurface`/`updateComponents`/`<A2uiSurface>`) that do NOT match the
installed `@a2ui` 0.10 packages. This refresh is grounded in probing the actual
installed API (2026-05-29).

## Why the refresh

The installed packages are `@a2ui/react@0.10.0` + `@a2ui/web_core@0.10.0`. Probed
reality:
- `@a2ui/react`'s main entry re-exports the **v0_8** API; the **v0_9** API is at
  the `@a2ui/react/v0_9` subpath (README-recommended) — `createComponentImplementation`,
  `createBinderlessComponentImplementation`, `basicCatalog`.
- `@a2ui/web_core/v0_9` provides `Catalog`, `createFunctionImplementation`,
  `CommonSchemas` (`DynamicString`/`DynamicNumber`/`DynamicBoolean`/`Action`/
  `Checkable`), `MessageProcessor`, the Generic Binder, and the message/state
  models (`ServerToClientMessage`, `A2UIClientEventMessage`, surface/data/component
  models).
- `@a2ui/react` (v0_8 + v0_9) exports `A2UIProvider`, `A2UIRenderer`, `A2UIViewer`,
  `MessageProcessor`, `Catalog`, `OnActionCallback`, `useA2UIActions`.
- **CopilotKit's chat renderer already builds its `Catalog` from `web_core/v0_9`**
  (`apps/web/src/a2ui/copilot-catalog.tsx` `createCatalog`). So the chat is
  already on v0_9 — a shared catalog across chat + panel is feasible.

Current state being replaced: the right panel uses a homegrown 97-line
`apps/web/src/a2ui/register.tsx` (REGISTRY + `renderA2UI` walk-and-dispatch) over
bare `{type,id,props,children}` JSON; the surfaces route returns a whole
`{tab, surface}` blob (`GET /v1/projects/{id}/surfaces/{tab}`). The 17 cards are
defined **twice** (chat: `copilot-catalog.tsx`; panel: `register.tsx` + `a2ui/components/*`).

## Decisions (locked in brainstorming)

1. **Target v0_9** (`@a2ui/react/v0_9` + `@a2ui/web_core/v0_9`) — the binder-bearing,
   README-recommended API that the chat already uses.
2. **One shared Aleph v0_9 `Catalog`** defines the 17 cards once; both the right
   panel and the chat consume it. Eliminates the dual definition.
3. **Full v0_9 messages + incremental deltas** — the backend emits a full surface
   on connect and diff-based `updateDataModel`/`updateComponents` deltas on change.

## Load-bearing rules preserved

- **Rule #8:** surfaces stay declarative; renderer validates against the catalog
  schema (Zod via `CommonSchemas`); no agent-emitted JS/SQL.
- **ActionRouter contract:** all card actions continue to POST
  `/v1/projects/{id}/cards/actions` with the `{action_kind, target_id, target_kind,
  params}` body. This is the load-bearing integration both renderers funnel through
  (the Wave 6 chat-routing fix already made the chat post here).
- **Rule #4/#5** unaffected (no new server mutations beyond what ActionRouter already
  ledgers).

## What ships

### W4.1 — Shared Aleph v0_9 catalog
`apps/web/src/a2ui/aleph-catalog-v09.tsx` (new): each of the 17 components
(`ClaimCard`, `SourceCard`, `ChartCard`, `TableCard`, `MapCard`, `GraphCard`,
`ApprovalCard`, `FindingCard`, `HypothesisCard`, `NotebookCellCard`, `FormCard`,
`DiffCard`, `ArtifactCard`, + the 5 surfaces if they render as components)
re-expressed via `createComponentImplementation(api, impl)` from
`@a2ui/react/v0_9`. Props typed with `CommonSchemas` so dynamic values
(`DynamicString` etc.) and `Action`s resolve through the Generic Binder (two-way
setters, reactive validation via `checks`). Composed into one `Catalog`
(`@a2ui/web_core/v0_9`). The existing card visuals/chrome (`a2ui/components/*`
`CardShell`/`Pill`) are reused inside the new implementations — this is a
re-wrapping, not a visual redesign.

### W4.2 — Right-panel renderer on MessageProcessor
`apps/web/src/components/A2UIRightPanel.tsx`: instantiate a `MessageProcessor`
(seeded with the shared catalog), feed it the surface messages from the stream
(W4.4), and render via `A2UIProvider` + `A2UIRenderer`. Retire `register.tsx`'s
walk-and-dispatch; preserve `SurfaceProvider`/`useSurface` (projectId/surface
context the cards rely on) by re-homing it or supplying the equivalent via the
A2UI action callback context. The `OnActionCallback` POSTs to `/cards/actions`
(unchanged body) and invalidates the relevant queries on success (mirror the
current behavior).

### W4.3 — Chat consumes the shared catalog
`apps/web/src/lib/copilot.tsx` + `copilot-catalog.tsx`: `createA2UIMessageRenderer`
is built from the **same** shared catalog (reconcile CopilotKit's `createCatalog`
wrapper — which already targets `web_core/v0_9` — with the upstream `Catalog`).
The chat's `onAction` already POSTs to `/cards/actions` (Wave 6 fix); keep that.
Net: the 17 cards are defined once.

### W4.4 — Backend emits v0_9 messages + delta SurfaceStreamer
`packages/aleph-a2ui/src/aleph_a2ui/` surface builders produce v0_9
`ServerToClientMessage`s (a `createSurface`-equivalent with the component tree +
a data model) instead of the bare `{type,id,props}` blob. New `SurfaceStreamer`:
- SSE endpoint `GET /v1/projects/{id}/surfaces/{tab}/stream` (in
  `apps/api/src/aleph_api/routes/surfaces.py`) — emits the **full** surface
  message on connect, then **diff-based** `updateDataModel`/`updateComponents`
  deltas: recompute the surface (same builder) on a change signal, diff against
  the last-sent snapshot per connection, emit minimal patches (e.g. new claim →
  patch `/claims/-`; confidence change → patch `/claims/<i>/confidence`).
- Change signal: reuse the existing event/SSE infra (the `agent-events` channel /
  ledger write hook) to trigger recompute; fall back to a bounded recompute
  interval if no targeted signal exists for a tab. Document whatever is wired.
- `GET /surfaces/{tab}` (whole blob) stays as the initial/non-streaming fallback,
  now returning the v0_9 message shape.

### W4.5 — Verification
- Existing `apps/web/playwright/specs/05-charts-tables-graphs.spec.ts` (4 viz
  tests) still pass against the new renderer.
- New spec: backend emits a partial `updateDataModel`; assert ONLY the targeted
  node re-renders (render-count probe / DOM-identity check).
- New spec/test: the shared catalog renders the same card (e.g. `HypothesisCard`)
  correctly in BOTH the chat and the right panel.
- Wave 6 Playwright flows (Live agent tools, ApprovalCard approve) still pass —
  the ActionRouter contract is unchanged.
- `pnpm -C apps/web typecheck && build` clean; rebuild `aleph-web` (baked image).

## File structure
- **New:** `apps/web/src/a2ui/aleph-catalog-v09.tsx` (shared catalog),
  `apps/api/.../surfaces.py` streamer additions, a backend
  `surface_streamer.py`/diff helper in `aleph_a2ui`.
- **Modified:** `A2UIRightPanel.tsx`, `lib/copilot.tsx`, `copilot-catalog.tsx`
  (reduce to catalog re-export/adapter), `aleph_a2ui` surface builders + the
  catalog schema if message shape changes.
- **Retired:** `register.tsx` walk-and-dispatch (REGISTRY + `renderA2UI`);
  `useSurface`/`SurfaceProvider` re-homed.
- The `a2ui/components/*` card visuals are **reused** inside the v0_9
  implementations (not deleted) where practical.

## Known risks (carry into the plan)
- **zod v3↔v4 boundary:** `@a2ui/*` peers `zod@^3.23.8`; app uses zod 4.4.3.
  `CommonSchemas` are v3. Isolate catalog definitions to the a2ui-pinned zod and
  cast at the boundary (the W2 pattern in `copilot-catalog.tsx`). This is the
  fiddliest integration point — probe the actual installed zod resolution
  (`apps/web/node_modules/@a2ui/web_core` peer + the app's zod) before coding.
- **Delta diff correctness:** the `SurfaceStreamer` must emit minimal, correct
  patches; the render-count test guards it. A wrong diff that re-emits the whole
  tree silently defeats the wave — assert it.
- **CopilotKit `createCatalog` reconciliation:** confirm one `Catalog` instance
  (or one definition set) can drive both `createA2UIMessageRenderer` and the
  upstream `A2UIRenderer` — probe `@copilotkit/a2ui-renderer`'s `createCatalog`
  signature (`npm pack` / `node_modules` d.ts) vs `web_core/v0_9` `Catalog`.
- **Surface-as-component vs surface-as-message:** the 5 right-panel surfaces
  (Wiki/Artifacts/Notes/Hypotheses/Briefs) currently come from `*_surface()`
  builders. Decide whether each surface is a v0_9 surface message whose root is a
  layout of Aleph cards (preferred) — confirm against the builders.

## References — probe before coding (do not trust this spec's API names blindly)
- **Installed packages (source of truth):** `apps/web/node_modules/@a2ui/react`
  (`/v0_9` subpath: `createComponentImplementation`, `basicCatalog`),
  `@a2ui/web_core/v0_9` (`Catalog`, `CommonSchemas`, `createFunctionImplementation`,
  `MessageProcessor`, generic-binder, message/state models). Read
  `node_modules/@a2ui/react/README.md` (the catalog + binder usage example this
  spec is based on) and the `src/v0_9/*.d.ts` files.
- **Reference clone:** `~/code/A2UI` (CLAUDE.md notable references) — core +
  React renderer + Python SDK; the tracked-upstream reference. Reconcile its
  version against installed 0.10.
- **CopilotKit renderer (chat path to share with):**
  `@copilotkit/a2ui-renderer` — `createCatalog`, `createA2UIMessageRenderer`
  (probe d.ts via `npm pack`; W2 reverse-engineered 1.58 this way).
- **In-repo prior art:** `apps/web/src/a2ui/copilot-catalog.tsx` (current chat
  catalog + the zod-boundary casts), `a2ui/components/*` (card visuals to reuse),
  `register.tsx` (the contract being replaced), `routes/surfaces.py` +
  `aleph_a2ui/components/surfaces.py` (backend builders), `A2UIRightPanel.tsx`
  (action POST + query-invalidation behavior to preserve).
- **CLAUDE.md rule #8** (declarative surfaces) and the **Wave 6 chat-action fix**
  (`copilot-catalog.tsx` adapter posts to `/cards/actions`) — the action contract
  to preserve across both renderers.

## Out of scope
- Visual redesign of any card (re-wrapping only).
- New card types or new surfaces.
- Converging the runtime-side inline catalog in `apps/copilot-runtime/src/server.ts`
  (the Node runtime's JSON-schema catalog used for `render_a2ui` tool injection) —
  it stays as the agent-facing schema; this wave unifies the *frontend* renderers.
  Re-evaluate after W4.
