# Chat surface

The center panel renders an active assistant session's thread.

## Behavior

- Auto-creates a session if none exists for the project.
- Posts user messages via `POST /v1/projects/{id}/threads/{tid}/messages`.
- Streams the assistant message via the push-backed
  `GET …/messages/{mid}/stream` (see below) while it is in `streaming`
  status; the stream closes on completion.
- Each assistant message shows: coverage judgment, latency, cost,
  cited wiki page titles, descent chunk count (when applicable).

## SSE streaming

`GET /v1/projects/{id}/messages/{mid}/stream` emits JSON events of
shape `{event: "token", delta: "..."}` then a final
`{event: "done", status: "complete"}`. The stream is backed by the
Postgres LISTEN/NOTIFY push layer: the assistant turn still writes
`body_md` in chunks as the workflow progresses, and each `body_md`
`UPDATE` fires an `aleph_changes` notification that wakes the stream,
which re-reads `body_md` and emits the new tail. This is not true
per-token streaming — the stream approximates tokens by diffing the
growing `body_md` — but the *trigger* is now a push (near-live deltas),
not a fixed poll. A slow 1s poll fallback runs underneath as a
self-healing safety net, and the turn is bounded by a ~5min wall-clock
deadline.

## Citation hover (Inc 1's renderer)

The chat reuses `WikiBodyMarkdown` from Inc 1, which renders
`[[wikilink]]` chips and `[cN]` markers. Clicking a chip navigates the
Wiki tab to the referenced page. `[cN]` markers (Inc 2 placeholder)
will gain a hover preview in Inc 4 when the renderer can resolve the
backing `Citation` row in real time.

## Budget banner

The `CostBanner` from Inc 0 is unchanged; it polls
`GET /v1/projects/{id}/cost` every 30s and shows green / yellow / red
based on the soft/hard percentages. The chat composer's send button is
disabled when the message's preflight returns `status="budget_blocked"`.

## Forking

Inc 2 ships `POST /v1/projects/{id}/sessions/{sid}/threads/fork` with
`{parent_thread_id, from_ordinal}`. The UI surface for "retry from
here" lands in Inc 4 alongside A2UI; the API contract is stable.
