# Aliases

`Alias(surface_form, canonical_name, canonical_page_id?)`. Extracted at
ingest by the wiki agent's `alias_extraction` node. Used to:

1. Resolve `[[wikilink]]` targets when the surface form doesn't match a
   page title exactly.
2. Normalize concept names across multiple sources that use different
   labels for the same thing.
3. Repair broken links after page creation: `repair_broken_links`
   iterates `WikiLink` rows with null `dst_page_id` and resolves via
   the alias table.

## Sources of aliases

- The wiki agent's `alias_extraction` LLM call.
- Concept extraction's `surface_forms` list — every observed surface
  form becomes an alias for its canonical name.
- Analysts via `POST /v1/projects/{id}/wiki/aliases`.

## Conflict resolution

`(project_id, surface_form)` is the unique key. An upsert updates
`canonical_name` and bumps `confidence` to the max of the existing and
new values. Conflicts are resolved by highest-confidence winner.

## API

- `GET /v1/projects/{id}/wiki/aliases?surface_form=...`
- `POST /v1/projects/{id}/wiki/aliases` — owner/editor only.
- `POST /v1/projects/{id}/wiki/aliases/repair-links` — owner/editor.
  Returns `{repaired: N}`.
