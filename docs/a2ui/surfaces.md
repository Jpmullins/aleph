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
SSE update path), only its *content type* awaits its own increment.

## Refresh

Inc 4 surfaces are refreshed by client-side `@tanstack/react-query`
poll/refetch (10s for Briefs). The SSE channel from Inc 2 wires up
true server-push in a follow-on commit.

## Pinned cards

`InteractiveCard.pinned_to` records which surface a saved card lives
in. Cards composed by the assistant in chat (Inc 2's
`attached_cards_jsonb`) can be saved by the analyst to a surface via
`POST /v1/projects/{id}/cards/{id}/pin`. (Pin route lands in a
follow-on; the model column is present.)
