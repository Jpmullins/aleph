# Real-time push layer + Live Wiki — design

**Date:** 2026-05-29 (scope expanded 2026-05-30)
**Branch:** `wave-realtime-push-live-wiki`
**Status:** IMPLEMENTED 2026-05-30 on `wave-realtime-push-live-wiki` (push layer + all
4 streams migrated + live wiki). Verified: 113 unit + 24 integration tests, and a live
end-to-end check against the rebuilt running stack (real SSE pushed compiling + committed
for a real page). Not verified: browser visual (no Chrome connected for the remote session;
frontend is tsc/eslint/build-clean and mirrors the proven ActivityCard EventSource pattern).
**Vision tie-in:** "the knowledge base evolves organically in real time" (the living
multi-agent workspace).

## Scope (as agreed)

Two layers, built together on a new branch, thoroughly tested:

1. **Real-time push layer** — replace every SSE stream's idle polling with Postgres
   `LISTEN/NOTIFY` push (instant on write) **plus a slow poll fallback** for self-healing.
   Migrate **all four** streams: agent-events, surfaces, assistant-message, and the new
   changes stream. No stubs — push primary, fallback underneath.
2. **Live Wiki** — the flagship consumer: the wiki tab updates the instant an agent writes,
   with a live "✦ agent is editing this page…" presence indicator and an "updated just now"
   pulse, built on the push layer.

Decisions locked: **all streams migrated now** (highest completeness, accepted regression
surface, mitigated by thorough per-stream tests); **hand-edits excluded** from live updates
(the editor already sees their own change; agent-writes only).

## Why LISTEN/NOTIFY + triggers (not Redis pub/sub, not app-level publish)

Every mutation already lands a row in a known table (`action_ledger_events`,
`agent_events`) or updates `assistant_messages`. A **Postgres trigger** that calls
`pg_notify` on insert/update is:

- **Automatic + can't-be-bypassed** — fires for every write regardless of code path, so
  no call site can forget to publish (a real risk with app-level Redis publish).
- **Transactionally correct** — `NOTIFY` is delivered on COMMIT, so subscribers never see
  uncommitted data. App-level publish must be carefully placed after commit at every site;
  the trigger gets this for free.

Redis pub/sub was considered (Redis is already in `app.state`) but loses both properties.
The trigger approach's one cost — a dedicated listener connection with reconnect — is
contained, and the poll fallback covers a dropped listener so the UI never goes silent.

## Architecture

```
mutation commits (ledger/agent_event/assistant_message write)
  └─ AFTER INSERT/UPDATE trigger → pg_notify('aleph_changes', {src,project_id,…})   [DB, on commit]
        └─ NotifyListener (one raw asyncpg conn, LISTEN, reconnect-supervised)        [per API process]
              └─ ChangeBroker.publish(project_id, signal)                              [in-process fan-out]
                    └─ each SSE generator: await broker.wait(project_id, timeout=FALLBACK)
                          ├─ woke on push  → run its query/recompute, emit             [instant]
                          └─ woke on timeout (no push) → same, as a safety net          [self-healing]
```

### Components — push layer

#### P1. DB triggers (new Alembic migration; raw SQL via `op.execute`)

A shared notify function + per-table triggers. Payload is minimal (well under the ~8000-byte
`NOTIFY` cap) — it carries identity, not data; the stream still does a small cursor query to
fetch the actual new rows.

- `action_ledger_events` — `AFTER INSERT` →
  `pg_notify('aleph_changes', json_build_object('src','ledger','project_id',NEW.project_id,'action_kind',NEW.action_kind,'target_id',NEW.target_id,'ts',extract(epoch from NEW.timestamp)))`.
  Rows with `project_id IS NULL` (e.g. `user.create`) are skipped (no project to fan out to).
- `agent_events` — `AFTER INSERT`. `agent_events` has no `project_id` column, so the trigger
  resolves it: `SELECT project_id FROM agent_runs WHERE id = NEW.agent_run_id` (indexed PK
  lookup) → `pg_notify('aleph_changes', {'src':'agent_event','project_id':…,'agent_run_id':…,'event_kind':NEW.event_kind,'ts':…})`.
- `assistant_messages` — `AFTER UPDATE` when `body_md` or `status` changed →
  `pg_notify('aleph_changes', {'src':'assistant','project_id':NEW.project_id,'message_id':NEW.id,'status':NEW.status,'ts':…})`.

`down_revision` drops the triggers + function. No table/column changes (the `agent_runs`
lookup is read-only).

