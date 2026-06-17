# Wiki Curation + A2UI Rebuild — Design

**Date:** 2026-06-17
**Status:** Approved for planning (brainstorming complete; implementation plan to follow)
**Owners:** Aleph core
**Supersedes:** nothing (additive to the post-Inc-8 wave line)

---

## 1. Context

This spec covers two converging tracks the user asked to prioritize — **(3) wiki management/curation** and **(4) deeper A2UI adoption** — plus the UI/rendering bugs that block a good wiki experience, folded in where they belong. A third symptom, the **broken Live assistant**, was diagnosed and fixed live in the same session and is recorded here as shipped (§3) because it gates any conversational curation work.

The decision taken during brainstorming: the A2UI layer is **rebuilt fully on upstream A2UI standard primitives**, not extended incrementally. The rich wiki-curation surface is therefore built **once, on the new A2UI foundation**, rather than built on today's bespoke catalog and migrated later. This reorders the stages: A2UI foundation first, curation surface on top.

### Authoritative references (pinned)

- **Upstream A2UI:** local clone `~/code/A2UI`. Current npm packages: `@a2ui/react@0.10.1`, `@a2ui/web_core@0.10.1`, `@a2ui/markdown-it@0.0.4` (verified via npm 2026-06-17). Protocol specs in `~/code/A2UI/docs/specification/` — `v0.9`, `v0.9.1`, **`v1.0` (candidate)**. Standard catalog + data-binding: `docs/reference/components.md`, `docs/concepts/data-binding.md`. Agent patterns: `docs/guides/agent-development.md`. MCP: `docs/guides/a2ui_over_mcp.md` + recipe `samples/community/mcp/a2ui-over-mcp-recipe/server.py`.
- **Aleph A2UI today:** catalog `packages/aleph-a2ui/src/aleph_a2ui/catalog.py` (`CATALOG_ID="aleph-v1"`, `CATALOG_VERSION="1.0.0"`), wire protocol `messages.py` (`A2UI_VERSION="v0.9"`), delta `surface_streamer.py` (RFC-6902), renderer `apps/web/src/a2ui/aleph-catalog-v09.tsx` (18 bespoke component impls), composition `apps/web/src/a2ui/A2UISurfaceView.tsx`, cards `apps/web/src/a2ui/components/*.tsx`.
- **Wiki:** compile `packages/aleph-wiki/src/aleph_wiki/agent/workflow.py`, index `index_service.py`, curation services `handedit_service.py` / `feedback_service.py` / `alias_service.py`, routes `apps/api/src/aleph_api/routes/{wiki,handedits,feedback,aliases,synthesize,notes}.py`, retrieval `packages/aleph-assistant/src/aleph_assistant/retrieval/router.py`, assistant tool `apps/api/src/aleph_api/copilot_agent.py` (`search_wiki`).

---

## 2. Problem statement — what it should be vs. how it is

### Wiki

**Should be:** a *living, navigable, curated* knowledge base. Pages link to each other (clickable, navigating in place) and out to sources/external URLs (clickable). Every page surfaces its lifecycle (draft / approved / archived), who/what last edited it, any pending synthesis proposal, broken (unresolved) links, and protected (hand-edited) sections — and you can act on all of it **inside the wiki surface**, by clicking *or* by asking the assistant. Retrieval reliably finds the wiki the agents build.

**How it is:**
- **Links don't render.** `apps/web/src/components/WikiBodyMarkdown.tsx` parses only `[[wikilinks]]`, `[c#]` citations, and headings — there is **no `[text](url)` markdown-link parsing**, so external links render as dead grey text (`WikiBodyMarkdown.tsx:10-12`).
- **Wikilinks aren't navigable.** `WikiBodyMarkdown` *can* render `[[...]]` as a clickable `WikilinkChip` with an `onNavigate` callback, but `WikiSurface.tsx:283` calls it **without** `onNavigate`, so inline wikilinks do nothing. The separate "Links out" section (`WikiSurface.tsx:312-324`) renders links as static `<span>`s. The compile pipeline is healthy — it inlines and resolves `[[wikilinks]]` into page bodies (`workflow.py` `_wikilinks_from_body`, `_node_wikilink_resolve`) — so this is purely a render gap.
- **Curation is scattered, not a surface.** The backend is largely complete and wired (hand-edit marks, rejection feedback, aliases + repair-links, note-promote, synthesis approve/reject) but spread across the Briefs tab and routes with no presence in the wiki surface. There is no status badge, no "needs attention" view, no inline approve/protect/repair. So *how the wiki is managed and curated is genuinely unclear* — the operations exist but are undiscoverable.
- **Retrieval may not find it.** During verification the assistant's `search_wiki` returned empty while the panel listed 4 pages. Retrieval does **not** filter by status (`index_service.select_pages` FTS over `WikiIndex.index_tsv`, no status clause — confirmed). The likely cause is **WikiIndex freshness/FTS population** (pages exist in the table but their index rows weren't refreshed, or the query didn't match). This needs reproduction and a fix; it is treated as an investigation item, not an assumed cause.

