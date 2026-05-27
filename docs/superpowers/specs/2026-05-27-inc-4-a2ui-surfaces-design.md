# Increment 4 — A2UI Aleph Catalog + Interactive Workspace Surfaces

**Parent spec:** `docs/superpowers/specs/2026-05-26-aleph-design.md`
**Depends on:** Inc 0, Inc 1, Inc 2, Inc 3
**Status:** Subsystem design spec.
**Written:** 2026-05-27.

## 4.1 Scope

Increment 4 wires A2UI as the rendering substrate for the **entire right panel** (per top-level §7) and for **inline cards in chat** (per top-level §11). The Aleph A2UI Catalog ships: 5 top-level Surface components, one per right-panel tab, plus inline cards used in chat and embedded inside surfaces.

After Inc 4, the right panel goes from plain React placeholders to A2UI-rendered surfaces. The Wiki tab moves from Inc 1's plain Markdown to a `WikiSurface` (with graph view, faceted navigation, inline cards). The Briefs tab lights up with `SynthesisProposal`s as `ApprovalCard`s — replacing the Inc 3 owner-only API approval with a proper analyst UX.

### In scope

- **A2UI integration:** `a2ui` Python SDK in agents; `@a2ui/react` renderer in the web app; AG-UI transport wiring with CopilotKit
- **Aleph A2UI Catalog JSON Schema** — versioned, validated at the renderer
- **Surface components** (5 total): `WikiSurface`, `ArtifactsSurface`, `NotesSurface`, `HypothesesSurface`, `BriefsSurface`
- **Inline cards** (12 in this catalog version): `ClaimCard`, `SourceCard`, `ChartCard`, `TableCard`, `MapCard`, `GraphCard`, `ApprovalCard`, `FindingCard`, `HypothesisCard`, `NotebookCellCard`, `FormCard`, `DiffCard`
- Models: `InteractiveCard`, `InteractiveCardVersion`, `CardAction`
- Models: `Note`, `NoteSection` (so the NotesSurface has something real to render)
- Card-action routing: every user interaction with a card produces a typed action that routes through a service and lands a ledger event
- Schema validation at both ends: Python SDK validates outbound surface messages; React renderer validates inbound; mismatches logged + rejected
- Catalog versioning + migration policy
- Tests + docs + eval (A2UI surface validation eval)

### Explicitly out of scope

- `Hypothesis` / `HypothesisVersion` / `HypothesisEvidence` (full models + reviewer agents) → Inc 5. The `HypothesesSurface` ships in Inc 4 but renders only a stub view in this increment with a clear "Hypotheses model lands in Increment 5" placement. (Acceptable per top-level §15.1 because we ship the *surface* complete; the *content type* lands in its own increment.)
- Reviewer agents (Mechanical + Editorial) → Inc 5. The `FindingCard` schema exists in the catalog but the source `ReviewFinding` rows aren't produced yet — the card lands as the rendering contract; reviewer agents in Inc 5 produce the rows that populate it.
- `Dataset` / `DatasetVersion` / `Observation` → Inc 6. The `ChartCard` / `TableCard` / `MapCard` / `GraphCard` schemas exist in the catalog but they can't bind to real data until Inc 6. They're tested with fixture data in Inc 4.
- `Artifact` / `ArtifactVersion` → Inc 7. The `ArtifactsSurface` ships in Inc 4 but its content is "no artifacts yet — Builder lands in Inc 7" until then.

This is intentional. Inc 4's scope is the **A2UI rendering plumbing + surface shells**. Inc 5/6/7 plug content into the slots Inc 4 reserves. Inc 4's surfaces are not stubs — they are real A2UI components that render whatever data is available. They look sparse when their content increment hasn't landed yet, which is **honest**, not fake.

### Dependencies

- Inc 0–3 fully
- A2UI upstream: `@a2ui/react` (npm) and `a2ui` (PyPI), tracking latest per §15.6
- CopilotKit (Inc 0 declared) — now wired to the A2UI render path

### Downstream

