I have a complete picture of both codebases. Writing the report.

---

# Aleph UI Architecture + benchmesh_v2 Comparative Read

**Verification convention:** every claim tagged `[V]` was read directly in the cited file; `[I]` is inference from what I read. All paths absolute-relative to `/home/claude/aleph` (Aleph) or `/mnt/user-data/uploads/code/benchmesh_v2` (benchmesh).

**Staging caveat:** benchmesh is partial. `packages/frontend/app/workspace/cards/` contains only 7 of the ~14 card modules `canvas.tsx:8-19` imports (`experiment-registry`, `misc`, `models`, `register-app`, `register-dataset`, `registry-list` are absent), and there is no `package.json`, so the "Next 16 / React 19 / CopilotKit 1.59" stack claim is **unverified from code** — I can only confirm `CLAUDE.md:74` asserts Next 16/React 19 and `app/providers.tsx:3` imports `@copilotkit/react-core/v2`.

---

# PART A — Aleph's current UI architecture

## A1. Shell, routing, state, styling

### Layout — three resizable panels, one modal drawer layer

`apps/web/src/components/ProjectWorkspace.tsx:56-80` is the whole shell: a `react-resizable-panels` `PanelGroup` with `autoSaveId="aleph-workspace-layout"` and three panels — **left 18%** (`LeftPanel`, min 14 max 30), **center 52%** (`ActivityCard` stacked above `CopilotChatSurface`, min 30), **right 30%** (`A2UIRightPanel`, min 22 max 50). Divider handles at `:67` and `:76`. A `Drawer` overlays at `:82-84` for `settings|logs|notifications|profile` (`components/Drawers.tsx:50-53`). [V]

Provider nesting, outermost first: `WorkspaceUIProvider` → `LiveSignalsProvider` → PanelGroup (`ProjectWorkspace.tsx:52-53`), sitting inside `QueryClientProvider` → `AlephCopilotProvider` from `main.tsx:23-31`. [V]

### Routing — hand-rolled, ~20 lines, no router library

`App.tsx:12-19` `parseRoute()` matches four paths (`/auth/callback`, `/`, `/projects`, `/projects/:uuid`) off `window.location.pathname` with a regex; `App.tsx:21-24` `navigate()` does `history.pushState` then fires a **synthetic `PopStateEvent`** so the listener at `:30` re-parses. `@tanstack/react-router` is a declared dependency (`package.json` deps) but **is not imported anywhere in `src/`** — dead weight. [V]

Consequence: there is **no URL for workspace state**. Active tab, open page, and open drawer live only in React state (`workspace-ui.tsx:58-62`), so nothing in the right panel is linkable, bookmarkable, or back-button-able. That is a real limitation for a "web of belief" whose whole point is that a claim/page/edge is a durable addressable node.

### State management — four disjoint layers

| Layer | Where | Scope |
|---|---|---|
| Server cache | `@tanstack/react-query`, `main.tsx:12-16` (`retry: 1`, `staleTime: 30s`) | left panel, drawers, chat's `card-actions` poll |
| Workspace UI | `lib/workspace-ui.tsx:31-53` React context | `activeSurface`, `openPageId`, `openPageTitle`, `selection`, `highlightedClaimId` |
| A2UI surface state | `MessageProcessor` instance inside `A2UISurfaceView.tsx:180` | right panel data model, per connection |
| Agent thread | CopilotKit `<CopilotChat threadId>`, `CopilotChatSurface.tsx:62,181-185` | chat transcript |

There is no Redux/Zustand/store. `WorkspaceUIProvider` re-`useMemo`s a 10-key object on every field change (`workspace-ui.tsx:64-78`), so any selection change re-renders every consumer — including the whole chat surface. Minor today, will bite when selection becomes hover-granular. [V/I]

### Styling — Tailwind 4 with a bolt-on token layer, and a dark-mode shim that is technical debt

- `apps/web/tailwind.config.ts` extends **only `fontFamily`** (Inter / JetBrains Mono). No color, spacing, or radius tokens in the Tailwind theme. [V]
- `src/styles/tokens.css:8-48` (light) and `:50-84` (dark, `[data-theme="dark"]`) define ~30 CSS custom properties: surfaces, borders, text, an **orange accent `#f97316`**, five badge pairs, three shadows. `:86-117` duplicates the dark block under `prefers-color-scheme`. [V]
- **The problem:** `tokens.css:119-182` is an explicit "dark-mode override shim" that remaps **hardcoded Tailwind palette classes** with `!important` — `[data-theme="dark"] .bg-white { background-color:#1a1a1c !important }`, `.text-slate-900`, `.border-slate-200`, etc. Its own comment at `:130` says "This is a pragmatic shim; a full token migration can replace it later." [V]

So Aleph has **two competing color systems**: components mostly write `bg-white text-slate-900 border-slate-200` (e.g. `A2UIRightPanel.tsx:37-53`, `WikiPageCard.tsx:175,220,234`), which the shim repaints in dark; while `_shared.tsx:38-43,68,98-104` writes the *correct* `bg-[var(--badge-warning-bg,#fef3c7)]` token form. The card primitives are token-clean; everything around them is not.

- CopilotKit is themed by variable-aliasing in `styles.css:83-107` (mapping `--cpk-color-gray-*` onto Aleph tokens), plus a genuinely ugly escape hatch at `:118-125` that matches **inline styles by literal RGB string** (`[style*="rgb(250, 250, 250)"]`) to repaint tool-call cards. [V] That's a fragile coupling to a vendored component's internals.
- Serif prose face (`Source Serif 4`) for chat + wiki (`styles.css:26-31, 110-113`). Nice touch, correct instinct for a reading product.

### Component inventory

**Chrome / non-A2UI (10):** `ProjectList.tsx` (243), `ProjectWorkspace.tsx` (105), `LeftPanel.tsx` (175), `ActivityCard.tsx` (349, SSE agent-run ticker — `:139-163` handles `phase_started|completed|failed`), `CopilotChatSurface.tsx` (189), `A2UIRightPanel.tsx` (65), `Drawers.tsx` (565, four bodies), `ThemeToggle.tsx` (114), `SourceUploadModal.tsx` (102), `AlephLogo.tsx` (43), plus `WikiBodyMarkdown.tsx` (154) and `WikilinkChip.tsx` (35) as reader primitives. [V]

**A2UI catalog views (20 + 2 helpers):** all in `src/a2ui/components/`; `_shared.tsx` (222) exports `isSandboxedAssetSrc`, `Pill`, `CardShell`, `SurfaceHeader`, `FeedbackButton`; `HypothesisMatrix.tsx` (122) is an ACH grid used *inside* `HypothesesSurface` but is **not a catalog component**. [V]

---

## A2. The A2UI layer — how the declarative protocol actually works

### The wire

`packages/aleph-a2ui/.../messages.py:28-58` builds three nested-envelope message kinds — `createSurface`, `updateComponents`, `updateDataModel` — matching upstream `@a2ui/web_core` v0_9. Props ride **inline on the component object**, not under a `props` key (`messages.py:36-46` docstring + `surfaces.py:91-96`). `full_surface()` at `messages.py:61-82` emits the ordered triple: create → components → one bulk `updateDataModel` at `path="/"`. [V]

### Server-side surface construction

`components/surfaces.py` has five builders. The four canonical ones are **data-bound**: e.g. `wiki_surface_v09` (`:76-102`) emits a single component `{"id":"root","component":"WikiSurface","pages":{"path":"/pages"},"open":{"path":"/open"}}` plus a data model `{pages, open}`. Same shape for `artifacts_surface_v09` (`:105-129`, `/sources` + `/artifacts`), `notes_surface_v09` (`:132-151`), `hypotheses_surface_v09` (`:154-175`, `/items` + `/ach`). `briefs_surface_v09` (`:178-189`) is the odd one out — it uses `_surface_messages` (`:27-57`) with an inline `children` array of legacy `{type,id,props}` card dicts and **no data model at all**. [V]

