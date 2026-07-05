# A2UI Catalog v1.0.0

The contract between Aleph's Python agent SDK and the `@a2ui/react`
renderer in the web app. The canonical source is the JSON Schema
exposed at `GET /v1/a2ui/catalog` and the Python module
`aleph_a2ui.catalog`.

## Components (17)

### Surfaces (5)

| Type | Tab | Server backing | Action handlers |
|---|---|---|---|
| `WikiSurface` | Wiki | wiki_* tables | navigate_wiki, mark_handedit, clear_handedit |
| `ArtifactsSurface` | Artifacts | artifacts (Inc 7) | open |
| `NotesSurface` | Notes | notes, note_sections | edit_note, navigate |
| `HypothesesSurface` | Hypotheses | hypotheses (Inc 5) | create_hypothesis, open |
| `BriefsSurface` | Briefs | synthesis_proposals, review_findings (Inc 5) | approve, reject, open |

### Inline cards (12)

| Type | Server backing | Action handlers |
|---|---|---|
| `ClaimCard` | wiki_claims, citations | open |
| `SourceCard` | sources, source_pages | open, navigate_wiki |
| `ChartCard` | dataset_versions (Inc 6) | open |
| `TableCard` | dataset_versions (Inc 6) | open |
| `MapCard` | dataset_versions (Inc 6, geo) | open |
| `GraphCard` | dataset_versions (Inc 6, nodes+edges) | open |
| `ApprovalCard` | synthesis_proposals / review_findings / wiki_revisions | approve, reject |
| `FindingCard` | review_findings (Inc 5) | open, approve, reject |
| `HypothesisCard` | hypotheses (Inc 5) | open |
| `NotebookCellCard` | note_sections | edit_note |
| `FormCard` | none — transient (clarifier) | submit_form |
| `DiffCard` | wiki_revisions (pair) | open |

## Validation

Outbound (Python → renderer): `aleph_a2ui.catalog.validate_surface`
recurses through the component tree and validates every node against
its component schema.

Inbound (renderer → Python): the server's `cards.dispatch_card_action`
route validates the action params against `CATALOG["actions"][kind].params`
JSON Schema before invoking the handler.

## Versioning

- `CATALOG_VERSION = "1.0.0"`.
- Adding a component or action is a minor bump.
- Changing an existing schema in a backwards-incompatible way is a
  major bump and requires every persisted `InteractiveCardVersion`
  row's `catalog_version` to be migrated.

## Adding a new component

1. Add the schema entry to `aleph_a2ui.catalog._COMPONENTS`.
2. Add a typed builder in `aleph_a2ui.components.cards` or `.surfaces`.
3. Add the React renderer in `apps/web/src/a2ui/components/`.
4. Register it in `apps/web/src/a2ui/register.tsx`.
5. Bump `CATALOG_VERSION` and update this doc.