- Inc 5 produces `ReviewFinding`s and `Hypothesis` rows that flow into existing surfaces; no schema change needed.
- Inc 6 binds `DatasetVersion` to the existing `ChartCard`/`TableCard`/`MapCard`/`GraphCard` schemas.
- Inc 7 produces `Artifact` rows that flow into the `ArtifactsSurface`.

---

## 4.2 Repository changes

```
packages/
└── aleph-a2ui/                         # Aleph's A2UI integration
    └── src/aleph_a2ui/
        ├── __init__.py
        ├── catalog.py                  # JSON Schema generator + Python typed wrappers
        ├── surface.py                  # SurfaceMessage builders
        ├── components/
        │   ├── __init__.py
        │   ├── surfaces.py             # 5 Surface dataclasses
        │   └── cards.py                # 12 inline-card dataclasses
        ├── action_router.py            # CardAction → service dispatch
        └── streaming.py                # incremental A2UI updates over AG-UI

packages/aleph-wiki/src/aleph_wiki/
└── surfaces/                           # wiki-side rendering helpers
    └── wiki_surface_builder.py         # WikiSurface composition

packages/aleph-assistant/src/aleph_assistant/
└── cards.py                            # inline-card attachment to AssistantMessage

packages/aleph-notes/                   # new package for analyst notes
└── src/aleph_notes/
    ├── models.py                       # Note, NoteSection
    └── note_service.py

apps/api/src/aleph_api/routes/
├── surfaces.py                         # GET /surfaces/{tab}; SSE updates
├── cards.py                            # POST card action; GET card detail
├── notes.py                            # CRUD on notes
└── briefs.py                           # GET briefs (lists SynthesisProposals + future ReviewFindings)

apps/web/src/
├── a2ui/                               # registration with @a2ui/react
│   ├── catalog.ts                      # mirror of Python catalog (generated)
│   ├── components/
│   │   ├── WikiSurface.tsx
│   │   ├── ArtifactsSurface.tsx
│   │   ├── NotesSurface.tsx
│   │   ├── HypothesesSurface.tsx
│   │   ├── BriefsSurface.tsx
│   │   ├── ClaimCard.tsx
│   │   ├── SourceCard.tsx
│   │   ├── ChartCard.tsx           # bound to DatasetVersion in Inc 6; renders empty state until then
│   │   ├── TableCard.tsx
│   │   ├── MapCard.tsx
│   │   ├── GraphCard.tsx
│   │   ├── ApprovalCard.tsx
│   │   ├── FindingCard.tsx
│   │   ├── HypothesisCard.tsx
│   │   ├── NotebookCellCard.tsx
│   │   ├── FormCard.tsx
│   │   └── DiffCard.tsx
│   └── register.ts                     # wires components to @a2ui/react renderer
├── pages/                              # the right-panel host
│   └── RightPanel.tsx                  # tab strip + active surface render
└── lib/
    └── a2ui-client.ts                  # AG-UI transport client wrapper
```

---

## 4.3 Aleph A2UI Catalog (the contract)

The catalog is a JSON Schema document at `packages/aleph-a2ui/src/aleph_a2ui/catalog.schema.json`. It is the single source of truth for what A2UI components Aleph supports.