The `id: "root"` is load-bearing: `<A2uiSurface>` renders exactly the component named `root` (documented at `surfaces.py:42-47`). [V]

Row loading happens in the **route layer**, not the builders, because the route owns the session: `routes/surfaces.py:391-446` (wiki), `:289-309` (library), `:536-578` (notes), `:273-286` (hypotheses), `:581-699` (briefs). These reuse the REST handlers directly (`from aleph_api.routes.wiki import get_page, list_pages` at `:398`) so surface and REST can't diverge. [V] Batching is deliberate and documented: `_source_previews` one query (`:367-388`), `_resolve_citations` two queries (`:476-533`), `_annotate_drift` two queries (`:312-358`). [V]

### Data-model bindings on the client

`aleph-catalog-v09.tsx` defines each component as `{name, schema}` where the schema is built with **zod v3 via a package alias** (`:64` `import { z as z3 } from "zod3"`, `package.json` `"zod3": "npm:zod@3.25.76"`). The module docstring at `:42-62` documents why this is load-bearing: the v0_9 Generic Binder introspects zod *v3* internals (`_def.typeName === 'ZodObject'`), and the app resolves zod 4 — a v4 schema collapses to STATIC, bindings pass through unresolved as objects, and React throws "Objects are not valid as a React child." [V] **This is a landmine.** It is documented but not enforced by any check; a new card written with the wrong `z` import fails at runtime, not at typecheck.

The classification rule (`:56-62`): `CommonSchemas.Dynamic*`/`Action` = bindable scalar the binder resolves; complex literals (Vega spec, table rows, claims arrays) must be `z3.any()`/`z3.array(z3.any())` to pass through verbatim. The four canonical surfaces use `CommonSchemas.DynamicValue` (`:454-455, 466-467, 478, 489-490`) so a `{path:"/pages"}` resolves a whole array. [V]

`adapt()` (`:110-169`) is the shim between the binder's resolved-props world and Aleph's `{component:{type,id,props}, onAction}` view convention. It also does three other things: mints the `useMutation` that POSTs every action to `/v1/projects/{id}/cards/actions` (`:120-136`), invalidates react-query surface keys on success (`:140-142`), and **applies navigation results locally** (`:144-148` — if the router returns `{navigate:{tab,page_id}}`, it calls `setOpenPageId`/`setActiveSurface`). [V]

### Delta flow: LISTEN/NOTIFY → recompute → diff → SSE

1. DB triggers `pg_notify('aleph_changes', …)` (`apps/api/alembic/versions/20260530_1000_realtime_notify_triggers.py:42,66,89`), channel constant at `apps/api/src/aleph_api/realtime.py:32`. [V]
2. `NotifyListener` (`realtime.py:119+`) holds one asyncpg LISTEN connection per process, reconnects with capped backoff, and feeds `ChangeBroker` (`:73-116`) — an in-process per-project fan-out over bounded `asyncio.Queue(maxsize=256)`. A full queue **drops** the signal with a warning (`:93-99`) and relies on the poll fallback to reconcile. [V]
3. `stream_surface` (`routes/surfaces.py:151-270`) subscribes at `:234` and loops: `await sub.wait(timeout=10.0)` (`:238`, constant at `:56`), rebuild the whole tab (`:242-245`), `split_surface_messages` (`:246`), re-emit `updateComponents` **only if the structural list changed** (`:248-253`), then `diff_data_model(prev_model, model)` → `data_model_patches_to_messages` → one `updateDataModel` per patch (`:255-259`), then a `: heartbeat` SSE comment (`:264`). [V]
4. The diff is pure and unit-testable (`surface_streamer.py:67-112`): dicts key-by-key, lists index-by-index with tail add/remove (removes emitted high-index-first, `:111-113`), type change = wholesale `replace`. The wire has **no remove primitive**, so `data_model_patches_to_messages` (`:161-205`) maps object-key removes to `value=None` and — for any array remove — **re-sets the entire changed array** (`:176-190`). Documented rationale at `:27-47`. [V] Honest and correct; costs one full array re-send on the rare delete.

### Resume / ordering

`SurfaceStreamBuffer` (`surface_streamer.py:233-289`) is a pure state machine: monotonic `seq` stamper (`:257-263`), bounded `deque(maxlen=64)` ring, `can_replay()` (`:275-285`) with the off-by-one carefully handled (`last_event_id >= oldest - 1`), and — critically — it stores `model` and `structural` **as last emitted** (`:253-254`) so a resuming generator can forward-diff from exactly what the client had rather than resnapshot. [V]

The route keys buffers by `f"{project_id}:{tab_lc}:{raw_cid}"` (`routes/surfaces.py:192`) with an explicit security note at `:186-190`: a `cid` leaked through query-string logging can only ever replay the exact project+tab that created it. Good defensive thinking. TTL sweep at `:72-82` (300s TTL, 4096 soft cap). Resume path `:203-217`, cold-snapshot path `:218-226`. `_sse` emits `id: {seq}` (`:85-87`) so `EventSource` echoes `Last-Event-ID`. [V]

Client side: `A2UISurfaceView.tsx:171-177` mints one `cid` per mount (`crypto.randomUUID`) and `withConnectionId` (`:37-46`) appends it. The `onmessage` handler (`:194-208`) drops `seq <= lastSeq`. **`lastSeq` is a closure local reset on every effect re-run** (`:184`) — correct, since a re-run means a new connection and a fresh baseline. `onerror` (`:209-215`) intentionally does nothing: the auto-reconnect resends a full surface whose duplicate `createSurface` throws inside `processMessages` and is swallowed by the per-message `try/catch` at `:205`. [V]

> **Assessment of that last bit:** swallowing a thrown `A2uiStateError` as the *designed* self-heal path is clever but brittle — the same catch silently eats malformed frames and any future processor error. A `seq`-aware "if seq === 0 and we already have surfaces, tear down and rebuild" would be more honest than exception-driven idempotency.

The tab switch is a full remount by construction: `A2UIRightPanel.tsx:34` keys the stream view on `` `${tab}:${openPageId}` `` for wiki, so opening a page **drops the SSE connection and re-streams** rather than patching `/open` in place. That's a deliberate simplification with a real cost — every page open is a full surface rebuild, and the ring buffer for the old cid is orphaned until TTL.

### No-self-fetch enforcement

`scripts/check-no-self-fetch.sh:32` greps `apps/web/src/a2ui/components` for `@tanstack/react-query|useQuery[(<]|useMutation[(<]|refetchInterval|EventSource(|fetch(|api.(get|post|put|delete)`, with `ALLOWLIST=()` **empty** (`:33`). CI-wired. [V] The pattern is written to match code, not prose mentioning the APIs (`:29-32`), which is why `_shared.tsx`'s comments about react-query don't trip it. Verified clean: no component file imports react-query.

The one wrinkle — `ArtifactsSurface.tsx:159-164` renders an `<iframe src=…/assets/source/…>`. That's a URL, not a fetch, so it passes the grep and is legitimately within the rule. [V]

### The catalog schism (a real finding)

There are **two Python catalogs that do not describe the same thing**:

- `catalog.py:54-330` — JSON Schema over the **legacy `{type, id, props}`** envelope. `WikiSurface` there (`:56-63`) requires `view_mode ∈ {page,graph,list,recent}` and accepts `current_page_id`/`filters`. The v0.9 builder emits **none of those** — it emits `pages`/`open` bindings (`surfaces.py:91-96`).
- The v0.9 wire builders (`messages.py` + `surfaces.py`) — inline props, no `props` key.

`validate_component`/`validate_surface` are called in exactly **two** places: `a2ui_handlers.py:927` (compose_dossier payload) and `routes/cards.py:125` (pin-card payload). [V] **The entire right-panel surface stream is never validated against `catalog.py`.** So `catalog.py`'s component schemas are a live contract only for pinned/agent-emitted cards, and a partly-fictional one for surfaces. `check-catalog-roster.sh:77-83` compares **names only** between `catalog.py` and `catalog.ts` — it cannot catch shape drift. This is the single largest correctness gap in the A2UI layer.