#### P2. `NotifyListener` + `ChangeBroker` (new module `apps/api/src/aleph_api/realtime.py`)

- **`ChangeBroker`** — pure in-process pub/sub, unit-testable, no DB. Holds
  `dict[UUID, set[asyncio.Queue]]`. `subscribe(project_id) -> Subscription` (context manager
  registering a bounded queue); `publish(project_id, signal: dict)` puts the signal on every
  live queue for that project (drops on full queue with a logged warning — backpressure
  safety). `Subscription.wait(timeout) -> dict | None` returns the next signal or `None` on
  timeout (drives the fallback).
- **`NotifyListener`** — opens **one raw asyncpg connection** (DSN derived from
  `settings.database_url` by stripping the SQLAlchemy `+asyncpg` driver tag — pure helper
  `asyncpg_dsn(url)`), runs `LISTEN aleph_changes`, and on each notification parses the JSON
  payload and calls `broker.publish(project_id, signal)`. **Supervised:** wrapped in a task
  that, on connection error, logs and reconnects with capped backoff; while down, every
  stream's poll fallback keeps data flowing. Built + started in the lifespan
  (`app.state.change_broker`, `app.state.notify_listener`), cancelled + connection closed on
  shutdown.

#### P3. Stream migrations (all four) — push primary, poll fallback

Each stream replaces `await asyncio.sleep(interval)` with
`signal = await subscription.wait(timeout=FALLBACK_SEC)` inside a `broker.subscribe(project_id)`
context. On wake (push or timeout) it runs its existing query/recompute and emits. Per-stream
fallback windows (safety net only; push is the real path):

1. **`agent-events/stream`** — fallback 5s. Cursor SELECT `AgentEvent > cursor` → emit.
2. **`surfaces/{tab}/stream`** — fallback 10s. Recompute `_build_tab_messages` + diff → emit
   structural/data deltas (unchanged diff logic; only the wake trigger changes).
3. **`messages/{id}/stream`** (assistant) — fallback 1s (token-streaming wants snappy). Re-read
   message, emit `token`/`done`. Push fires on each `body_md` update for near-live tokens.
4. **`changes/stream`** (new) — fallback 5s. See L-layer below.

The 5-minute / 600-iteration caps become wall-clock deadlines so behavior is unchanged.

### Components — live wiki (consumer of the push layer)

#### L1. `GET /v1/projects/{id}/changes/stream` (new `routes/changes.py`)

Subscribes to the broker; on wake, runs two scoped cursor SELECTs and merges signals in
ascending-`ts` order:

- Ledger rows with `action_kind` in a wiki allowlist (`{"wiki.revision.commit"}`, a frozenset,
  extensible) → `{"kind":"committed","page_id","page_title","actor_kind","ts"}`.
- Wiki page-scoped `AgentEvent`s (joined to `AgentRun` for `project_id` + `agent_kind=="wiki"`)
  whose payload has a `page_id`: `phase_started`→`{"kind":"compiling","page_id","page_title","page_kind","ts"}`,
  `phase_completed`/`phase_failed`→`{"kind":"compile_done","page_id","ts"}`.

On connect, replays in-flight compiles from the last `_REPLAY_WINDOW_SEC=60`s (a `compiling`
with no later `compile_done`/commit for the same page) so a page opened mid-compile shows
presence immediately. One SSE frame per signal: `event: change\ndata:<json>`.

#### L2. Pure signal serializers (`routes/changes.py`)

`ledger_rows_to_signals(rows) -> list[dict]` and `phase_rows_to_signals(rows) -> list[dict]` —
pure, deterministic, unit-tested without a DB (allowlist filter, page_id requirement,
ordering, shape).

#### L3. Page-scoped progress emission (modify `packages/aleph-wiki/.../agent/workflow.py`)

Wrap each per-page commit in `_node_commit_revision` (source page + each topic stub) in the
existing `phase(...)` context manager with phase name `"compile_page"` and
`payload={"page_id","page_title","page_kind"}`. (The node-level `@with_phase("commit_revision")`
stays.) Add `page_title=title` to the `wiki.revision.commit` ledger payload in
`WikiService.commit_revision` (additive). Side effect: the ActivityCard's existing agent-events
feed will show the new `compile_page` phase — benign extra granularity.

#### L4. Frontend `useWikiLiveSignals(projectId)` (new `apps/web/src/hooks/useWikiLiveSignals.ts`)