### Catalog shape (top-level)

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://aleph.research/a2ui/catalog/v1.json",
  "title": "Aleph A2UI Catalog",
  "catalogId": "aleph-v1",
  "version": "1.0.0",
  "components": {
    "WikiSurface":         {...},
    "ArtifactsSurface":    {...},
    "NotesSurface":        {...},
    "HypothesesSurface":   {...},
    "BriefsSurface":       {...},
    "ClaimCard":           {...},
    "SourceCard":          {...},
    "ChartCard":           {...},
    "TableCard":           {...},
    "MapCard":             {...},
    "GraphCard":           {...},
    "ApprovalCard":        {...},
    "FindingCard":         {...},
    "HypothesisCard":      {...},
    "NotebookCellCard":    {...},
    "FormCard":            {...},
    "DiffCard":            {...}
  },
  "actions": {
    "approve":         {"params": {"target_id": "uuid", "target_kind": "string"}},
    "reject":          {"params": {"target_id": "uuid", "target_kind": "string", "reason": "string"}},
    "open":            {"params": {"target_id": "uuid", "target_kind": "string"}},
    "navigate_wiki":   {"params": {"page_id": "uuid"}},
    "submit_form":     {"params": {"form_id": "string", "values": "object"}},
    "create_hypothesis": {"params": {...}},
    "edit_note":       {"params": {"section_id": "uuid", "body_md": "string"}},
    "clarify":         {"params": {"agent_run_id": "uuid", "answer": "string"}},
    "mark_handedit":   {"params": {"page_id": "uuid", "section_anchor": "string"}},
    "clear_handedit":  {"params": {"page_id": "uuid", "section_anchor": "string"}}
  }
}
```

### Component schema convention

Every component definition:

```jsonc
{
  "type": "object",
  "properties": {
    "type": {"const": "<ComponentName>"},
    "id": {"type": "string"},
    "props": {
      "type": "object",
      "properties": {/* component-specific */},
      "required": [/* component-specific */],
      "additionalProperties": false
    },
    "data_bindings": {/* JSON pointers into the surface's data_model */},
    "children": {"type": "array", "items": {"$ref": "#/$defs/component_ref"}}
  },
  "required": ["type", "id", "props"],
  "additionalProperties": false
}
```

### Sample component schema — `ApprovalCard`

```jsonc
"ApprovalCard": {
  "type": "object",
  "properties": {
    "type": {"const": "ApprovalCard"},
    "id": {"type": "string"},
    "props": {
      "type": "object",
      "properties": {
        "target_id": {"type": "string", "format": "uuid"},
        "target_kind": {"enum": ["synthesis_proposal", "review_finding", "wiki_revision"]},
        "title": {"type": "string", "maxLength": 200},
        "summary": {"type": "string", "maxLength": 1000},
        "severity": {"enum": ["info", "low", "medium", "high"]},
        "evidence_refs": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "kind": {"enum": ["claim", "source", "chunk", "page"]},
              "id": {"type": "string", "format": "uuid"},
              "label": {"type": "string"}
            }
          }
        },
        "diff_card_id": {"type": ["string", "null"]},
        "approve_action": {"const": "approve"},
        "reject_action": {"const": "reject"},
        "view_diff_action": {"const": "open"}
      },
      "required": ["target_id", "target_kind", "title", "summary", "approve_action", "reject_action"]
    }
  }
}
```

(All 17 components — 5 Surfaces + 12 inline cards — have full schemas in the JSON Schema file. Above is one example.)

### Surface components — high-level shapes

#### `WikiSurface`

Renders the Wiki tab. Composed of:
- A header strip: title, search box, view toggle (`graph` | `list` | `recent`), filters (`page_kind`, `status`, `is_stub`)
- A main panel: the selected page rendered as Markdown with `[[wikilink]]` and `[c12]` extension components — embedded `ClaimCard`/`SourceCard`/`DiffCard` slots
- A side rail: outgoing wikilinks list, claims list with confidence, citations list
- A graph view (when toggled): React Flow graph of the project's wiki built from `WikiLink` rows
- A "filter by source-pages" affordance — replaces the temporary "Sources tab" Inc 1 introduced. All `SourcePage`s are listable here.

Surface props: `current_page_id?`, `view_mode`, `filters`. Reads from `wiki_index`, `wiki_pages`, `wiki_revisions`, `wiki_links`, `wiki_claims`, `citations`, `hand_edit_marks`, `source_pages`.

#### `BriefsSurface`

Renders the Briefs tab. List of action cards waiting on the analyst. Composed of:
- A header strip: filters (`severity`, `target_kind`), badge count
- A list of inline cards — each item is an `ApprovalCard` or `FindingCard` (Inc 5+)
- An inline detail pane: click a card → expanded view with `DiffCard` + evidence panel

Surface props: `filters`. Reads from `synthesis_proposals`, `review_findings` (Inc 5), `approval_requests` (Inc 5), plus any assistant-emitted one-off cards (`InteractiveCard` with `pinned_to="briefs"`).

#### `NotesSurface`

Renders the Notes tab. Analyst's notebook. Composed of:
- A tree of `Note`s with `NoteSection`s
- A markdown editor for the selected section (with `[[wikilink]]` autocomplete and `@source` mentions)
- An inline embed slot for `ChartCard` / `TableCard` / `HypothesisCard`

Surface props: `current_note_id?`, `current_section_id?`. Reads from `notes`, `note_sections`.

#### `HypothesesSurface`

Renders the Hypotheses tab. In Inc 4 the underlying `Hypothesis` model doesn't exist yet (Inc 5). The surface ships with the *contract* in the catalog and renders an honest "no hypotheses yet — create one or wait for Increment 5 features" state.

Surface props: `current_hypothesis_id?`. Reads from `hypotheses` (empty until Inc 5).

#### `ArtifactsSurface`

Renders the Artifacts tab. In Inc 4 the underlying `Artifact` model doesn't exist (Inc 7). The surface ships with the *contract* in the catalog and renders "no artifacts yet — Builder lands in Increment 7."

Surface props: `current_artifact_id?`. Reads from `artifacts` (empty until Inc 7).

### Component-to-server backing map

This map exists in code (`aleph_a2ui.components`) and in docs:

| Component | Server table(s) | Action handlers |
|---|---|---|
| `WikiSurface` | `wiki_*` family | navigate_wiki, mark_handedit, clear_handedit |
| `ArtifactsSurface` | `artifacts` (empty in Inc 4) | open, navigate |
| `NotesSurface` | `notes`, `note_sections` | edit_note, navigate |
| `HypothesesSurface` | `hypotheses` (empty in Inc 4) | open, create_hypothesis (Inc 5) |
| `BriefsSurface` | `synthesis_proposals`, `review_findings` (Inc 5+) | approve, reject, open |
| `ClaimCard` | `wiki_claims`, `citations` | open |
| `SourceCard` | `sources`, `source_pages` | open, navigate_wiki |
| `ChartCard` | `dataset_versions` (Inc 6) | open |
| `TableCard` | `dataset_versions` | open |
| `MapCard` | `dataset_versions` (geo) | open |
| `GraphCard` | `dataset_versions` (nodes+edges) | open |
| `ApprovalCard` | `synthesis_proposals` / `review_findings` (Inc 5) / `wiki_revisions` | approve, reject |
| `FindingCard` | `review_findings` (Inc 5; schema exists Inc 4) | open, approve, reject |
| `HypothesisCard` | `hypotheses` (Inc 5; schema exists Inc 4) | open, edit |
| `NotebookCellCard` | `note_sections` | edit_note |
| `FormCard` | none — transient (clarifier loop) | submit_form, clarify |
| `DiffCard` | `wiki_revisions` (pair) | open |

---

## 4.4 Domain model additions

```python
# packages/aleph-a2ui/src/aleph_a2ui/models.py

