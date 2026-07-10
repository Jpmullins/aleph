# Future work (parked, not shipped)

This file tracks deliberately-deferred ideas and pre-built-but-unwired scope, so
they are recorded rather than rediscovered. Nothing here is live; each entry says
why it is parked and what wiring it would take. Keep this honest — when an item
ships, delete it here and describe it in the relevant `docs/` file instead.

## Obsidian-compatible wiki export (one-way mirror)

**Idea.** Materialize the compiled wiki as a read-only [Obsidian](https://obsidian.md)
vault so a human can browse the graph, hand-read pages, and use Obsidian's graph
view / backlinks without touching the live system.

**Why an export, not a store.** The wiki's value is the *relational structure
around* the markdown — `WikiClaim` / `Citation` → `SourcePage` → `Source` rows drive
retraction blast-radius, the citation-health dimension of freshness scoring, and the
reviewer's fabricated-DOI pass; `commit_revision` is a row-locked, hash-idempotent,
ledger-writing transaction; the curator / refresh / synthesis paths write
concurrently under `FOR UPDATE`. A filesystem vault as the source of truth would
turn joins into grep-and-parse, make rule 4 (ledger event in the *same* transaction)
physically unsatisfiable, and reintroduce cross-process file-locking. So the vault is
a **projection**, never the substrate.

**Shape when built.**
- A worker (or a mode of the curator) writes `vault/<project-slug>/<page-slug>.md`
  from each page's current `WikiRevision.body_md`.
- YAML frontmatter carries the trust metadata: `freshness`, `volatility`,
  `verified_at`, `page_kind`, `status`, and a `retracted-source` flag when any
  contributing source is retracted.
- Claims render as Obsidian callouts (`> [!cite]`) with their citation markers;
  wikilinks are already `[[...]]`, so they map directly.
- Regenerate on the same `ChangeBroker` LISTEN/NOTIFY wake the surface streams use —
  no polling, no file watchers.
- Write to the fs `AssetStore` (or a dedicated bind mount); serve/zip via the
  existing authenticated streaming route if remote access is ever wanted.

**Optional round-trip (later, if wanted).** Treat a changed vault file as a *proposed*
revision through `WikiService.commit_revision` + a `HandEditMark` — never as direct
truth. Do not build this until the one-way mirror has proven useful.

**If the real motivation is "agents work better on a filesystem":** prefer a
filesystem-*shaped tool layer* over `WikiService` (list/read/write-page tools that
hit the service, Claude-Code-style) rather than making files the substrate. Agents
get file ergonomics; the trust layer keeps its transactions.

**Effort:** ~a few hundred lines for the one-way mirror; no schema change.

## Richer wiki-progress signals on the surface stream

**Context.** `next-steps.md` item 4 asks for visible progress of wiki bootstrap /
refresh ("some kind of progress graphic and report"). The old `useWikiLiveSignals`
hook gestured at this (`compilingPages` / `recentlyCommitted` / `isAgentBuilding` +
"editing…" presence pulses) but was wired to react-query keys that no longer exist
post-WP-4, so it did nothing; it has been removed.

**What already exists.** The backend data source is built and unit-tested: the
`changes` router (`GET /v1/projects/{id}/changes/stream`) subscribes to the
ChangeBroker and emits compact `committed` / `compiling` / `compile_done` signals
(pure serializers in `test_changes_serializers.py`). It is currently consumer-less
(allowlisted in `check-route-reachability.sh`) after the broken `useWikiLiveSignals`
hook was removed.

**Shape when built.** Build a *proper* consumer for the retained `changes/stream`
endpoint — or, better, emit wiki-build progress as first-class `AgentEvent` phases
(the research loop already does this) and/or a dedicated Briefs card, consumed by the
existing `ActivityCard` and surface-stream substrate — not a self-fetching component.
Reuse the seq-stamped delta path; no polling.

## aleph-datasets (parked foundation for the figure/chart goal)

**Status.** The package (`Dataset` / `DatasetVersion` / `Observation` models,
`dataset_service`, `vega_compile`, `schema_inference`) and the `artificialanalysis`
`dataset_rows` connector are **built but unwired** — no runtime caller drives them.
Per GOAL.md, the figure-composition stack is the *next* goal and datasets are its
foundation, so this is intentional pre-build, not accidental cruft.

**Disposition (decided).** Keep the ORM models + migration (no destructive
down-migration); the service code is quarantined (see the package README) rather than
deleted. Wiring it means: branch the research loop on `output_kind == "dataset_rows"`
to call `extract_rows` → `commit_version`, and expose a datasets route + surface card.

## Deferred cleanups (verified-unsafe to do without infra this pass)

These were identified in the 2026-07-10 review but need a running stack, a lockfile
regen, or a CI change that can't be verified in a code-only pass. Each is safe and
worth doing when that infra is at hand:

- **Prune unused web deps** — `@tanstack/react-router`, `@xyflow/react`,
  `maplibre-gl`, `@copilotkit/react-ui` have zero `src` imports. Removal needs a
  `pnpm-lock.yaml` regen (CI runs `--frozen-lockfile`), so do it with `pnpm install`
  in the loop.
- **copilot-runtime CI + lockfile** — versions are now pinned (`1.58.0` / `0.0.53`),
  but the service still has no lockfile and no `tsc`/CI gate. Add a lockfile, a
  `typescript` devDep + `typecheck` script, and a CI step.
- **Wire Playwright into CI** — 10 specs under `tests/playwright/specs` never run in
  CI. Add at least a smoke job against the booted compose stack (or delete the specs).
- **Make the eval gate exercise the real system** — `aleph_evals` currently grades 6
  fixture rows that embed their own `actual` output. Wire the existing
  `adapters/freshqa.py` / `adapters/deepresearch_bench.py` live-stack drivers into
  `runner.py` (nightly), or at minimum have scorers consume produced output.
- **Finish or drop the dark theme** — the token system (`styles/tokens.css`) is
  complete and `ThemeToggle` sets `[data-theme]`, but most components hardcode light
  slate classes, so dark mode renders as a patchwork. Migrate the ~15 files to tokens.
- **Upgrade the wiki markdown renderer** — `WikiBodyMarkdown` handles only headings +
  paragraphs + inline wikilinks/citations; the primary read surface can't render
  lists, emphasis, code, blockquotes, tables, or single-newline breaks.
- **Minor:** the one-shot `A2UISurfaceView` export is unused (only `buildAlephCatalog`
  and `A2UIStreamSurfaceView` are consumed); remove it when next touching that file.
