# A2UI ActionRouter

`POST /v1/projects/{id}/cards/actions` is the single dispatch
chokepoint for every analyst interaction with an A2UI card.

## Body

```json
{
  "surface_kind": "BriefsSurface",
  "action_kind": "approve",
  "card_id": "...",        // optional — null for transient cards
  "target_id": "...",
  "target_kind": "synthesis_proposal",
  "params": { "target_id": "...", "target_kind": "synthesis_proposal" }
}
```

## Pipeline

1. **Validate** params against `CATALOG["actions"][kind].params` JSON
   Schema. Bad payload → `validation_failed` problem detail.
2. **Resolve** the handler from the registry. Unknown action → 422.
3. **Open OTEL span** `a2ui.action` tagged with project/surface/action.
4. **Run handler** inside the dispatching session. Handler writes its
   ledger event(s) and returns a dict suitable for the renderer.
5. **Append `a2ui.action.<kind>` ledger event** + insert a `CardAction`
   row pointing at the same `ledger_event_id`.

## Built-in handlers (Inc 4)

- `approve` / `reject` (synthesis proposals — extended in Inc 5 for
  review findings)
- `open` — returns navigate intent for the renderer
- `navigate_wiki` — returns target page_id
- `submit_form` — echoes form values for transient capture
- `create_hypothesis` — placeholder until Inc 5
- `edit_note` — updates a NoteSection
- `clarify` — placeholder; full AIQ clarifier loop wires in Inc 3+
- `mark_handedit` / `clear_handedit` — calls the wiki hand-edit
  service

## Adding a handler

```python
def build_action_router() -> ActionRouter:
    r = ActionRouter()
    r.register("approve", _approve)
    # ...
    return r
```

The function is set on `app.state.action_router` at lifespan startup;
the `/cards/actions` route reads from there.