class InteractiveCard(CommonColumns, Base):
    """Server-side record of an A2UI surface or inline card proposed by an agent
    or generated server-side. NOT every A2UI component instance corresponds to a
    row — only ones the server needs to persist (e.g. a chart the analyst pinned,
    a one-off table the assistant generated and the analyst saved).
    """
    __tablename__ = "interactive_cards"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    card_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # one of the catalog component types
    catalog_version: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    pinned_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # null = transient; wiki | notes | briefs | hypotheses | artifacts (for pinned cards)
    pinned_target_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # e.g. the wiki_page_id this chart is pinned to

class InteractiveCardVersion(Base):
    __tablename__ = "interactive_card_versions"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    card_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(nullable=False)
    a2ui_payload_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # canonical A2UI surface/component JSON (validates against the catalog schema)
    data_model_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # A2UI's data_model that the components bind into
    parent_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # user | aleph_agent | aiq_agent
    author_id: Mapped[UUID] = mapped_column(nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("card_id", "version_no"),)
# Immutable: same triggers as wiki_revisions

class CardAction(Base):
    """Every analyst interaction with an A2UI card produces one row. Ledgered."""
    __tablename__ = "card_actions"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    card_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # null for transient cards (e.g. clarifier FormCard)
    surface_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # WikiSurface | BriefsSurface | NotesSurface | HypothesesSurface | ArtifactsSurface | inline
    action_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(nullable=True)
    target_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    actor_id: Mapped[UUID] = mapped_column(nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)