---

## A3. Full catalog roster

20 components. Props schema cited from `catalog.py` (JSON Schema, agent/pin path) and `aleph-catalog-v09.tsx` (zod3, render path). Producers from the ledgered map in `scripts/check-catalog-roster.sh:34-55`, verified against the files.

### Surfaces (5)

| Component | Wire props (v0.9 render path) | Producer | Renderer |
|---|---|---|---|
| **WikiSurface** | `pages`, `open` — both `DynamicValue` · `aleph-catalog-v09.tsx:451-457`; catalog.py:56-63 declares `current_page_id`/`view_mode`/`filters` (**divergent**) | `surfaces.py:76-102` `wiki_surface_v09`; data from `routes/surfaces.py:391-446` | `components/WikiSurface.tsx:59-160` |
| **ArtifactsSurface** ("Library" tab) | `sources`, `artifacts` · `:463-469`; catalog.py:64-66 `current_artifact_id` | `surfaces.py:105-129`; data `routes/surfaces.py:289-309` | `ArtifactsSurface.tsx:43-89` |
| **NotesSurface** | `notes` · `:475-480`; catalog.py:67-72 | `surfaces.py:132-151`; data `routes/surfaces.py:536-578` | `NotesSurface.tsx:20-89` |
| **HypothesesSurface** | `items`, `ach` · `:486-492`; catalog.py:73-75 | `surfaces.py:154-175`; data `routes/surfaces.py:273-286` | `HypothesesSurface.tsx:33-108` (+ `HypothesisMatrix.tsx:44`) |
| **BriefsSurface** | `badge_count` (DynamicNumber), `filters` (any), `children` (any[]) · `:498-505`; catalog.py:76-81 | `surfaces.py:178-189`; children assembled `routes/surfaces.py:581-699` | `BriefsSurface.tsx:5-49` |

### Cards (15)

| Component | Props (zod3 schema, `aleph-catalog-v09.tsx`) | JSON Schema | Producer | Renderer |
|---|---|---|---|---|
| **ClaimCard** | `claim_id`, `text`, `confidence` (Dyn str); `citations` (any[]); `open_action` · `:190-201` | `catalog.py:83-111` — confidence enum incl. `retracted`; req `claim_id,text,confidence` | `cards.py:111-122` `claim_card` | `ClaimCard.tsx:4-54` |
| **SourceCard** | `source_id,short_id,title,url,status,normalized_preview`, `retracted` (bool), `open_action`, `navigate_wiki_action` · `:207-220` | `:112-128` | `cards.py:125-140` | `SourceCard.tsx:14-99` |
| **ArtifactCard** | `artifact_id,short_id,title,artifact_kind,status`, `drifted` (bool), `open_action` · `:227-238` | `:129-142` | **`apps/copilot-runtime/src/server.ts:133-148`** (agent-emit declaration) | `ArtifactCard.tsx:12-65` |
| **ChartCard** | `title,chart_id,chart_url,artifact_version_id` + `vega_lite_spec` (any) · `:245-256` | `:143-154` | `apps/api/src/aleph_api/subagents/viz_builder.py` (`"ChartCard"`) + code_runner | `ChartCard.tsx:33-111`; `:11-32` installs a **network-blocked vega loader** |
| **ImageCard** | `src` (req), `title,alt,artifact_version_id` · `:264-271` | `:156-165` | `apps/workers/.../jobs/render_code.py` | `ImageCard.tsx:13-37` |
| **HtmlFrameCard** | `src` (req), `title,artifact_version_id` · `:278-284` | `:166-176` | `render_code.py` | `HtmlFrameCard.tsx:18-45`; gated by `isSandboxedAssetSrc` |
| **TableCard** | `dataset_version_id,title`, `columns`/`rows` (any[]), `_placeholder`, `open_action` · `:291-301` | `:177-185` | `cards.py:159-170` | `TableCard.tsx:11-118` |
| **ApprovalCard** | `target_id,target_kind,title,summary,severity`, `evidence_refs` (any[]), `approve_action`, `reject_action` · `:308-320` | `:186-226` — target_kind enum, req incl. both actions | `cards.py:173-189`; called `routes/surfaces.py:606,643` + `jobs/wiki_refresh.py:327` | `ApprovalCard.tsx:5-104` |
| **FindingCard** | `finding_id,severity,kind,summary`, `evidence_refs`, 3 actions · `:327-338` | `:227-239` | `cards.py:192-206`; called `routes/surfaces.py:674` | `FindingCard.tsx:4-64` |
| **HypothesisCard** | `hypothesis_id,title,confidence,evidence_count`, `open_action` · `:175-183` | `:240-249` | `cards.py:209-220` | `HypothesisCard.tsx:3-32` |
| **FormCard** | `form_id,title`, `fields` (any[]), `submit_action` · `:345-353` | `:250-271` | `server.ts:170+` (agent-emit) | `FormCard.tsx:13-89` |
| **DiffCard** | `from/to_revision_id`, `page_id`, `from/to_body_md`, `open_action` · `:360-369` | `:272-283` | `cards.py:236-248` | `DiffCard.tsx:53-93` (real line diff at `:14-45`) |
| **WikiPageCard** | `page_id,body_md,html_url` (Dyn str); `claims`,`citations`,`wikilinks_out` (any[]); `page_meta` (any); `retracted`,`derived`,`read_only` (bool); 4 actions · `:382-400` | `:285-306` — req `body_md` | `apps/api/src/aleph_api/a2ui_handlers.py:910` (compose_dossier) + `WikiSurface.tsx:95-116` | `WikiPageCard.tsx:114-355` |
| **NoteEditorCard** | `note_id,section_id,title,body_md`, `edit/rename/promote_action` · `:407-417` | `:307-318` | `NotesSurface.tsx` (embeds it) | `NoteEditorCard.tsx:25-115` |
| **HtmlDocCard** | `src` (req), `title`, `derived`, `read_only` · `:424-431` | `:319-329` | `WikiPageCard.tsx:252-260` (document view) | `HtmlDocCard.tsx:17-53` |

**Roster notes:**
- The comment "13 cards + 5 surfaces" at `A2UISurfaceView.tsx:50` and `lib/copilot.tsx:11` is **stale** — `ALEPH_CARD_IMPLS` (`:516-540`) has **15 cards**. [V]
- Three "producers" are frontend files (`NotesSurface.tsx`, `WikiPageCard.tsx`) or a TS catalog *declaration* (`server.ts`), not backend emitters. The sweep's own header (`check-catalog-roster.sh:8-10`) admits this. So "every component has a producer" is weaker than it reads.
- **21 actions** in `catalog.py:337-530` / `catalog.ts:33-55`; all 21 are registered as handlers at `a2ui_handlers.py:1245-1265`. [V] Params are JSON-Schema-validated per action before dispatch (`action_router.py:78-88`).

### Action routing

`ActionRouter.dispatch` (`action_router.py:68-154`): validate params against `CATALOG["actions"]` → resolve handler → drop kwargs the handler doesn't accept via `inspect.signature` (`:111-117`) → run inside an OTEL span → **append a `ledger.append` event `a2ui.action.{kind}`** (`:120-134`) → insert a `CardAction` row pointing at that ledger event (`:136-152`). One transaction, hash-chained. This is the strongest part of the architecture: *every* interaction — analyst click or agent tool call — is one audited path.

---

## A4. CopilotKit integration

### Provider + shared catalog

`lib/copilot.tsx:35-38` builds `createA2UIMessageRenderer({theme: a2uiDefaultTheme, catalog: buildAlephCatalog()})` **once at module scope** and passes it as `renderActivityMessages` to `<CopilotKitProvider runtimeUrl=…>` (`:40-48`). So chat-emitted cards and panel-streamed cards resolve through the **same** `ReactComponentImplementation` set. [V] Correct and elegant — one card definition, two consumption sites.

### The bridge