### A2UI

**Should be:** surfaces composed from upstream A2UI **standard primitives** (layout + inputs) on the **latest catalog/protocol**, with **JSON-Pointer data-binding + RFC-6902 delta streaming** so agents update cards reactively, and an **A2UI MCP server** that makes the catalog/surfaces a first-class, introspectable capability.

**How it is:** Aleph ships a **bespoke parallel catalog** (`aleph-v1`) of 18 custom component types (5 surfaces + 13 cards) that happen to render through the `@a2ui/react` machinery but **do not use any upstream primitive** (Row/Column/Card/Text/List/TextField/…). Cards bind **literal props only**; the `data_bindings` field exists in the catalog schema (`catalog.py:43`) but nothing uses it. `SurfaceStreamer` computes RFC-6902 diffs server-side but the renderer cannot consume them as reactive `updateDataModel` patches. Wire protocol is pinned at `v0.9`. `FormCard` reimplements inputs as hand-rolled HTML. There is no MCP server.

### Assistant (fixed in this session — see §3)

Was fully broken by a CopilotKit v2 unbound-`fetch` bug.

---

## 3. Already shipped this session (assistant unblock)

Verified live in the browser on `:5173` (zero console errors; agent ran `search_wiki`/`ls` tools and streamed a coherent reply):

1. **`apps/web/src/lib/fetch-bind.ts`** (new) — binds `window.fetch = window.fetch.bind(window)`, imported as the **first** import in `apps/web/src/main.tsx`. Fixes `TypeError: Failed to execute 'fetch' on 'Window': Illegal invocation`, thrown because the CopilotKit v2 transport (`@copilotkit/react-core/v2`) issues requests via RxJS `fromFetch(url, { fetch: environment.fetch })` with an unbound `fetch`.
2. **`apps/web/package.json`** — `react`/`react-dom` `19.2.6 → 19.2.7`. A fresh `npm install` (the baked web image) failed `ERESOLVE` because `@a2ui/react@0.10.1` peer-requires `react@^19.2.7`. This was a pre-existing build-health blocker.
3. **`deploy/compose/docker-compose.yml`** — `aleph-web` `mem_limit`/`memswap_limit` `1.5g → 2.5g`. Vite 8's dep pre-optimizer was OOM-killed (SIGKILL) mid-bundle on cold start, leaving the dev server in a restart loop.

Residual: a benign dev-only `AbortError: BodyStreamBuffer was aborted` from React StrictMode's double-mount aborting the first SSE stream; the reply still completes and it does not occur in a production build. No action required.

---

## 4. Goals / non-goals

**Goals**
- Wiki pages render external links and inter-page wikilinks as working, navigable links; broken links are visibly distinct.
- The wiki surface presents a clear, discoverable management/curation model (status, provenance, pending proposals, broken links, protected sections) with inline actions, all on existing backend routes.
- Right-panel cards scale fluidly to the panel.
- `search_wiki` reliably returns the project's wiki pages.
- The A2UI layer is rebuilt on upstream standard primitives at the latest protocol, with data-binding + reactive delta streaming end-to-end.
- An A2UI MCP server exposes the Aleph catalog/surfaces.
- The assistant can perform curation conversationally, gated by `ApprovalCard` for mutations.