# packages/aleph-notes/src/aleph_notes/models.py

class Note(CommonColumns, Base):
    __tablename__ = "notes"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_note_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # for a simple tree

class NoteSection(CommonColumns, Base):
    __tablename__ = "note_sections"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    note_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # may contain [[wikilink]] and `@source:S0042` mentions; renderer resolves them
```

Migration `<timestamp>_inc4_a2ui_notes.py` creates these tables + the catalog-version row.

```python
class CatalogVersion(Base):
    __tablename__ = "catalog_versions"
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    schema_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    introduced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

The Inc 4 migration seeds `version="1.0.0"` with the full schema JSON.

---

## 4.5 A2UI rendering plumbing

### Python SDK side (agents)

```python
# packages/aleph-a2ui/src/aleph_a2ui/surface.py

class SurfaceBuilder:
    """Agent-side helper to compose A2UI surface messages from typed Python."""

    def __init__(self, catalog: AlephCatalog): ...

    def wiki_surface(self, *, project_id: UUID, current_page_id: UUID | None, ...) -> SurfaceMessage:
        """Compose a WikiSurface payload with current state. Validates against catalog."""

    def approval_card(self, *, target_id, target_kind, title, summary, severity, ...) -> ComponentRef:
        ...

# packages/aleph-a2ui/src/aleph_a2ui/streaming.py

class SurfaceStreamer:
    """Incremental updates over the AG-UI transport. Emits A2UI surfaceUpdate
    or updateComponents messages (per spec version)."""

    async def send_create(self, surface_id: str, payload: SurfaceMessage): ...
    async def send_update(self, surface_id: str, component_diff: list[ComponentUpdate]): ...
    async def send_data_update(self, surface_id: str, data_model_diff: dict): ...
    async def send_delete(self, surface_id: str): ...
```

### React renderer side

```typescript
// apps/web/src/a2ui/register.ts
import { Renderer, Catalog } from "@a2ui/react";
import catalogSchema from "./catalog.json"; // generated from the Python side
import * as components from "./components";

const catalog: Catalog = {
  id: "aleph-v1",
  version: "1.0.0",
  schema: catalogSchema,
  components: {
    WikiSurface: components.WikiSurface,
    ArtifactsSurface: components.ArtifactsSurface,
    // ... all 17
  },
};

export const AlephCatalog = catalog;
```

The renderer is mounted in `apps/web/src/pages/RightPanel.tsx`:

```tsx
<A2UIRenderer
  catalog={AlephCatalog}
  transport={agUiTransport}
  surfaceId={`project-${projectId}-${activeTab}`}
  onAction={routeAction}        // -> POST /v1/projects/.../cards/actions
  onError={logSchemaViolation}  // any invalid surface payload logged + visible to owner
/>
```

### Catalog generation pipeline

`packages/aleph-a2ui/src/aleph_a2ui/catalog.py` exposes:

```python
def emit_json_schema() -> dict: ...
def emit_typescript_types() -> str: ...
```

A build step (`scripts/build-a2ui-catalog.sh`) regenerates `apps/web/src/a2ui/catalog.json` and `apps/web/src/a2ui/types.generated.ts` from the Python source of truth. CI fails if the generated files are out of date.

### Schema validation

- **Python (outbound):** every `SurfaceBuilder` method calls `jsonschema.validate(payload, catalog_schema)` before sending. Failures raise `SurfaceInvalid`.
- **React (inbound):** the renderer validates each incoming surface message against the catalog schema (zod or ajv). Invalid messages are logged to the action endpoint as a `schema_violation` event and rendered as an inline error component.

---

## 4.6 Action routing