One `EventSource` to `…/changes/stream`. Maintains `compilingPages: Map<key,{pageTitle,since}>`
(keyed by `page_id`, or `title:<t>` for a not-yet-existing page) and `recentlyCommitted:
Map<page_id,ts>` (auto-expires ~3s for the pulse). On `committed`:
`queryClient.invalidateQueries(["wiki-pages",pid])` and, if that page is open,
`["wiki-page",pid,pageId]` — this is the fix for "open page never refreshes". Cursor (`sinceRef`)
+ auto-reconnect mirror `ActivityCard`/`A2UIStreamSurfaceView`.

#### L5. `WikiSurface.tsx` (modify)

Consume the hook; render "✦ editing…" on compiling pages (index + reader banner) and an
"updated just now" highlight on recently-committed pages. Lower the index query
`refetchInterval` to a 30s safety net (freshness now comes from the stream); the open-page
query stays event-invalidated (no interval).

## Data model / API changes

- **One migration:** triggers + notify function only (no table/column changes).
- **One new route:** `GET /v1/projects/{id}/changes/stream`.
- **Ledger payload addition:** `wiki.revision.commit` gains `page_title` (additive).
- **New AgentEvent phase:** `compile_page` (page-scoped).

## Testing (thorough — explicit user requirement)

### Unit (no compose)
- **`ChangeBroker`**: publish reaches all subscribers of a project and none of another;
  `wait` returns the signal, or `None` on timeout; unsubscribe removes the queue; full-queue
  drop is safe.
- **`asyncpg_dsn(url)`**: strips `+asyncpg`, preserves host/db/creds/params.
- **Notify-payload parse**: malformed/again payloads don't crash the listener loop.
- **`ledger_rows_to_signals` / `phase_rows_to_signals`**: allowlist filter, page_id
  requirement, `committed`/`compiling`/`compile_done` shapes, ordering.

### Integration (compose: Postgres + Redis [+ MinIO for ingest])
- **Trigger → push end-to-end**: a real `LISTEN aleph_changes` test connection receives a
  notify when a ledger row / agent_event / assistant_message is written in a committed txn;
  payload has the right `project_id`.
- **Each migrated stream wakes on push** (emits within a short deadline well under its
  fallback window), and **still emits via fallback when the listener is disabled** (prove the
  safety net).
- **changes/stream**: ledger `wiki.revision.commit` → `committed` signal (+ `page_title`),
  scoped to the right project, not leaked to another; wiki `phase_started`/`completed` →
  `compiling`/`compile_done`.
- Reuse the deterministic insert pattern from `tests/e2e/test_agent_events.py`; consume SSE via
  the ASGI client with a short read deadline.

### Browser (per the "verify each wave live" rule)
- Ingest a source via the Live agent; on the Wiki tab watch a page appear → "✦ editing…" →
  "updated just now" → body renders, and an already-open page refresh in place.
- Regression sanity on the migrated streams: ActivityCard phases still stream, a hypothesis
  still appears on the Hypotheses surface via a delta, assistant tokens still stream.

## Honest scope / trade-offs / limits

- **Push primary, poll fallback** — not pure push. This is deliberate and production-grade:
  the fallback self-heals a dropped listener so the UI never silently dies. Fallback windows
  (1–10s) are safety nets, not the latency you'll see (push is sub-100ms in practice).
- **One listener connection per API process.** Single uvicorn process today (confirmed: no
  `--workers`); if scaled to N processes, each holds its own `LISTEN` — fine, `NOTIFY`
  broadcasts to all. In-process fan-out means cross-process delivery already works (every
  process gets the notify).
- **`agent_events` trigger does a PK lookup** on `agent_runs` to resolve `project_id` — one
  indexed read per event insert. Acceptable; avoids a schema denormalization.
- **Hand-edits** (`mark_section`) write no ledger event, so they don't push — out of scope
  (editor sees their own edit locally).
- **SSE auth** relies on `local` mode (EventSource can't send headers) — the existing,
  documented SSE×OIDC gap; no new exposure. Must be addressed before an OIDC deploy regardless.
- **Brand-new page** shows presence by title until its row exists, then resolves to `page_id`.
- **`ChangeBroker` is in-memory** (per process) — correct, since each process has its own
  subscribers + its own listener. No external broker needed.

## Out of scope (this wave)
- Postgres `LISTEN/NOTIFY` is the push transport; no message broker (Kafka/Redis-streams).
- Live updates for hand-edits; Notes/Briefs presence UI (the `changes/stream` is general —
  consumers come later).
- Per-keystroke collaborative editing / multi-user cursors.
- SSE×OIDC auth (tracked separately).
