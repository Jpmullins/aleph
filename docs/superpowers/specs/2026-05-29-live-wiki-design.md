# Live Wiki — design

**Date:** 2026-05-29
**Status:** approved (brainstorming) — pending implementation plan
**Vision tie-in:** "the knowledge base evolves organically in real time" (the living
multi-agent workspace). Wiki is the KB core, so it is the flagship live surface.

## Goal

The wiki right-panel tab updates the instant an agent writes — no manual refresh —
with **full presence**:

1. New / changed pages appear in the index immediately.
2. The open page you are reading refreshes immediately when an agent edits it
   (today it is a one-shot fetch that never refreshes).
3. A live "✦ agent is editing this page…" indicator shows while a wiki compile is
   in flight (before the commit lands), and a brief "updated just now" pulse flashes
   on a page when its commit lands.

## Why event-driven off the Action Ledger

Every wiki write already emits an `ActionLedgerEvent` (`action_kind="wiki.revision.commit"`,
`target_id=page_id`) inside the commit transaction — rule #4 ("every mutation writes a
ledger event"). The table is indexed and cursor-queryable by `(project_id, timestamp)`,
exactly like `AgentEvent`, which the working agent-events SSE already polls. So the
"something changed" signal exists and is the architecturally-correct source; nothing
new needs to be invented to know *that* a page changed.

In-flight presence ("editing now", before the commit) is **not** a state mutation, so it
does not belong in the ledger. It rides the existing ephemeral-progress channel —
`AgentEvent` rows emitted per workflow phase via `emit_phase_*` — which the wiki
workflow already produces. We add a page-scoped progress event so the browser learns
*which* page is being compiled.

### Approaches considered (and why this one)

- **A — event-driven off the ledger (chosen).** Reuses the ledger + the proven SSE
  cursor-poll pattern; keeps the rich markdown reader intact; refetches only the
  affected page (correct granularity for large markdown). Sub-second, low-risk.
- **B — rebuild the wiki as a data-bound A2UI surface** (like Hypotheses) so the
  existing 2.5s streamer diffs it. Rejected: re-expressing the whole reader/browser as
  declarative components and diffing markdown bodies through JSON-pointer every tick is
  wasteful and regression-prone, for marginal gain.
- **C — hybrid** (ledger trigger + data-model deltas for just the index list).
  A reasonable later refinement, not the first cut.

## Architecture

```
agent compiles page
  └─ workflow emits page-scoped progress AgentEvent {page_id,page_title,page_kind}
        └─ GET /changes/stream poll (≈0.75s, cursor) → SSE signal {kind:"compiling",…}
              └─ WikiSurface shows "✦ editing…" on that page
  └─ commit_revision writes ledger wiki.revision.commit {page_id,page_title}
        └─ GET /changes/stream poll → SSE signal {kind:"committed",…}
              └─ WikiSurface invalidates ["wiki-pages",pid] + (if open) ["wiki-page",pid,pageId]
                 clears "editing", flashes "updated just now" pulse
                    └─ react-query refetches → index + reader re-render with new content
```

The change source is the Action Ledger (committed) + AgentEvents (in-flight). A new
lightweight per-project **change-signal SSE** carries compact signals to the browser; the
existing rich `WikiSurface` (react-query) consumes them and invalidates precisely. The
heavy markdown reader is unchanged — we refetch the *affected* page, not diff it.

## Components

### Backend

#### B1. `GET /v1/projects/{project_id}/changes/stream` (new: `apps/api/src/aleph_api/routes/changes.py`)

SSE endpoint, cursor-based poll at `_POLL_INTERVAL_SEC = 0.75` (mirrors `agent_events.py`).
Project-scoped via `ProjectScopeDep`. `since` query param (ISO 8601) is an optional cursor.

Each poll runs two scoped SELECTs and merges their signals in ascending timestamp order:

1. **Committed** — `ActionLedgerEvent` rows where `project_id == project_id`,
   `timestamp > cursor`, and `action_kind` ∈ the wiki allowlist
   `{"wiki.revision.commit"}` (a module-level frozenset, extensible). →
   `{"kind":"committed","action_kind":…,"page_id":str(target_id),"page_title":payload.get("page_title"),"actor_kind":…,"ts":iso}`.