```python
# packages/aleph-a2ui/src/aleph_a2ui/action_router.py

class ActionRouter:
    """Maps an incoming CardAction to a typed service call. Single chokepoint."""

    async def dispatch(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        action: CardActionInput,  # parsed from POST body
    ) -> CardActionResult:
        ...
```

For each action kind, the router dispatches to a specific service:

| Action | Routes to |
|---|---|
| `approve` (target=synthesis_proposal) | `SynthesisService.approve` (Inc 3) |
| `approve` (target=review_finding) | `ReviewService.approve` (Inc 5) |
| `approve` (target=wiki_revision) | `WikiService.approve_revision` |
| `reject` (any) | corresponding service's reject method, requires `reason` |
| `open` (target=*) | no-op server-side; pure client navigation, but logged as `CardAction` |
| `navigate_wiki` | logged as `CardAction`; client navigates |
| `submit_form` (form_id=clarifier) | `AIQJobService.submit_clarifier_answer` (Inc 3) |
| `submit_form` (form_id=project_create) | `ProjectService.create` (Inc 0) — used when wizard moves to A2UI in Inc 4 |
| `edit_note` | `NoteService.edit_section` |
| `create_hypothesis` | (Inc 5) — no-op in Inc 4 |
| `mark_handedit` / `clear_handedit` | `HandEditMarkService.mark_section` / `clear_section` (Inc 1) |
| `clarify` | `AIQJobService.submit_clarifier_answer` |

Every dispatch writes a `CardAction` row + ledger event in one transaction.

---

## 4.7 HTTP API

All under `/v1/projects/{project_id}/`.

### Surfaces

- `GET /surfaces/{tab}` — returns the current `SurfaceMessage` payload for the given tab (`wiki | artifacts | notes | hypotheses | briefs`); used on tab load
- `GET /surfaces/{tab}/stream` — SSE channel for incremental surface updates (subscribed while the tab is active)
- `GET /surfaces/catalog` — public; returns the catalog schema (cached by client)

### Cards

- `POST /cards/actions` — body `{action_kind, target_id, target_kind, params, surface_kind}`; routes via `ActionRouter`
- `GET /cards/{card_id}` — fetch an `InteractiveCard` and its current version
- `POST /cards/{card_id}/pin` — pin a transient card to a surface
- `DELETE /cards/{card_id}/pin` — unpin

### Notes

- `GET /notes` — list project notes (tree)
- `POST /notes` — create
- `PATCH /notes/{id}` — rename / re-parent
- `DELETE /notes/{id}` — soft delete; ledgered
- `GET /notes/{id}/sections` — ordered sections
- `POST /notes/{id}/sections` — append section
- `PATCH /notes/{id}/sections/{section_id}` — edit body_md (via `edit_note` action)
- `DELETE /notes/{id}/sections/{section_id}` — ledgered

### Briefs

- `GET /briefs` — paginated list of items: `synthesis_proposals` + future review findings + pinned inline cards. Combined feed sorted by `created_at desc`. Filters: `severity`, `target_kind`, `status`.
- `GET /briefs/{item_id}` — detail dispatch (returns the right card type)

---

## 4.8 How agents use A2UI

### Inline cards in chat

When the assistant (Inc 2) composes a turn, the composer may emit not just Markdown but also structured card descriptors. Inc 4 extends `ComposerResponse`:

```python
class ComposerResponse(BaseModel):
    body_md: str
    descent_requests: list[DescentRequest]
    synthesis_requests: list[SynthesisRequest]
    inline_cards: list[InlineCardDescriptor]
    # NEW: each is one of: claim_summary | source_brief | mini_table | approval_handle | finding_brief
```

The assistant agent's `finalize` node (Inc 2) now also:
- Persists card descriptors into `attached_cards_jsonb` on the message
- For each `approval_handle` descriptor that references a synthesis proposal: emit an `ApprovalCard` payload as the inline card

Chat renders cards inline below the message body via the same `<A2UIRenderer>` component. Card actions on inline cards POST to `/v1/projects/.../cards/actions` the same way surface cards do.

### Surface composition