**Non-goals**
- No spend-gating/budget friction (only the existing global cap). [per standing project guidance]
- Do **not** vendor-copy the upstream `~/code/A2UI` repo into this monorepo. Depend on the published npm/PyPI packages + a thin MCP server.
- No change to the wiki-first retrieval *contract* (page-selector → load pages + 1-hop wikilinks → composer). The `search_wiki` fix is an index-freshness fix, not a retrieval redesign.
- No new auth modes; OIDC path untouched.

---

## 5. Target architecture

### 5.1 A2UI foundation (rebuilt on upstream primitives)

The bespoke `aleph-v1` catalog is replaced by a catalog built from upstream A2UI standard components, with a **minimal set of irreducible domain extensions**.

**Standard primitives adopted (upstream v0.9.1/v1.0 catalog):** `Text, Icon, Image, Button, Divider, Card, Row, Column, List, Tabs, Modal, TextField, CheckBox, MultipleChoice (ChoicePicker), Slider, DateTimeInput`.

**Surfaces and cards re-expressed as compositions.** The 5 surfaces and the cards that are fundamentally layout + text + inputs become **compositions of primitives over a data model** rather than bespoke React components:
- Compose from primitives: `ClaimCard, SourceCard, ArtifactCard, FindingCard, HypothesisCard, NotebookCellCard, ApprovalCard, FormCard, DiffCard`, and the 5 surfaces' list/detail scaffolding.
- `FormCard` is rebuilt on `TextField/CheckBox/MultipleChoice/Slider/DateTimeInput` with upstream input-binding + client-side `checks` validation, replacing hand-rolled HTML.

**Irreducible domain extensions (remain custom catalog components, registered as catalog extensions).** These cannot be expressed as primitive compositions and stay as Aleph-authored renderer components: `ChartCard` (vega-embed), `MapCard` (maplibre), `GraphCard` (xyflow/SVG), `HypothesisMatrix` (ACH grid). They are declared in the catalog as named components the renderer resolves to bespoke React, exactly as the standard renderer resolves a custom catalog.

**Data model + binding.** Surfaces carry a JSON **data model**; components bind props to **JSON-Pointer paths** (`{"path": "/page/title"}`) instead of literals. Dynamic lists (e.g. wiki page list, claims, findings) use **templated `List`** over array paths with scoped item paths. This is what makes reactive updates possible.

**Reactive delta streaming.** `surface_streamer.py` already computes RFC-6902 diffs; these are emitted as upstream **`updateDataModel`** patches over the existing SSE `SurfaceStreamer` channel. The renderer applies them to the data model and bound components update in place — no re-emit of component trees. This replaces today's "re-emit the whole card" pattern and is the mechanism behind the live-updating wiki page (§5.2).

**Protocol/version.** Move the wire protocol from `v0.9` to the **latest the `@a2ui/react@0.10.x` renderer supports** (v0.9.1, advancing to v1.0 where the renderer supports it). The Python SDK uses `A2uiSchemaManager` to generate catalog-aware schemas/validation (per `docs/guides/agent-development.md`); `messages.py` emits `createSurface` / `updateComponents` / `updateDataModel` / `deleteSurface` aligned to that version. `CATALOG_ID`/`CATALOG_VERSION` are bumped; the copilot-runtime's `createSurface.catalogId` stamp and the web `buildAlephCatalog()` id must match (today `aleph://v1` in the runtime vs `aleph-v1` in the catalog — reconcile to one id during the rebuild).

**Both surfaces, one catalog.** The invariant that the right panel and chat-inline cards render through one shared catalog (W4) is preserved: the rebuilt catalog is consumed by both `A2UISurfaceView` and CopilotKit's `createA2UIMessageRenderer` (`apps/web/src/lib/copilot.tsx`).

### 5.2 Wiki as a navigable, curated knowledge base

Built **on the rebuilt A2UI foundation** (§5.1).

**Navigation + links.**
- Page body renders `[text](url)` external links as anchors (`target="_blank" rel="noreferrer"`), matching `SourceCard`'s existing pattern.
- Inline `[[wikilinks]]` and the "Links out" list are clickable and navigate the surface in place (wire `onNavigate` / the surface action router through the body renderer).
- Unresolved wikilinks (`dst_page_id is null`) render visibly distinct ("broken/unlinked") and offer **Repair links** (calls `POST .../aliases/repair-links`).