2. **Compiling** — `AgentEvent` rows joined to `AgentRun` (so we can scope by
   `AgentRun.project_id == project_id` and filter `AgentRun.agent_kind == "wiki"`) where
   `AgentEvent.timestamp > cursor` and `event_kind == "phase_started"` and the payload
   carries a `page_id`. → `{"kind":"compiling","page_id":…,"page_title":…,"page_kind":…,"ts":iso}`.
   A matching `phase_completed`/`phase_failed` with the same `page_id` →
   `{"kind":"compile_done","page_id":…,"ts":iso}` (lets the frontend clear "editing" even
   if no commit followed, e.g. a no-op revision).

The cursor advances to the max emitted `timestamp` across both sources. Each signal is
one SSE frame: `event: change\ndata: <json>\n\n`. A `: heartbeat\n\n` is emitted each
idle tick so proxies don't close the connection. On connect (before the loop), the
endpoint **replays in-flight compiles** from the last `_REPLAY_WINDOW_SEC = 60` seconds —
`phase_started` page events with no later `phase_completed`/`phase_failed`/commit for the
same `page_id` — so a page opened mid-compile shows presence without waiting for the next
signal.

The SSE auth limitation applies (EventSource can't send `Authorization`; relies on `local`
auth mode — same as the existing surface and agent-events streams). This is the documented
SSE×OIDC gap; no new exposure.

#### B2. Pure signal serializers (in `routes/changes.py` or a small `aleph_api` helper module)

Extracted so the gen-loop is thin and the mapping is unit-tested without a DB:

- `ledger_rows_to_signals(rows: list[tuple[ActionLedgerEvent]]) -> list[dict]` — applies the
  allowlist and maps to `committed` signals.
- `phase_rows_to_signals(rows: list[tuple[AgentEvent, str]]) -> list[dict]` — maps wiki
  page-scoped `phase_started`→`compiling`, `phase_completed`/`phase_failed`→`compile_done`;
  drops rows without a `page_id` in payload.

Both are pure (`list[dict]` in/out), deterministic, no I/O.

#### B3. Page-scoped progress emission (modify `packages/aleph-wiki/src/aleph_wiki/agent/workflow.py`)

The compile path knows each page's `page_id` + `title` (drafts pre-allocate `page_id`).
Emit a page-scoped `phase_started` **before** committing each page and a matching
`phase_completed` after, carrying `payload={"page_id":…, "page_title":…, "page_kind":…}`:

- A new phase name `"compile_page"` wraps the per-page commit in `_node_commit_revision`
  (source page + each topic stub), using the existing `phase(...)` context manager with the
  page payload — NOT the node-level `@with_phase` (which is per-node, not per-page). The
  node-level `commit_revision` phase stays as-is.
- Add `page_title` to the `wiki.revision.commit` ledger payload in
  `WikiService.commit_revision` if not already present (today payload is
  `{page_id, revision_no, body_sha256, page_kind, commit_message}` — add `page_title=title`).

No new tables, no migration: reuses `AgentEvent` + `ActionLedgerEvent`.

### Frontend

#### F1. `useWikiLiveSignals(projectId)` (new: `apps/web/src/hooks/useWikiLiveSignals.ts`)

Opens one `EventSource` to `…/changes/stream`, parses `change` frames, and:

- Maintains `compilingPages: Map<string, {pageTitle:string; since:number}>` keyed by
  `page_id` (or, when `page_id` is absent for a brand-new page, by a `title:`-prefixed key).
  `compiling` adds; `compile_done`/`committed` remove.
- Maintains `recentlyCommitted: Map<string, number>` (page_id → ts) for the pulse; entries
  auto-expire after ~3s.
- On `committed`: `queryClient.invalidateQueries({queryKey:["wiki-pages",projectId]})` and, if
  the page is currently open, `["wiki-page",projectId,pageId]`.
- Cursor (`sinceRef`) tracks the last `ts` so reconnects don't replay processed signals.
- Auto-reconnects on error (mirrors `ActivityCard`/`A2UIStreamSurfaceView`).