For each tab, a *Surface Builder* runs server-side, takes the current Aleph state, and produces a `SurfaceMessage`:

```python
# packages/aleph-wiki/src/aleph_wiki/surfaces/wiki_surface_builder.py
async def build_wiki_surface(
    *,
    principal: Principal,
    project_id: UUID,
    current_page_id: UUID | None,
    view_mode: str,
    filters: dict,
) -> SurfaceMessage: ...
```

Builders are called on `GET /surfaces/{tab}` and SSE-updated on relevant model changes (e.g. a new wiki revision triggers an update on the WikiSurface for any subscribed client viewing that page).

Builders may also nest A2UI cards inside surfaces — e.g. the WikiSurface for a page that has a chart pinned (Inc 6+) embeds a `ChartCard` directly into the page render area.

---

## 4.9 Chat composer's clarifier loop (Inc 3 → Inc 4 upgrade)

In Inc 3, AIQ's Clarifier surfaced clarifying questions as plain chat text. Inc 4 upgrades this to a `FormCard`:

When AIQ Clarifier emits a clarification, the assistant agent:
1. Builds a `FormCard` payload via `SurfaceBuilder.form_card(form_id="clarifier:{run_id}", fields=[...])`
2. Persists as `InteractiveCard` with `pinned_to="briefs"` AND attached to the chat message inline
3. User submits → `submit_form` action → `ActionRouter` → `AIQJobService.submit_clarifier_answer`
4. AIQ resumes
5. Form card transitions to a "answered" state (rendered as a `DiffCard` showing the question + answer)

---

## 4.10 Tests

### Unit

- `aleph-a2ui/tests/test_catalog_schema.py` — generated JSON Schema validates against draft-2020-12 meta-schema
- `aleph-a2ui/tests/test_surface_builder.py` — each surface builder produces schema-valid payloads
- `aleph-a2ui/tests/test_action_router.py` — each action kind dispatches to the right service; unknown action kind returns 400
- `aleph-a2ui/tests/test_catalog_versioning.py` — adding a component bumps catalog version; renderer rejects messages with mismatched catalog id
- `aleph-notes/tests/test_note_service.py` — note CRUD + section edit + wikilink autocomplete metadata

### Frontend tests

- `apps/web/tests/a2ui/components.test.tsx` — each component renders without errors given a valid props payload (snapshot tests)
- `apps/web/tests/a2ui/schema-validation.test.tsx` — invalid payloads are rejected and render the inline error component, not the malformed UI
- `apps/web/tests/a2ui/action-flow.test.tsx` — clicking Approve on an `ApprovalCard` POSTs the right action; UI optimistically updates and rolls back on server reject

### Integration (`tests/e2e/`)

- `test_wiki_surface_renders.py` — `GET /surfaces/wiki` returns a schema-valid payload; React renderer mounts; page body is visible; `[[wikilinks]]` and `[c12]` markers render as components
- `test_briefs_approval.py` — Inc 3 leaves a `SynthesisProposal pending`; open Briefs surface; an `ApprovalCard` is visible; click Approve; verify: page status → approved, `ApprovalDecision` row written, `CardAction` row written, ledger event, Briefs surface re-renders without that card
- `test_notes_edit.py` — Create note + section in NotesSurface; edit body via `edit_note` action; persisted; rendered Markdown with wikilink hover
- `test_inline_card_in_chat.py` — chat turn produces a synthesis-needed coverage → assistant emits an inline `FormCard` for a `SynthesizeButton`-style affordance; submitting routes through ActionRouter
- `test_catalog_validation_blocks_bad_message.py` — Force-inject a malformed surface payload; renderer rejects with logged schema_violation event
- `test_clarifier_form_card.py` — AIQ Clarifier surfaces a question → FormCard rendered in Briefs + chat; submit answer → AIQ resumes
- `test_no_a2ui_execution.py` — Negative test: verify no `eval`, no dynamic component import, no agent-emitted JavaScript path exists; all rendering goes through the registered catalog
- `test_permission_leakage_a2ui.py` — Member of Project X cannot fetch `/surfaces/{tab}` of Project Y. 404.

