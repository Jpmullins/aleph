# Chat surface

The center panel renders an active assistant session's thread.

## Behavior

- Auto-creates a session if none exists for the project.
- Posts user messages via `POST /v1/projects/{id}/threads/{tid}/messages`.
- Polls `GET …/messages` while the assistant message is in `streaming`
  status; switches to no-polling on completion.
- Each assistant message shows: coverage judgment, latency, cost,
  cited wiki page titles, descent chunk count (when applicable).

## SSE streaming

`GET /v1/projects/{id}/messages/{mid}/stream` emits JSON events of
shape `{event: "token", delta: "..."}` then a final
`{event: "done", status: "complete"}`. Inc 2 backs streaming with
periodic body polling — the assistant turn writes `body_md` in chunks
as the LangGraph workflow progresses; Inc 4 swaps in true composer
token streaming when the A2UI integration replaces the polling
mechanism.

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