`apps/copilot-runtime/src/server.ts` declares `ALEPH_A2UI_CATALOG = {catalogId: "aleph://v1", components: {…}}` (`:33-201`) with per-component **natural-language descriptions** aimed at the model (e.g. ChartCard at `:37-41`: "Provide a self-contained Vega-Lite spec … with data embedded under `data.values`"). At `:299-301` it sets `injectA2UITool: true`, `schema: ALEPH_A2UI_CATALOG`, `defaultCatalogId: "aleph://v1"`. The comment at `:292-297` explains why `defaultCatalogId` is load-bearing: without it the middleware stamps the upstream basic-catalog id, which the frontend never registers, and the renderer errors "Catalog not found." [V]

Note the catalog exists in **three** hand-maintained forms — `catalog.py` (JSON Schema), `catalog.ts`+`aleph-catalog-v09.tsx` (zod3), `server.ts` (agent-facing descriptions). Only names are cross-checked.

### Eyes — shared state

`CopilotChatSurface.tsx:67-81` registers `useAgentContext` with `{active_tab, open_page_id, open_page_title, selection}` and a description that explicitly instructs the model to use `open_page_id`/`selection` for "this page"/"this claim" without asking. A **second** `useAgentContext` at `:104-117` feeds the agent the analyst's last 10 card actions (from a `refetchInterval: 15_000` query at `:99-103`) so it knows the outcome of cards it rendered. [V] That second one is a genuinely good idea — most agent-UI systems are fire-and-forget on their own cards.

Selection is published by the reader: `WikiPageCard.tsx:138-144` sets `{claim_id, text, page_id}` on claim click or `mouseup` text selection.

### Hands — frontend tools

Three `useFrontendTool` registrations, each **dispatching through the ledger-audited router before applying the UI change** (`:86-94` `dispatchAction`):
- `focus_tab` (`:121-134`) — z-enum over `SURFACE_TABS`
- `open_page` (`:138-161`) — `page_id` **or** `slug`; the router resolves the slug and returns `{navigate:{page_id}}`
- `highlight_claim` (`:164-176`) — sets `highlightedClaimId`; `WikiPageCard.tsx:288` rings the claim with `ring-2 ring-amber-400`

Plus three **agent composition verbs** that go through the router but have no frontend tool: `pin_to_brief`, `compose_dossier`, `spotlight` (`catalog.py:512-529`, handlers at `a2ui_handlers.py:1264-1265` and `:846-960`). `compose_dossier` builds a markdown body with `[[wikilinks]]`, validates it as a `WikiPageCard` payload marked `derived:true, read_only:true`, and pins it (`:906-955`). [V] This is the closest thing Aleph has to agent-authored composite views — and it's the right primitive to build on for Part C.

Project scope rides on the thread id: `proj:${projectId}:${threadId}` (`:62`) — the only channel `ag-ui-langgraph` threads into the graph config (`:15-16`).

---

## A5. Honest assessment

### What is genuinely good

1. **The delta substrate is real engineering.** Pure diff + pure ring buffer, both unit-testable with zero I/O (`surface_streamer.py`); forward-diff-on-resume rather than resnapshot (`routes/surfaces.py:203-217`); seq ordering enforced on both ends. Most "realtime UI" codebases have none of this. The push path (`pg_notify` → `NotifyListener` → `ChangeBroker` → recompute-and-diff) means **no polling in the right panel**, with a 10s self-heal.
2. **One audited action path.** Analyst click and agent tool call converge on `ActionRouter.dispatch` → ledger event + `CardAction` row in one transaction. For an adjudication product this is exactly right, and it means "who decided this and when" is answerable by construction.
3. **The no-self-fetch invariant, actually enforced, with an empty allowlist.** Rare. It buys you a single ordered data path per surface, which is what makes the delta stream trustworthy.
4. **Security posture at the renderer.** `isSandboxedAssetSrc` (`_shared.tsx:23-28`) is a tight regex allowing exactly three route shapes, and `HtmlDocCard`/`HtmlFrameCard` refuse to mount otherwise. `ChartCard.tsx:11-32` installs a rejecting loader so vega can't fetch. Rule 8 is enforced *at the component*, not just in docs.
5. **One card definition, two surfaces** (panel + chat) via `buildAlephCatalog`.

### What is awkward

1. **The catalog schism** (§A2). `catalog.py`'s surface schemas describe a shape nothing emits, and the streaming path is unvalidated. The roster sweep gives false confidence because it only diffs names.
2. **The zod3 alias landmine.** A correctness-critical convention (`aleph-catalog-v09.tsx:42-62`) enforced by a comment. No lint rule, no test.
3. **Dark mode via `!important` class overrides** (`tokens.css:132-182`) plus RGB-string matching for CopilotKit (`styles.css:118-125`). Every new component written in `bg-white text-slate-900` silently depends on the shim.
4. **No URL state.** Nothing in the right panel is addressable. For a belief graph this is close to disqualifying — you cannot send a colleague a link to a contradiction.
5. **Tab switch = full remount.** `A2UIRightPanel.tsx:34`'s key includes `openPageId`, so every page open tears down the connection, re-runs `_build_tab_messages`, and re-sends the whole surface. The delta machinery is bypassed for the single most common navigation.
6. **Briefs is architecturally the odd one.** No data model, inline `children` array of legacy-shaped dicts (`surfaces.py:178-189`), dispatched by a hand-rolled `renderChildCard` map (`surface-context.tsx:81-117`) that sits *outside* the binder. Two rendering paths for the same card set.
7. **`_build_tab_messages` rebuilds the entire tab on every wake** (`routes/surfaces.py:242-245`) — full page list + all previews + drift computation — then throws almost all of it away in the diff. Fine at 50 pages; not at 50,000 claims.
8. **Right panel is 30% of a horizontal split.** A wiki reader with claims, citations, and link-outs in ~400px is cramped today. A grounding tree or an impact graph will not fit at all.

### Where new surfaces are easy vs. hard

**Easy** — a new *card* inside an existing tab. Add a zod3 schema + impl in `aleph-catalog-v09.tsx`, a view file, a name in both catalogs, a producer, and it streams with deltas for free. ~150 lines, mechanical.

**Medium** — a new *tab*. Add to `SURFACE_TABS` (`workspace-ui.tsx:17`), a `*_v09` builder, a branch in `_build_tab_messages` (`routes/surfaces.py:123-136`), a surface view, catalog entries in three places. The 5-tab nav (`A2UIRightPanel.tsx:38-54`) is `flex-1` per tab — a 6th or 7th tab degrades to unreadable ~60px labels. **The tab bar is the scaling wall.**

**Hard** — anything that is (a) not a vertical list in a 400px column, (b) needs two things side by side, (c) needs its own detail/master navigation, or (d) needs graph layout. Aleph has *no* canvas, *no* multi-pane surface, *no* modal-detail pattern inside a surface, and *no* graph renderer (`@xyflow/react` is a dependency but **unimported in `src/`**; `GraphCard` was explicitly deleted per `check-catalog-roster.sh:29`). Every Part-C surface below lands in this category.

---

# PART B — benchmesh_v2 comparative read

## B6/B7. Characterization

### Layout model — rail · assistant · (context-bar / canvas)

`app/page.tsx:15-32`: `WorkspaceProvider > SessionProvider > RunProvider > .ws-root [ Rail | AssistantPanel | .ws-main[ ContextBar, Canvas ] ]`. [V]

- **Rail** (`rail.tsx:9-21`): 11 fixed icon entries + a `B` home mark + a bottom-anchored Settings. Clicking dispatches `{type:"open", kind}`. `data-active` = "is this card open" (`:25,46`), not "is this the current page" — a **fundamentally different mental model from tabs**: the rail is a *launcher*, not a selector. 56px wide, active state = accent-soft background + a 3px left bar via `::before` (`globals.css:274-306`), hover tooltip at `:308-329`.
- **Assistant panel** (`assistant-panel.tsx:680-893`): a 340px flex-basis dock, collapsible to 34px with a `writing-mode: vertical-rl` reopen tab (`globals.css:337-371`). It is **always present**, left of the canvas.
- **Context bar** (`context-bar.tsx:18-58`): a 13px strip reading `Looking at [App ▸] [kind chip] … [session]`. Read-only — clicking opens the Systems card, "so switching targets always goes through one card-based pathway" (`:4-6`). One shared answer to "what are we looking at" for both human and agent.
- **Canvas** (`canvas.tsx:86-101`): a 2400×1800 scrollable plane (`globals.css:702-708`) with a 22px radial-dot grid (`:698-699`), holding absolutely-positioned cards.