### Eval (`packages/aleph-evals/datasets/inc4_a2ui/`)

- `surface_payload_validity.jsonl` — sample states drive builders; verify all emit schema-valid payloads
- `action_routing_correctness.jsonl` — `{action_kind, target_kind, expected_service}` — verify dispatcher maps right

---

## 4.11 Documentation

- `docs/a2ui/overview.md` — what A2UI is in Aleph, how it differs from chat-only generative UI
- `docs/a2ui/catalog.md` — full catalog component list with screenshots
- `docs/a2ui/surfaces.md` — each surface's responsibility, builder, server backing, action set
- `docs/a2ui/inline-cards.md` — chat-inline card patterns
- `docs/a2ui/action-routing.md` — the action router contract
- `docs/a2ui/catalog-versioning.md` — how to add a component, version bump policy, migration of stored card versions
- `docs/security/a2ui-sandbox.md` — declarative-only contract; no agent JS execution; renderer schema validation as a security control
- `docs/ui/right-panel.md` — tab strip, surface mount, SSE update lifecycle
- `docs/domain/notes.md`
- `docs/implementation-log.md` — Inc 4 entry

---

## 4.12 Acceptance criteria

1. **Catalog defined and versioned.** `catalog.schema.json` exists, validates against draft-2020-12, version `1.0.0` seeded into `catalog_versions` table.
2. **All 17 components render.** Each component has a React implementation that renders without errors against valid props.
3. **WikiSurface live.** Opening the Wiki tab renders the WikiSurface for the current project. Page navigation, wikilink chips, citation hovers, claim list — all work. The Inc 1 plain-Markdown rendering is replaced.
4. **BriefsSurface live.** Inc 3's SynthesisProposals appear as ApprovalCards. Approve → page status flips. Reject → reason captured, page soft-deleted.
5. **NotesSurface live.** Create note, add sections, edit body, render with `[[wikilinks]]` resolved.
6. **HypothesesSurface and ArtifactsSurface live as shells.** They render the catalog component with empty data; users see honest empty-state messaging. (Inc 5 / Inc 7 populate.)
7. **Inline cards in chat.** Assistant messages with `attached_cards_jsonb` render the cards below the body via the same renderer. Card actions route.
8. **Clarifier FormCard.** AIQ clarifier produces a FormCard (not raw text). Submitting routes through ActionRouter to AIQ.
9. **Schema violations rejected.** Malformed A2UI payloads (server- or agent-emitted) are rejected by the renderer with a clear inline error and a schema_violation event logged.
10. **CardActions ledgered.** Every card interaction writes a `CardAction` row + ledger event.
11. **No agent code execution.** Static + runtime tests confirm no `eval`, no dynamic import, no JS strings from agents.
12. **Catalog generation reproducible.** `scripts/build-a2ui-catalog.sh` regenerates `apps/web/src/a2ui/catalog.json` and `types.generated.ts` byte-identically; CI fails if out of date.
13. **Permission leakage zero.** Tab fetches and card actions on other projects return 404.
14. **Eval gates pass.** Both surface validity and action routing eval datasets pass under both profiles.
15. **Docs complete.** All Inc 4 docs exist.
16. **Implementation log written.**

---

## 4.13 Handoff to Increment 5

Inc 5 brings:
- `Hypothesis` / `HypothesisVersion` / `HypothesisEvidence` (the data behind `HypothesisCard` and `HypothesesSurface`)
- `ReviewRun` / `ReviewFinding` (data behind `FindingCard`)
- `MechanicalReviewer` + `EditorialReviewer` agents
- `ApprovalRequest` wraps the existing `ApprovalDecision` (Inc 3) for richer workflow
- `AgentMemory`
- Full rejection-feedback wiring from the reviewer's UI

Inc 5 reuses every A2UI catalog component from Inc 4; no catalog schema changes required (it's at v1.0.0 throughout). If Inc 5 needs a new component, it's a v1.1.0 bump per the versioning doc.

See `docs/superpowers/specs/2026-05-27-inc-5-reviewers-hypotheses-design.md`.