**Management surface.** The wiki tab gains:
- **Page header:** status badge (`draft` / `approved` / `archived`), last-editor/agent + timestamp, and a pending-synthesis-proposal indicator.
- **Inline actions** (calling existing routes): Approve / Reject a pending proposal (`routes/synthesize.py` approve/reject → page `approved`/`archived`); Protect / Unprotect a section (`routes/handedits.py` mark/clear); Give rejection feedback (`routes/feedback.py`); Repair broken links (`routes/aliases.py`).
- **Needs-attention view:** a curation dashboard listing drafts awaiting review, pages with broken links, and pending proposals — the answer to "how is this wiki curated."
- **Visibility:** surface active hand-edit marks and prior rejection feedback in the page reader so the curation state is legible.
- **Live updates:** the open page refreshes in place via `updateDataModel` patches (§5.1) when an agent edits it, extending the existing "✦ editing…" presence.

**Retrieval fix.** Reproduce the `search_wiki`-returns-empty case and fix WikiIndex freshness: ensure every committed revision refreshes its `WikiIndex` row (`index_service.refresh_page` is called from `wiki_service.commit_revision` — verify it runs for all page-creation paths, incl. compile and note-promote), and add a project-level reindex/backfill path for pages whose index rows are stale or missing. Acceptance is behavioral: after compile, `search_wiki` returns the pages.

### 5.3 A2UI MCP server

A thin server (Python, adapting `~/code/A2UI/samples/community/mcp/a2ui-over-mcp-recipe/server.py`) that exposes Aleph's rebuilt catalog and surface payloads as **MCP resources** (static catalog/schema) and **tools** (dynamic surface payloads wrapped as `EmbeddedResource(mimeType="application/a2ui+json")`), validated by `A2uiSchemaManager` against the Aleph catalog. Registered as a workspace package; **no upstream repo vendoring**. Purpose: make surfaces an introspectable, cross-tool capability and give agents a typed way to request/validate UI.

### 5.4 Right-panel scaling

`apps/web/src/components/A2UIRightPanel.tsx:33` hard-codes `w-[28rem]` inside a `react-resizable-panels` Panel already sized to 22–50% of the viewport; the fixed width fights the container. Change to fluid (`w-full min-w-0`). Make domain visualizations responsive: `GraphCard` SVG min-width floor and `HypothesisMatrix` fixed sticky column should respect the panel via `w-full` + horizontal scroll containers. These land in Stage 0 and are re-verified after the §5.1 rebuild re-expresses the surfaces.

---

## 6. Staged execution plan

Each stage is independently shippable and verified by driving the live UI in a browser (per standing project guidance), not only headless tests.

### Stage 0 — Unblock & make the wiki legible (small, high-value, current stack)
**Scope:** external-link + navigable-wikilink rendering in `WikiBodyMarkdown` + `WikiSurface` (incl. broken-link styling); right-panel scaling fix (`A2UIRightPanel`, `GraphCard`, `HypothesisMatrix`); reproduce + fix `search_wiki` empty (WikiIndex freshness). A minimal status badge on the page header is included if cheap on the current stack.
**Acceptance:** clicking an external link opens it; clicking a wikilink/"Links out" entry navigates to that page; broken links are visually distinct; cards fill and don't overflow the panel at min/max width; after a compile, `search_wiki` returns the project's pages.
**Note/tradeoff:** Stage 0 touches `WikiBodyMarkdown`, which Stage 3 re-expresses on primitives. The Stage 0 work is small and stops the bleeding; the rework cost is low and accepted.

### Stage 1 — A2UI foundation rebuild (catalog + renderer + protocol + data-binding)
**Scope:** rebuild the shared catalog on upstream primitives + the 4 irreducible domain extensions; bump protocol to latest renderer-supported version; reconcile the catalog id; emit `createSurface`/`updateComponents`/`updateDataModel`/`deleteSurface` with a data model + JSON-Pointer bindings; wire RFC-6902 → `updateDataModel` through `SurfaceStreamer` for reactive updates; migrate all 5 surfaces + the primitive-expressible cards; rebuild `FormCard` on input primitives with `checks` validation. Sub-staged by surface to keep the panel live throughout.
**Acceptance:** every surface + card renders through the rebuilt catalog in both the right panel and chat; an agent `updateDataModel` patch updates a bound component without re-emitting its tree; `FormCard` validates client-side; pyright/eslint/`pnpm build` green; the W4 "one shared catalog, both surfaces" invariant holds.