### The card shell pattern — the strongest single idea in this codebase

`card-shell.tsx:20-30` — `CardShell({card, title?, savable?, children})`. Every surface renders inside it. It provides:
- **Pointer-based drag** on the header (`:38-63`), with `setPointerCapture` and a `closest("button")` guard so header buttons don't drag.
- **Resize** from a corner grip (`:65-98`), min 280×160 (`store.tsx:222`).
- **Pin** (`:157-167`) → `z + 1000` (`:121`) and a 3px accent top border (`globals.css:735-737`).
- **Minimize** (double-click header too, `:132`) — collapses to just the header.
- **Save to artifacts** (`:100-106, 137-156`) — an *optional* `savable: {name, kind, getPayload}` contract. Any card can opt into durable persistence with three fields.
- **Eyebrow + title** from central maps (`store.tsx:68-88` titles, `:90-110` eyebrows: `registry|config|visualization|execution|assistant|workspace`).

Content-driven height with a `max-height: 68vh` cap **until the user resizes**, after which the body fills and scrolls (`globals.css:812-816`). Small detail, very good ergonomics.

### Design system

Not a framework — hand-written CSS with **semantic** tokens. `globals.css:6-29` light, `:36-57` dark under `:root[data-theme="dark"]`, `:59-82` the same values under `prefers-color-scheme` with `:not([data-theme="light"]):not([data-theme="dark"])`. [V]

- **Palette:** ~19 tokens. `--bg #f2f3f5`, `--surface`, `--surface-2`, `--ink #1c2126`, `--muted`, `--faint`, `--line`, `--line-strong`, `--accent #35597f` (desaturated slate-blue), `--accent-soft`, `--on-accent`, and three status triples `--ok/--warn/--err` each with a `-soft` companion, plus `--shadow`/`--shadow-raised`, `--mono`, `--sans`. **Dark is a hand-tuned reciprocal, not an inversion** — accent flips from `#35597f` to `#8ab0d9` (lightens for dark bg), `--on-accent` flips white↔`#10141a`. That's the correct way to do it.
- **Typography:** one 14px/1.5 base (`:94-101`), with a deliberate micro-scale — 10px eyebrows at `letter-spacing: .14em` uppercase (`:759-766`, `:475-482`), 11px chips, 12px tooltips, 13px body, 13.5px card titles, 16px `.a2-h2`. Font-weight `550` appears at `:848` (variable-font-aware).
- **Spacing/radius:** informal but consistent — 6/7/8/9/10/12px radii scaling with element size (button 8, card 12, chip 999).
- **Motion:** almost none, by design. One `@keyframes blink` for the streaming cursor (`:458-468`). No transitions on hover — state changes are instant color/border swaps. **`prefers-reduced-motion` kills all animation globally at `:116-123`.**
- **Focus:** one global `:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px }` at `:111-114`. Aleph has no equivalent global focus rule.
- **Theme control:** `theme.ts:24-31` sets/clears `data-theme` on `<html>`; `setThemePref` (`:33-41`) persists and fires a `benchmesh:theme-changed` CustomEvent that `useTheme` (`:43-57`) subscribes to. `layout.tsx:14,20` injects a blocking inline script to apply the saved theme **before first paint** — no FOUC.

Zero `!important`. Zero utility-class overriding. Light and dark are the same rules with different variables.

### State/store

- `store.tsx:162-245` — a plain `useReducer` over `{cards, nextZ, hydrated}` with 9 actions. Cards are **singletons per kind** (`:167-182` — opening an open kind focuses it and merges props). `cascade()` (`:157-160`) staggers new cards. Per-kind default widths at `:112-132`. Persisted to `localStorage` on every change (`:278-288`), hydrated once client-side to avoid SSR mismatch (`:258-274`).
- `session-context.tsx` — apps list + active app + named chat sessions, each carrying `messages` and a `PipelineState` (`:41-64`). **localStorage is the source of truth; the server is a best-effort mirror** (`:91-94`): PUT on change with a `lastSynced` dedupe map (`:213-227`), GET+merge on boot where the server wins only if `updated_at` is newer (`:232-273`). Every call is try/caught so the console behaves identically with no backend.
- `run-context.tsx:36-54` — **deliberately not persisted**: "base64 frames and step telemetry don't belong in localStorage" (`:5-7`).
- **Cross-component coordination via `window` CustomEvents**: `benchmesh:workflow-approved`, `spec-approved`, `workflow-registered`, `load-experiment`, `artifact-saved`, `workspace-reset`, `assistant-reset`, `theme-changed`. The assistant panel listens (`assistant-panel.tsx:290-324, 328-388, 392-407`) and advances the pipeline. This decouples cards from each other completely — and is also **untyped, undiscoverable, and unloggable**. Clever; not a pattern I'd port wholesale.

### Its A2UI renderer vs. Aleph's

`app/a2ui-renderer.tsx` is **158 lines total**. It supports 8 upstream basic-catalog primitives (`Card/Column/Row/List/Text/Divider/Image/Tabs`, `:57-106`), resolves `{path}` against the data model with a 6-line walker (`:15-25`), and reads components from **the first `updateComponents` message and the first `updateDataModel` message** in a static array (`:28-35`).

| | Aleph | benchmesh |
|---|---|---|
| Processor | upstream `@a2ui/web_core` `MessageProcessor`, stateful | none — reads a message array |
| Domain components | 20 | 0 (primitives only) |
| Bindings | full binder, schema-classified (DYNAMIC/ACTION/STRUCTURAL/STATIC) | `{path}` string walk |
| Deltas | yes, per-path `updateDataModel` over SSE | **no** — one-shot surface |
| Actions | full router + ledger | **none** — surfaces are read-only |
| Transport | live SSE, resume, seq | `getJSON` fetch (`scorecards.tsx:37,101`) |

benchmesh's A2UI is **a static server-rendered report format**, not an interactive protocol. Its interactivity lives in hand-written React cards (`workflow-review.tsx` etc.) that talk to REST. Aleph's A2UI is strictly more capable **and** strictly more complex.

Correspondingly, benchmesh's charts are **server-rendered SVG as base64 data URIs** (`charts.py:25-27` `svg_data_uri`, `a2ui.py:251-253`) dropped into an A2UI `Image`. Dependency-free, deterministic, works anywhere. `charts.py:14-22` documents its palette and — importantly — `forest_plot_svg` (`:34-45`) colors a CI **teal/amber when it excludes zero, muted grey when it crosses** — "the honest read of 'distinguishable from no effect'." That is exactly the epistemic-honesty instinct Aleph needs.

### Stack comparison

| | Aleph | benchmesh |
|---|---|---|
| Build | Vite 8, React 19.2.7, TS 6, Tailwind 4.3 (`package.json`) | Next.js 16 App Router, React 19 (`CLAUDE.md:74`, unverified) |
| Routing | hand-rolled `pushState` (`App.tsx:12-24`) | single page, `/?open=<kind>` deep link (`canvas.tsx:72-84`) |
| CopilotKit | `^1.58` `@copilotkit/react-core` + `/react-ui` + `/a2ui-renderer`; `<CopilotChat>` component | `@copilotkit/react-core/v2` provider only (`providers.tsx:3,8`) — **the chat UI is hand-written** |
| Agent transport | AG-UI via Node copilot-runtime :4000 | raw SSE POST parsed by hand (`assistant-panel.tsx:89-143`) + AG-UI events server-side (`ag_ui.py:9-33`) |
| Styling | Tailwind + token shim | hand-written CSS + tokens |
| API | react-query + `lib/api.ts` | Next proxy routes + `authFetch` (`api.ts:43-56`) |