Returns `{compilingPages, recentlyCommitted}` for the view to render presence.

#### F2. `WikiSurface.tsx` (modify)

- Call `useWikiLiveSignals(projectId)`; pass `compilingPages`/`recentlyCommitted` down to the
  index list and the reader.
- **Index:** each page row that is in `compilingPages` (by id or title) renders a
  "✦ editing…" badge; a row in `recentlyCommitted` briefly gets an "updated just now"
  highlight (CSS class with a fade transition).
- **Reader (`WikiPageReader`):** if the open page id is in `compilingPages`, show a
  "✦ an agent is editing this page…" banner; when its commit arrives the reader has already
  been invalidated+refetched, so the banner clears and the pulse flashes once.
- Lower the index query's `refetchInterval` to a slow `30_000` safety net (was 4–15s
  adaptive) — freshness now comes from the event stream, the interval is just a backstop.
- Give the open-page query a `refetchInterval` of `false` (unchanged default) — it is now
  refreshed by targeted invalidation, which is the fix for "open page never refreshes".

## Data model / API changes

- **No schema change, no migration.** Reuses `ActionLedgerEvent` + `AgentEvent`.
- **One new route:** `GET /v1/projects/{id}/changes/stream`.
- **Ledger payload addition:** `wiki.revision.commit` gains `page_title` (additive; existing
  consumers ignore unknown keys).
- **New AgentEvent phase:** `compile_page` (page-scoped), emitted by the wiki workflow.

## Testing

### Unit (no compose)
- `ledger_rows_to_signals`: allowlist filter (a non-wiki `action_kind` is dropped); shape of a
  `committed` signal incl. `page_title` from payload; empty input → `[]`.
- `phase_rows_to_signals`: `phase_started`+page_id → `compiling`; `phase_completed`+page_id →
  `compile_done`; a `phase_started` with no `page_id` is dropped; ascending-ts ordering preserved.

### Integration (compose: Postgres + Redis)
- Insert a project; insert a `wiki.revision.commit` `ActionLedgerEvent` (target_id=page_id,
  payload has page_title) → `GET /changes/stream` (consume initial replay + one tick) emits a
  `committed` signal with the right page_id/title; a second project's stream does **not** see it
  (scoping).
- Insert an `AgentRun(agent_kind="wiki")` + a page-scoped `phase_started` `AgentEvent` → the
  stream emits a `compiling` signal; the matching `phase_completed` → `compile_done`.
- (Reuse the deterministic insert pattern from `tests/e2e/test_agent_events.py`.)

The SSE gen-loop is consumed via the ASGI client with a short read deadline (read the initial
replay frames + one poll tick, then assert and close) to keep the test deterministic.

### Browser (per the "verify each wave live" rule)
Ask the Live agent to ingest a source (e.g. a URL) into the wiki. Without touching the UI,
observe on the Wiki tab: the page appears in the index, shows "✦ editing…" during compile,
then flashes "updated just now" and renders the body — and an already-open page refreshes in
place when re-compiled.

## Honest scope / trade-offs

- Still a **0.75s ledger poll**, not Postgres `LISTEN/NOTIFY`. Fine for single-user local;
  LISTEN/NOTIFY is the documented scale upgrade (noted in `agent_events.py`). N viewers = N
  poll loops, same as the existing streams.
- A **brand-new page** shows presence by `title` until its row exists, then resolves to
  `page_id` (the commit signal carries `page_id`).
- **Hand-edits** (`handedit_service.mark_section`) write no ledger event today, so they do not
  trigger live updates — out of scope (the editor already sees their own change locally).
- SSE auth: relies on `local` mode (the existing SSE×OIDC gap). No new exposure; documented.
- The `changes/stream` is intentionally **general** (allowlist-filtered): Notes/Briefs can
  subscribe later by widening the allowlist and adding their own consumers. Out of scope now —
  wiki is the only consumer this iteration (YAGNI on the others).

## Out of scope (this iteration)

- Notes/Briefs/Artifacts live updates (the stream is built general; consumers come later).
- Postgres `LISTEN/NOTIFY` (poll is sufficient now).
- Live updates for hand-edits.
- Per-keystroke collaborative editing / multi-user cursors.
