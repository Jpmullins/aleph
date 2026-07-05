# A2UI Surfaces

Each right-panel tab is rendered by a single A2UI Surface component
(`WikiSurface`, `ArtifactsSurface`, `NotesSurface`,
`HypothesesSurface`, `BriefsSurface`). The server composes the
surface from current project state and returns it via
`GET /v1/projects/{id}/surfaces/{tab}`.

The renderer is a small dispatcher (`apps/web/src/a2ui/register.tsx`)
that walks the surface tree and recursively renders each child via the
component registry.

## Honest scope

Surfaces with content types that don't exist yet ship a clear
placeholder rather than a fake one. Inc 4's `ArtifactsSurface` says
"no artifacts yet — Builder lands in Increment 7." This is intentional:
the *surface* is real (the catalog component, the action plumbing, the
push update path), only its *content type* awaits its own increment.

## Refresh

The surface stream `GET /v1/projects/{id}/surfaces/{tab}/stream` is
backed by the Postgres LISTEN/NOTIFY push layer: any project mutation
fires an `aleph_changes` notification that wakes the stream, which
recomputes the surface and emits a diff (structural `updateComponents`
plus per-path `updateDataModel` deltas). A slow 10s poll fallback runs
underneath as a self-healing safety net — wakes are now sub-second on
writes rather than the previous fixed 2.5s recompute poll. Tabs that
self-fetch via client-side `@tanstack/react-query` poll/refetch (e.g.
Briefs at 10s) keep that path.

## Pinned cards

`InteractiveCard.pinned_to` records which surface a saved card lives
in. Cards composed by the assistant in chat (Inc 2's
`attached_cards_jsonb`) can be saved by the analyst to a surface via
`POST /v1/projects/{id}/cards/{id}/pin`. (Pin route lands in a
follow-on; the model column is present.)