Notable: benchmesh **mounts `CopilotKitProvider` but doesn't appear to use it** for its assistant — `assistant-panel.tsx` streams `/api/assistant-stream` itself. [I, from staged files]

---

## B8. What to port, what not

### Port — high value, low risk

**1. `card-shell.tsx` as a card frame contract → fixes Aleph's "20 cards, 20 layouts" problem.**
Aleph's `CardShell` (`_shared.tsx:54-86`) is a *div with a title*. benchmesh's (`card-shell.tsx:20-202`) is a **capability contract**: eyebrow, title, savable, pin, minimize, close. Port the *concept*, not the drag code: give every Aleph catalog card a shell with a **kind eyebrow** (`store.tsx:90-110`), a **pin-to-brief** button (Aleph already has `pin_to_brief` in the router — today it's agent-only), and an **optional `savable`** that maps to Aleph's artifact store. Immediate win: pinning becomes an analyst gesture, not just an agent verb.

**2. The token system + theme boot → deletes `tokens.css:119-182` entirely.**
Adopt `globals.css:6-82`'s structure: semantic tokens only (`--surface/--ink/--muted/--line/--accent/--ok/--warn/--err` + `-soft` companions), hand-tuned dark reciprocals, `data-theme` on `<html>`, and the pre-paint inline script from `layout.tsx:14,20`. Then migrate Aleph components off `bg-white`/`text-slate-*` onto the tokens and **delete the `!important` shim**. Aleph's card primitives (`_shared.tsx:38-43,68`) already write the token form — the migration is mostly the chrome. Also port the two one-liners Aleph is missing outright: global `:focus-visible` (`globals.css:111-114`) and global `prefers-reduced-motion` (`:116-123`).

**3. The context bar → Aleph's missing "what am I looking at" surface.**
`context-bar.tsx:18-58` gives one persistent, shared answer for human and agent. Aleph *already computes* this — `useAgentContext` at `CopilotChatSurface.tsx:73-80` sends `{active_tab, open_page_id, open_page_title, selection}` to the agent — **but never shows it to the human**. Porting it is nearly free and closes an asymmetry: the analyst should see exactly the context the agent sees. For the belief graph, extend it to `Looking at: [Claim c-8812] [in Page X] [rev 14] [status: contested]`.

**4. The staged approval pipeline strip.**
`assistant-panel.tsx:48-70` + `:715-750` renders `Workflow › Spec › Experiment › Run` with `data-on`/`data-done`, persisted on the session (`session-context.tsx:41-64,75`) so a reload lands on the same step, and a guard at `:266-272` that demotes a stuck `running` back to the approval step. Aleph's adjudication flows (contradiction review, merge approval, retraction blast-radius) are exactly this shape and currently have **no visible stage model** — an ApprovalCard just appears in Briefs with no sense of where it sits in a process.

**5. `lifecycle.tsx` — the review-card pattern.**
`:21-37` canonical status ordering + `groupByStatus`; `:39-55` `useCardList`; `:59-69` one transition call; `:71-114` `ValidationReportView` rendering per-check `outcome/severity/message/remediation` with a blocking count. And in `workflow-review.tsx:260-265`, an **inline explainer telling the reviewer what each button actually does** ("Validate previews the checks…; Approve is one act: the control plane re-validates and registers…"). Aleph's `ApprovalCard.tsx` has title + summary + two buttons and no explanation of consequence. For dispute-edge adjudication, "what will happen if I approve this" is the whole job.

**6. Server-rendered deterministic SVG for statistical displays.**
`charts.py` + `a2ui.py:251-253`: no client deps, byte-stable, embeddable in a static A2UI Image, **and archivable as an artifact**. Aleph already has the pipeline (`code_runner` → `RenderedAsset` → `ImageCard`); what it lacks is charts.py's *epistemic* rendering conventions — muted-grey when a CI crosses zero (`charts.py:19-21, 40-45`). Port those conventions into Aleph's `viz_builder`.

**7. Ephemeral-vs-persisted state discipline.**
`run-context.tsx:5-7` — live frames and step telemetry explicitly excluded from persistence, while `store.tsx`/`session-context.tsx` persist layout and threads. Aleph persists nothing client-side; when it starts (layout, pinned view state), copy this split.

### Port with heavy modification

**8. The rail + canvas model — port the *rail*, be careful with the *canvas*.**

The rail (`rail.tsx:9-21`) is strictly better than Aleph's 5-tab bar (`A2UIRightPanel.tsx:38-54`): it scales to 11+ entries at fixed cost, uses icons + hover tooltips, and separates "open" from "active." **Aleph's tab bar cannot hold the surfaces Part C requires** — Wiki/Library/Notes/Hypotheses/Briefs + Contradictions + Concepts + Provenance is 8, at ~50px each.

The free-floating canvas is a harder call. Pro: two claims side by side, a grounding tree next to the claim it grounds — exactly the comparative reading a belief graph demands, which Aleph's single-column 30% panel cannot do. Con: it drops Aleph's delta streaming (each benchmesh card fetches independently — `scorecards.tsx:37`), it drops the ordering guarantees, and free positioning is an ongoing tax on the user. **My recommendation: port the rail + a *tiled* multi-card region, not free-floating drag.** Aleph already has `react-resizable-panels`; a rail-driven 1/2/3-pane split with pin/close/minimize per pane gets ~80% of the canvas benefit and keeps the SSE model intact.

### Do not port

- **The `window` CustomEvent bus** (8+ event names across `assistant-panel.tsx`, `workflow-review.tsx:97-98`, `api.ts:259`, `store.tsx:292`). Untyped, unlogged, ungoverned. Aleph's ledger-audited `ActionRouter` is categorically better and should stay the only cross-surface channel.
- **localStorage as source of truth for sessions** (`session-context.tsx:91-94`). Aleph's threads are server-owned and project-scoped; inverting that would break the audit model.
- **The 158-line A2UI renderer.** A downgrade from Aleph's binder — no deltas, no actions, no domain components.
- **Regex-parsing agent prose for directives** (`assistant-panel.tsx:145-159` ```` ```benchmesh-action ```` blocks, `:163-178` `stripProposalJSON`). Aleph's typed `useFrontendTool` + `render_a2ui` injection is the right answer; benchmesh's `stripProposalJSON` heuristics (`:170-176` falls back to matching `\{[\s\S]*"(steps|criteria)"[\s\S]*\}`) are exactly the fragility Aleph avoided.
- **Cards fetching their own data.** Directly violates Aleph's enforced no-self-fetch invariant.
- **Next.js.** Aleph's Vite SPA is fine; the migration cost buys nothing here.

---

# PART C — Synthesis

## C9. What the web-of-belief vision requires that **neither** codebase has

For each: the sketch, and whether Aleph's A2UI catalog model expresses it **as-is**, with **catalog additions**, or needs **protocol changes**.

---

### C9.1 Grounding-tree / provenance inspector
**Sketch — `GroundingTreeCard`.** Root = claim. Level 1 = citation markers `[cN]`. Level 2 = the chunk(s) each resolves to, showing **verbatim span text with the supporting sentence highlighted**. Level 3 = source metadata (title, DOI, retraction status, verification tri-state from `aleph-scholar`). Each node carries a support/undercut/neutral role and a strength. A "show the actual words" affordance is non-negotiable — this is the card that answers *"does the source actually say that?"*

Props: `claim_id`, `nodes: [{id, parent_id, kind: claim|citation|chunk|source, label, span_text, char_start, char_end, role, strength, source_status}]`, `open_source_action`, `flag_action`.

**Feasibility: catalog additions only.** The data exists — `Citation.chunk_ids` (`packages/aleph-wiki/.../models.py:141`) → `DocumentChunk.char_start/char_end/text` (`aleph-rks/.../models.py`). `_resolve_citations` (`routes/surfaces.py:476-533`) already does the two-query resolution and would extend to spans in the same shape. A recursive tree of ~50 nodes is a `z3.array(z3.any())` structural prop, exactly like `WikiPageCard.claims`. **No protocol change.**

**Gap that is not the UI's:** claims currently have no per-citation *role* (support vs. undercut) or strength. That's backend claim-model work (§C10).

---

### C9.2 Claim node view — evidence for/against
**Sketch — `ClaimNodeCard`.** Replaces `ClaimCard` as the primary unit. Header: canonical claim text, computed status badge, calibration. Body: three columns — **Supports** / **Contradicts** / **Refines-or-Superseded-by** — each listing edges to other claims with edge strength, plus the grounding summary. Footer: "Why this status" (→ C9.6), "What breaks if this falls" (→ C9.5).

Props: `claim_id`, `text`, `status`, `confidence_interval`, `edges_in`/`edges_out: [{edge_id, type: assumes|contradicts|refines|supersedes, other_claim_id, other_text, strength, asserted_by, asserted_at}]`, `grounding_summary`, actions.

**Feasibility: catalog additions only** for display. The three-column layout is basic-catalog `Row`+`Column`, already merged into the shared catalog (`A2UISurfaceView.tsx:59`). Edges are structural literal arrays.

**Protocol pressure appears at scale:** the current model re-diffs a whole tab per wake (`routes/surfaces.py:242-259`). A claim with 200 edges inside a 5,000-claim project makes "rebuild the tab, diff it" untenable. **This is the first real protocol change** — see C10 Phase 3.

---

### C9.3 Contradiction / dispute review queue
**Sketch — `DisputeQueueSurface` + `DisputeCard`.** A new rail entry. Queue grouped by status (`proposed | under_review | accepted | rejected`) in benchmesh's `groupByStatus` style (`lifecycle.tsx:21-37`). Each `DisputeCard` shows the two claims **side by side**, the proposing agent + its rationale, the evidence each side rests on, a diff-like conflict highlight, and the *consequence preview*: "accepting marks C-991 superseded; 14 downstream claims and 3 briefs are affected." Actions: `accept_edge` / `reject_edge` / `request_evidence` / `defer`.

**Feasibility: catalog additions + one protocol-adjacent change.** The card itself is expressible. But:
1. Aleph's `ApprovalCard` (`catalog.py:186-226`) has a **closed `target_kind` enum** — adding `claim_edge` is a schema bump, fine.
2. The consequence preview requires a **server-computed impact projection** delivered as a bound prop. That's a new service method, not a protocol change.
3. **Side-by-side needs horizontal room** — this card does not work in a 400px panel. This is the concrete forcing function for the rail+multi-pane port.

---

### C9.4 Concept-mapping editor
**Sketch — `ConceptMapSurface`.** Left: concept list with alias counts and ambiguity flags. Right: for the selected concept — canonical label, definition, **surface forms** (each with the sources that use it), **maps-to** relations (`same_as | broader | narrower | distinct_from`) to other concepts, and a merge/split affordance. Every mapping shows *who asserted it* (agent vs. analyst) and *on what evidence*.

**Feasibility: catalog additions, but this is the one that most wants an editor primitive Aleph lacks.** Display is expressible. Editing is the problem: Aleph's only editing card is `NoteEditorCard` (debounced markdown textarea, `NoteEditorCard.tsx:25-115`) and `FormCard` (`FormCard.tsx:13-89`, four field types from `catalog.py:250-271`). Neither expresses "drag surface-form X from concept A onto concept B" or "select two concepts and assert `broader`."

Two options: (a) express every edit as a discrete `onAction` on a list row — ugly but **works today, zero protocol change**; (b) add a `RelationEditorCard` with a constrained relation-assertion interaction. I'd ship (a) first.

Aleph does have the right substrate: `Alias` (`aleph-wiki/.../models.py:160`) and the curator's `PageMergeProposal` (`:228`) already model "these two things are the same," and merges already flow through Briefs as human-gated `ApprovalCard`s (`routes/surfaces.py:630-657`).

---

### C9.5 Belief-revision impact view — "what breaks if this falls"
**Sketch — `ImpactCard`.** Given a claim (or a source), show the **transitive dependency cone**: claims that `assumes` it (directly and transitively), pages whose current revision cites it, artifacts whose lineage includes those pages, briefs that quote them. Grouped by blast radius with counts, each row showing the *path* by which it depends. A "simulate retraction" toggle that greys the cone without committing.

**Feasibility: needs the most, but *Aleph already has a working precedent*.** `aleph-reviewer` has a "source-retraction/blast-radius service" (`CLAUDE.md:86`), retraction sets dependent claims to `confidence="retracted"` (`routes/surfaces.py:452-455`), and `_annotate_drift` (`:312-358`) already computes a live artifact-invalidation flag off the current wiki graph. The pattern — *live-computed, never stored* — is exactly right and should be reused.

**But:** the natural display is a **graph or a nested cone**, and Aleph has no graph renderer. `GraphCard` was deliberately deleted (`check-catalog-roster.sh:29`) and `@xyflow/react` is an unused dependency. My opinionated call: **do not reintroduce a free-form node-link graph.** Ship the impact view as a **grouped, sorted, expandable dependency list with a magnitude bar** — it is more scannable, more accessible, works in a narrow pane, and expresses "14 claims, 3 pages, 1 brief" better than a hairball. Reintroduce graph rendering only if list-form demonstrably fails.

**Protocol status: expressible as-is**, if impact is server-computed and bound as a structural prop.

---

### C9.6 Calibration / status explanation
**Sketch — `StatusExplanationCard`.** For a computed epistemic status: the **inputs** (evidence count, source quality, contradiction count, freshness, agent agreement), the **rule or model** that combined them, the **contribution of each input** to the result, the **counterfactual** ("if source S-14 were retracted, this drops to `contested`"), and — if calibrated — the observed accuracy of this status class historically.

**Feasibility: expressible as-is.** All bound props. benchmesh has the closest analogue in `_scoring_method_text` (`a2ui.py:318-332`), which states in prose that "a semantic metric is only reportable once its judge is calibrated" and reports agreement/MAE. Aleph should adopt that norm: **a status badge must be clickable to its own justification.** Today `WikiPageCard.tsx:294-296` renders `confidence` as an opaque `Pill` with no explanation path — that is the single most important small fix in the existing UI.

---

### C9.7 The cross-cutting gaps neither codebase addresses

- **Addressability.** Neither has real URL state (Aleph: none; benchmesh: `/?open=kind` deep link that immediately deletes itself, `canvas.tsx:78-81`). A belief graph *requires* stable links to claim/edge/dispute.
- **Time / revision navigation.** Aleph has `DiffCard` for two wiki revisions (`DiffCard.tsx:14-45`) and nothing for "how did this belief's status change over time." No timeline component exists in either.
- **Bulk adjudication.** Both are one-item-at-a-time. A dispute queue with 200 proposed edges needs select-many + apply-consistent-verdict, with the ledger recording it as N events.
- **Density.** Both are comfortable-density card UIs. Reading 500 claims needs a table/inspector mode. Aleph's `TableCard` (`TableCard.tsx:11-118`) is the seed but it's a display table, not a working surface.

---

## C10. Sequenced roadmap

Ordering principle: **UI work that is independent of the claim model goes first** (it pays off immediately and de-risks everything after); UI that needs claim edges is gated on backend; the protocol change is gated on scale, not on features.

---

### Phase 0 — Foundations (no backend dependency; ~1–2 weeks)

These are pure ports/cleanups. Do them before building any belief surface, because every belief surface inherits them.

1. **Token migration + delete the dark shim.** Replace `styles/tokens.css:119-182` with benchmesh's token structure (`globals.css:6-82`); migrate `bg-white`/`text-slate-*`/`border-slate-*` in `A2UIRightPanel.tsx`, `WikiSurface.tsx`, `WikiPageCard.tsx`, `LeftPanel.tsx`, `Drawers.tsx` to `var()` form. Add global `:focus-visible` and `prefers-reduced-motion`. Add the pre-paint theme script to `index.html` (mirroring `layout.tsx:14,20`).
2. **URL state.** Encode `{tab, page_id, claim_id, card_id}` in the path/query; make `App.tsx:12-19` parse them and `workspace-ui.tsx` read/write them. Everything after this is linkable. **Do this before you build the dispute queue, not after.**
3. **Upgrade `_shared.tsx:CardShell` to a real shell**: kind eyebrow, optional pin (wire to the existing `pin_to_brief` router action — analyst-accessible for the first time), optional collapse, optional "explain" slot.
4. **Close the catalog schism.** Either (a) make `catalog.py` describe the v0.9 inline-prop shape and validate every streamed surface in `routes/surfaces.py`, or (b) delete the surface entries from `catalog.py` and extend `check-catalog-roster.sh` to compare **prop names** between `catalog.py`/`aleph-catalog-v09.tsx`/`server.ts`. I prefer (a). Also add a lint rule forbidding `from "zod"` inside `a2ui/` — the zod3 landmine deserves enforcement, not a comment.
5. **Fix the stale "13 cards" comments** (`A2UISurfaceView.tsx:50`, `copilot.tsx:11`).

---

### Phase 1 — Navigation model (no backend dependency; ~1–2 weeks)

6. **Replace the 5-tab bar with a rail** (port `rail.tsx:9-21` + `globals.css:248-333`). Aleph's tab bar is the hard ceiling on surface count; break it before you need 8 surfaces.
7. **Context bar** (port `context-bar.tsx:18-58`) — render exactly the `useAgentContext` payload from `CopilotChatSurface.tsx:73-80`. Human and agent see the same context string.
8. **Multi-pane right region.** Rail-driven 1/2/3 tiled panes over `react-resizable-panels` (already a dependency), each pane an independent `A2UIStreamSurfaceView` with its own `cid`. **This is the prerequisite for C9.3 and C9.5.** Explicitly *not* free-floating drag.

*By end of Phase 1 the shell can host belief surfaces. Nothing above needs a single backend change.*

---

### Phase 2 — Belief surfaces on the **existing** data model (~2–3 weeks, partly parallel with backend)

Sequenced by how much new backend each needs.

9. **`StatusExplanationCard` (C9.6) — first, because it needs almost nothing.** Make every `confidence` Pill clickable. v1 explains from data that already exists: citation count, source retraction status, freshness/volatility/verified_at (all already bound into `page_meta`, `routes/surfaces.py:428-435`). Ship the *affordance* now; enrich as the model lands.
10. **`GroundingTreeCard` (C9.1).** Extend `_resolve_citations` (`routes/surfaces.py:476-533`) to include chunk `text`/`char_start`/`char_end`. Needs no claim-edge work — this is the highest-value surface reachable today, and it is the one that makes the whole system credible.
11. **`ImpactCard` (C9.5), source-scoped v1.** Reuse the existing retraction blast-radius service + the `_annotate_drift` live-compute pattern (`:312-358`). Answers "what breaks if this *source* falls" before claim edges exist.

---

### Phase 3 — Gated on backend claim-model work

**Backend prerequisites, in dependency order:**
- **B1.** `ClaimEdge` table: `{src_claim_id, dst_claim_id, edge_type ∈ {assumes,contradicts,refines,supersedes}, strength, asserted_by, asserted_at, status, evidence_refs}` + `project_id`/`access_scope`/ledger per CLAUDE.md rule 6.
- **B2.** Promote claims to first-class persistent nodes with stable identity across revisions (today `WikiClaim` is `revision_id`-scoped — `aleph-wiki/.../models.py:127`, and `_retracted_page_ids` filters on `revision_id == current_revision_id`, `routes/surfaces.py:466-468`). **This is the single largest backend change and everything in C9.2/C9.3 depends on it.**
- **B3.** Per-citation role + strength (support/undercut) on `Citation`.
- **B4.** Concept layer: `Concept` + `SurfaceForm` + `ConceptRelation`, extending the existing `Alias` model.
- **B5.** Computed status service producing status + inputs + contributions (feeds C9.6 properly).
- **B6.** Transitive impact projection over `ClaimEdge` (feeds C9.5 claim-scoped).

**UI, gated as noted:**

12. **`ClaimNodeCard` (C9.2)** — gated on B1+B2. Replaces `ClaimCard` in the reader's claim list (`WikiPageCard.tsx:281-304`).
13. **`DisputeQueueSurface` + `DisputeCard` (C9.3)** — gated on B1. New rail entry. Bump `ApprovalCard.target_kind` enum (`catalog.py:189-198`) to include `claim_edge`; add `accept_edge`/`reject_edge`/`request_evidence` to `_ACTIONS` (`catalog.py:337`) + handlers in `a2ui_handlers.py` (registration block `:1245-1265`). Port `lifecycle.tsx`'s `groupByStatus` + `ValidationReportView` + the inline consequence explainer from `workflow-review.tsx:260-265`. **Requires Phase 1's multi-pane.**
14. **`ImpactCard` v2, claim-scoped** — gated on B6.
15. **`ConceptMapSurface` (C9.4)** — gated on B4. Ship edit-as-discrete-`onAction` first; only add a relation-editor primitive if that proves painful.
16. **Status explanation v2** — gated on B5.

---

### Phase 4 — Protocol changes, gated on **scale**, not features

Do not do these early. Everything in Phases 0–3 works on the current protocol.

17. **Scoped/incremental surface recompute.** Today every LISTEN/NOTIFY wake rebuilds the whole tab (`routes/surfaces.py:242-259`). Change `ChangeBroker` signals to carry entity identity so a surface can recompute one subtree. This is the change that makes a 50k-claim graph viable.
18. **Windowed/paginated bound collections.** Add a cursor convention to the data model (`{items, cursor, total}`) so a dispute queue streams a window, with `load_more` as a router action.
19. **Multi-surface-per-pane / surface composition.** Formalize what `compose_dossier` (`a2ui_handlers.py:846-960`) prototypes: agent-composed multi-card views as first-class, data-bound surfaces rather than pinned static payloads. This is the natural home for "the agent assembled a case for you."

---

### Leave alone

- The delta substrate (`surface_streamer.py`, `routes/surfaces.py:151-270`) — until Phase 4 scale forces it. It is correct.
- The `ActionRouter` ledger path (`action_router.py:68-154`) — extend with new actions; do not add a second channel.
- `isSandboxedAssetSrc` + the iframe/vega network blocks (`_shared.tsx:23-28`, `ChartCard.tsx:11-32`) — the security posture is sound.
- The shared catalog / one-card-two-surfaces design (`buildAlephCatalog`, `copilot.tsx:35-38`).
- The `code_runner` → artifact → `ImageCard`/`ChartCard` pipeline. Adopt benchmesh's *chart conventions* (`charts.py:14-22, 40-45`) inside it; don't replace it.
- CopilotKit's `useAgentContext`/`useFrontendTool` eyes-and-hands model (`CopilotChatSurface.tsx:67-176`) — extend it with `open_claim`, `open_dispute`, `explain_status`; the pattern is right.

---

### The single most important judgment call

The web-of-belief UI's defining requirement is **comparative reading under provenance** — two claims, their evidence, and their consequences, visible at once. Aleph's 30%-wide single-column tabbed panel structurally cannot do that, and no amount of new cards fixes it. benchmesh already solved the shell problem (rail + multi-surface region + a real card frame) and solved it with a **better design system and worse protocol** than Aleph.

So: **take benchmesh's shell, keep Aleph's protocol.** Phase 0–1 is that trade, it costs ~3 weeks, it depends on zero backend work, and every belief surface after it gets cheaper. Doing Phase 3 before Phase 1 would mean building a dispute review queue into a 400px column — which is the one outcome that would make the adjudication story fail on contact with a real analyst.agentId: a6d4f93f08168b5eb (use SendMessage with to: 'a6d4f93f08168b5eb', summary: '<5-10 word recap>' to continue this agent)
<usage>subagent_tokens: 274744
tool_uses: 79
duration_ms: 600471</usage>