### Stage 2 — A2UI MCP server
**Scope:** stand up the thin MCP server (§5.3) as a workspace package; expose catalog/schema as resources and surface payloads as tools; validate via `A2uiSchemaManager`; register it.
**Acceptance:** an MCP client can read the Aleph catalog resource and invoke a tool that returns a schema-valid A2UI surface payload.

### Stage 3 — Wiki management/curation surface (on the new foundation)
**Scope:** page-header status/provenance/proposal indicator; inline Approve/Reject/Protect/Unprotect/Give-feedback/Repair-links on existing routes; needs-attention curation view; hand-edit + rejection-feedback visibility in the reader; live page updates via `updateDataModel`.
**Acceptance:** from the wiki tab alone, a user can see a page's status and act on a pending proposal, protect a section, repair broken links, and view drafts-needing-review — without going to Briefs or calling routes by hand; an agent edit live-updates the open page.

### Stage 4 — Curation as conversation
**Scope:** first-class assistant curation tools (approve/reject page, protect section, repair links, request review), each mutation gated by `ApprovalCard`; cost-attributed via the existing `AgentCostCallbackHandler`; reactive surfaces throughout.
**Acceptance:** asking the assistant to "approve the draft on X" surfaces an `ApprovalCard`, and on approval transitions the page and live-updates the wiki surface; every tool call writes `ModelCall`+`CostLedgerEvent` and an `ActionLedgerEvent`.

---

## 7. Cross-cutting invariants (must hold every stage)

- **Every mutation writes an `ActionLedgerEvent`** in the same transaction (integration test asserts the row). New curation actions exposed in the UI already route through existing services that do this — verify, don't bypass.
- **Every LLM/tool call writes `ModelCall` + `CostLedgerEvent`** in an OTEL/Langfuse span.
- **A2UI surfaces stay declarative** — no agent-emitted JS/SQL; the renderer validates every payload against the catalog JSON Schema.
- **Wiki-first retrieval contract unchanged** — no secret RAG; embeddings only for intra-source descent.
- **One shared catalog** for right panel + chat (W4).
- **Track upstream latest** — pin A2UI/CopilotKit/React to verified-current registry versions; manifests are source of truth.

---

## 8. Risks & tradeoffs

- **Full A2UI rebuild is the largest item.** Mitigation: sub-stage Stage 1 by surface; keep the panel live by migrating one surface at a time behind the same catalog id; the 4 domain visualizations stay as custom components, bounding the rebuild surface area.
- **Protocol bump (v0.9 → v0.9.1/v1.0).** Renderer/SDK version skew can break payload validation. Mitigation: pin to the exact version `@a2ui/react@0.10.x` supports; validate examples via `A2uiSchemaManager` in CI.
- **Stage 0 rework.** `WikiBodyMarkdown` is touched twice (Stage 0, then Stage 3 on primitives). Accepted — Stage 0 is small and unblocks daily use.
- **`search_wiki` cause unconfirmed.** Treated as an investigation item with a behavioral acceptance test, not an assumed fix.
- **CopilotKit v2 is a beta API surface.** The shipped fetch fix is a robust workaround; watch for an upstream release that binds fetch and drop the polyfill when it lands.

---

## 9. Out of scope / sequenced later

- Bootstrap-on-create wiki build (tracked separately).
- AIQ research pipeline changes.
- Auth/OIDC and SSE×OIDC gap.
- Any spend-gating UI.

---

## 10. Open verification items (resolved during implementation, not assumptions)

1. Exact protocol version `@a2ui/react@0.10.1` validates against (v0.9.1 vs v1.0) — confirm against the installed renderer before bumping `messages.py`.
2. Whether `index_service.refresh_page` runs on **every** page-creation path (compile, note-promote, hand-edit commit) — the `search_wiki` fix depends on the answer.
3. The catalog-id reconciliation (`aleph://v1` in copilot-runtime stamp vs `aleph-v1` in `catalog.py`) — pick one, update both emitters.
