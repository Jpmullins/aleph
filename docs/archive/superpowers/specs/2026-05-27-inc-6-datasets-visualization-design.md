# Increment 6 — Datasets + Visualization Cards

**Parent spec:** `docs/superpowers/specs/2026-05-26-aleph-design.md`
**Depends on:** Inc 0–5
**Status:** Subsystem design spec.
**Written:** 2026-05-27.

## 6.1 Scope

Increment 6 brings the structured-data path online. `Dataset` / `DatasetVersion` / `Observation` models land, the `ChartCard` / `TableCard` / `MapCard` / `GraphCard` A2UI components (catalog-defined in Inc 4) bind to real data, the artificialanalysis.ai connector's `dataset_rows` path ships (deferred from Inc 3), and analysts can embed visualizations inside Wiki/Notes/Briefs surfaces.

After Inc 6, a wiki page about an AI benchmark question can render an inline chart bound to an immutable snapshot of artificialanalysis.ai data — and that snapshot won't move under the analyst's feet.

### In scope

- `Dataset`, `DatasetVersion` (immutable), `Observation`
- Dataset import service for `dataset_rows`-output connectors
- artificialanalysis.ai connector full implementation
- ChartCard (Vega-Lite) bound to DatasetVersion via JSON Pointer data_model bindings
- TableCard (sortable, filterable, paginated)
- MapCard (MapLibre GL) — for geo-typed DatasetVersions
- GraphCard (React Flow) — for nodes+edges-typed DatasetVersions
- Dataset editing UI (inline value correction with ledgered changes)
- Dataset version pinning: a card binds to a *specific* `DatasetVersion`; refreshes don't silently move charts
- Card-from-chat: assistant can propose a `ChartCard` inline; analyst pins to a surface
- Tests + docs + eval

### Out of scope

- Builder agent + artifact export → Inc 7
- Eval suite expansion → Inc 8
- New connectors beyond artificialanalysis.ai (others land in their own future increment per top-level §16.2)

### Dependencies

- Inc 0–5 fully
- Inc 3 connector framework + nat plugin pattern
- Inc 4 A2UI catalog (Chart/Table/Map/Graph schemas already defined)

### Downstream

- Inc 7 Builder embeds chart/map/graph cards into exported reports (uses `RenderedAsset` to snapshot PNG/SVG)
- Inc 8 eval suite gains dataset-related metrics

---

## 6.2 Repository changes

```
packages/
└── aleph-datasets/                     # new package
    └── src/aleph_datasets/
        ├── __init__.py
        ├── models.py                   # Dataset, DatasetVersion, Observation
        ├── dataset_service.py          # CRUD + version commit
        ├── import_service.py           # generic dataset_rows ingest
        ├── schema_inference.py         # infer column types from rows
        ├── vega_compile.py             # ChartCard spec compile (Vega-Lite)
        ├── geo.py                      # MapCard helpers (GeoJSON normalization)
        └── graph.py                    # GraphCard helpers (nodes+edges normalization)

packages/aleph-connectors/src/aleph_connectors/
└── artificialanalysis/
    ├── __init__.py
    ├── register.py
    ├── api_client.py
    ├── schema.py                       # model × metric × value × date row shape
    └── tests/

apps/api/src/aleph_api/routes/
├── datasets.py                         # CRUD + versions + observations + search
├── chart_cards.py                      # CRUD; vega-lite spec validation
├── map_cards.py
└── graph_cards.py

apps/web/src/
└── a2ui/components/
    ├── ChartCard.tsx                   # full impl (was placeholder in Inc 4)
    ├── TableCard.tsx                   # full impl
    ├── MapCard.tsx                     # full impl (MapLibre GL JS)
    └── GraphCard.tsx                   # full impl (React Flow)
```

Dependencies (verify latest at install):

- Python: `pandas` (for dataset ops), `pyarrow` (parquet snapshots), `pydantic` (already), `shapely` (geo validation)
- JS: `vega-lite`, `vega`, `react-vega`, `maplibre-gl`, `@types/maplibre-gl`, `@xyflow/react` (React Flow), `topojson-client`

---

## 6.3 Domain model

```python
# packages/aleph-datasets/src/aleph_datasets/models.py

class Dataset(CommonColumns, Base):
    __tablename__ = "datasets"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dataset_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # tabular | geo | graph
    source_connector_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # which connector produced it; null = user-created (manual import)
    short_id: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    # D0001 — referenced as [[Dataset:D0001]] in wiki/notes
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    column_schema_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # canonical column definitions: [{name, type, unit?, description?}]; rows must conform

class DatasetVersion(Base):
    """Immutable snapshot. A card binds to ONE version forever."""
    __tablename__ = "dataset_versions"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    dataset_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(nullable=False)
    row_count: Mapped[int] = mapped_column(nullable=False)
    column_schema_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False)
    # schema at the moment of snapshot
    parquet_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # s3://bucket/projects/{project_id}/datasets/{dataset_id}/{version_no}.parquet
    # null for very small datasets stored inline in observations table
    rows_inline: Mapped[bool] = mapped_column(nullable=False, default=False)
    # if true, rows live in the observations table; else in parquet
    data_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    diff_summary_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {added: N, removed: M, modified: K}
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_id: Mapped[UUID] = mapped_column(nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("dataset_id", "version_no"),)
# Immutable triggers (same shape as wiki_revisions)

class Observation(Base):
    """One row of a dataset. For 'rows_inline' DatasetVersions only.
    Larger versions live in parquet."""
    __tablename__ = "observations"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    dataset_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    dataset_version_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    payload_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # values keyed by column name; types per the column_schema_jsonb at version time
    source_refs_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # provenance: which Source(s) backed this row; for connector-imported rows,
    # the row is tied back to the SourceVersion that fetched it
```

### Dataset shape policies

- **`tabular`** — rectangular rows. `column_schema_jsonb` describes typed columns. Used by `TableCard` and `ChartCard`.
- **`geo`** — rows include a `geometry` GeoJSON column. Validated via `shapely`. Used by `MapCard`. Other columns are attributes for styling/tooltips.
- **`graph`** — two logical sub-collections per version: `nodes` and `edges`. Stored as two parquet sub-files or two row-kinds in `observations`. Used by `GraphCard`.

`Observation.payload_jsonb` keys exactly match `column_schema_jsonb` names. Schema mismatches at insert time raise.

### Inline vs parquet threshold

- ≤ 1000 rows OR ≤ 100 KB serialized: `rows_inline=true`, stored in `observations`. Easy to query for editing/viewing.
- Larger: parquet at `parquet_uri`, accessed via the dataset service which streams from MinIO/S3.

The threshold is configurable per project.

### Migration

`<timestamp>_inc6_datasets.py` creates `datasets`, `dataset_versions`, `observations`. Triggers for `dataset_versions` immutability. Seeds nothing.

---

## 6.4 Dataset service

```python
# packages/aleph-datasets/src/aleph_datasets/dataset_service.py

class DatasetService:
    async def create(
        self, *, principal, project_id, name, description, dataset_kind, column_schema,
        source_connector_kind=None,
    ) -> Dataset: ...

    async def commit_version(
        self,
        *,
        principal,
        project_id,
        dataset_id,
        rows: Iterable[dict] | None = None,
        parquet_bytes: bytes | None = None,
        commit_message: str = "",
        diff_summary: dict | None = None,
    ) -> DatasetVersion:
        """Atomic: in one transaction insert DatasetVersion + (Observations OR parquet write).
        Compute data_sha256 over canonical JSON of rows or parquet bytes.
        If data_sha256 matches the current version's hash: no-op, return current.
        Update dataset.current_version_id.
        Ledger event 'dataset.version.commit'.
        """

    async def get_version_rows(
        self, project_id, dataset_version_id, *, limit, offset
    ) -> list[dict]:
        """Reads from observations or streams from parquet."""

    async def diff(self, project_id, from_version_id, to_version_id) -> DatasetDiff: ...

    async def edit_cell(
        self, *, principal, project_id, dataset_version_id, ordinal, column, new_value,
        rationale: str,
    ) -> DatasetVersion:
        """A cell edit creates a NEW DatasetVersion (immutable; we never mutate observations).
        Ledgered with rationale. Used by inline-edit UI."""
```

Cell editing is implemented as: copy version → apply edit → commit new version with `diff_summary={"modified": 1}` + commit message containing the rationale. The wiki/notes/briefs cards bound to the OLD version don't move; user must explicitly rebind to the new version.

---

## 6.5 artificialanalysis.ai connector

The first `dataset_rows`-output connector. Lifts the framework's `output_kind="dataset_rows"` path from Inc 3.

### What it does

- `search(query)` — query AA API for benchmark families matching the query (e.g. "MMLU", "reasoning benchmarks")
- `fetch(result)` — returns raw JSON of benchmark runs for the selected model/metric/timeframe
- `normalize(payload)` — transforms into a flat row shape: `{model, metric, value, timestamp, model_metadata, metric_metadata}` and emits as `DatasetRows`, not `NormalizedDocument`

### Wiring

```python
# packages/aleph-connectors/src/aleph_connectors/artificialanalysis/register.py
class AAConfig(FunctionBaseConfig, name="artificialanalysis"):
    max_results: int = 20
    timeframe_days: int = 90

@register_function(config_type=AAConfig)
async def artificialanalysis(tool_config, builder):
    async def _run(query: str) -> DatasetRowsResult:
        credential = await callback_client.get_credential(connector_kind="artificialanalysis", ...)
        client = AAClient(api_key=credential)
        runs = await client.fetch(query, timeframe_days=tool_config.timeframe_days)
        rows = normalize(runs)  # to flat shape
        # Persist via callback (creates a Dataset + new DatasetVersion)
        result = await callback_client.persist_dataset_rows(
            connector_kind="artificialanalysis",
            dataset_name=f"AA: {query}",
            dataset_kind="tabular",
            column_schema=AA_COLUMN_SCHEMA,
            rows=rows,
        )
        return result  # contains dataset_id + version_id
    return _run
```

### Callback endpoint

A new internal endpoint extends Inc 3's callback contract:

- `POST /internal/v1/aiq/dataset_rows` — body `{connector_kind, dataset_name, dataset_kind, column_schema, rows}`. Creates or finds the dataset, commits a new version, returns `{dataset_id, dataset_version_id}`.

This is the analog of Inc 3's `/internal/v1/aiq/sources` for the dataset path.

---

## 6.6 A2UI cards: full implementations

### `ChartCard`

```typescript
// apps/web/src/a2ui/components/ChartCard.tsx
type ChartCardProps = {
  dataset_version_id: string;     // immutable binding
  spec: VegaLiteSpec;             // the chart spec (no data inline; data is fetched)
  title?: string;
  subtitle?: string;
  caption?: string;
  // Data binding: spec uses {"data": {"name": "rows"}} which is resolved
  // server-side and streamed via /v1/datasets/.../observations
};
```

Server-side: `chart_card_service.compile_spec(card)` validates the Vega-Lite spec, fetches the rows, embeds them inline for rendering. Renderer mounts via `react-vega`.

Cards are stored as `InteractiveCard` rows with `card_kind="ChartCard"`; spec lives in `InteractiveCardVersion.a2ui_payload_jsonb.props.spec`.

### `TableCard`

```typescript
type TableCardProps = {
  dataset_version_id: string;
  columns: ColumnSpec[];          // subset/order; "*" expands to schema
  sort?: {column: string, direction: "asc"|"desc"};
  filters?: FilterClause[];
  page_size?: number;
};
```

Renderer paginates via server (`GET /datasets/{id}/versions/{vid}/observations?limit&offset&sort&filter`). Inline edit affordance for cells (Inc 6 supports edit only when `dataset_kind="tabular"` and analyst has editor+ role); edits call `DatasetService.edit_cell` which creates a new version.

### `MapCard`

```typescript
type MapCardProps = {
  dataset_version_id: string;     // must be dataset_kind="geo"
  layer_specs: MapLayerSpec[];    // styling per geometry type
  initial_viewport: {center: [number, number], zoom: number};
  basemap?: "carto-positron" | "osm" | "satellite";
  // tooltips driven by data_bindings into the geometry column
};
```

Renderer mounts MapLibre GL. Geometries fetched via `GET /datasets/{id}/versions/{vid}/geojson` (returns a FeatureCollection). Performance: for large datasets, server emits TopoJSON-compressed; client decodes.

### `GraphCard`

```typescript
type GraphCardProps = {
  dataset_version_id: string;     // must be dataset_kind="graph"
  layout?: "force" | "dagre" | "elk" | "manual";
  node_style_spec?: NodeStyleSpec;
  edge_style_spec?: EdgeStyleSpec;
};
```

Renderer mounts React Flow. Nodes + edges fetched via `GET /datasets/{id}/versions/{vid}/graph` (returns `{nodes, edges}`).

---

## 6.7 Embedding in surfaces

Cards (Inc 4 plumbing) can now be **pinned to a surface** with the right card_kind:

- A `ChartCard` pinned to `wiki` with `pinned_target_id=<page_id>` renders inline on that wiki page (just below the section anchor the analyst pinned it to; anchor stored in `InteractiveCard` metadata)
- A `TableCard` pinned to a `NoteSection` renders inside that note
- A `MapCard` pinned to `briefs` renders inside the briefs detail pane for a specific finding (e.g. geo evidence)

The `WikiSurface` / `NotesSurface` / `BriefsSurface` builders (Inc 4) now query for pinned cards by target and include them in the rendered surface payload.

---

## 6.8 HTTP API

All under `/v1/projects/{project_id}/`.

### Datasets

- `POST /datasets` — body `{name, description, dataset_kind, column_schema, source_connector_kind?}`
- `GET /datasets` — list; filter by kind
- `GET /datasets/{id}` — detail with latest version summary
- `PATCH /datasets/{id}` — name/description (schema changes require a new version, not a patch)
- `DELETE /datasets/{id}` — soft delete; cascades to versions+observations; ledgered

### Dataset versions

- `POST /datasets/{id}/versions` — owner/editor; body `{rows[] | parquet_upload, commit_message}`; creates new version
- `GET /datasets/{id}/versions` — list
- `GET /datasets/{id}/versions/{vid}` — version detail
- `GET /datasets/{id}/versions/{vid}/observations` — paginated rows (for inline datasets) or signed parquet URL (for large)
- `GET /datasets/{id}/versions/{vid}/geojson` — for geo datasets
- `GET /datasets/{id}/versions/{vid}/graph` — for graph datasets
- `GET /datasets/{id}/versions/{vid}/diff?from={prev_vid}` — diff summary
- `PUT /datasets/{id}/versions/{vid}/observations/{ordinal}/cell` — owner/editor; body `{column, new_value, rationale}`; creates a new version

### Cards

- `POST /chart-cards` — body `{dataset_version_id, spec, title?, subtitle?, caption?}`; validates spec; persists as InteractiveCard
- `POST /chart-cards/{id}/rebind` — body `{new_dataset_version_id}`; creates a new InteractiveCardVersion
- Similar shapes for `/table-cards`, `/map-cards`, `/graph-cards`

---

## 6.9 Assistant integration (chat ↔ datasets)

The assistant composer (Inc 2) gets a new inline-card descriptor type: `dataset_card_proposal`. When answering a question that touches structured data the wiki cites, the composer may emit:

```json
{
  "type": "dataset_card_proposal",
  "dataset_id": "...",
  "preferred_kind": "ChartCard",
  "vega_lite_spec": {...},
  "rationale": "Three benchmark scores across time; line chart helps comparison."
}
```

This renders inline as a draft `ChartCard` in the chat. The analyst can click "Pin to wiki page [[Region X]]" or "Pin to Note: my-working-notes" or "Reject" — pinning creates the persistent `InteractiveCard` via the API.

The assistant cannot create persistent cards unilaterally — analyst pin is required, ledgered as a `CardAction(action_kind="pin_card")`.

---

## 6.10 Tests

### Unit

- `aleph-datasets/tests/test_dataset_service.py` — create, commit_version (idempotent on hash match), edit_cell creates new version
- `aleph-datasets/tests/test_schema_inference.py` — infer types from sample rows; reject inconsistent rows
- `aleph-datasets/tests/test_vega_compile.py` — validate Vega-Lite specs; reject unsafe specs (no inline JS expressions)
- `aleph-datasets/tests/test_geo.py` — GeoJSON validation; TopoJSON encode
- `aleph-datasets/tests/test_graph.py` — nodes+edges normalization; dedupe edges
- `aleph-connectors/artificialanalysis/tests/test_normalize.py` — AA API mock → expected row shape

### Integration (`tests/e2e/`)

- `test_artificialanalysis_to_chart.py` — Invoke AA connector via `/synthesize` (Inc 3 flow) → Dataset + version created → ChartCard bound → renders in WikiSurface
- `test_dataset_version_immutability.py` — Attempt UPDATE/DELETE on dataset_versions → raises
- `test_card_pins_to_specific_version.py` — Commit dataset v1 → bind ChartCard to v1 → commit v2 → card still shows v1 data; rebind → moves to v2; ledgered
- `test_cell_edit_creates_version.py` — Edit one cell → new version with diff_summary={"modified": 1}; rationale captured in commit_message
- `test_map_card_geo.py` — Create geo dataset → MapCard renders with correct features
- `test_graph_card.py` — Create graph dataset → GraphCard renders nodes+edges
- `test_card_in_wiki_surface.py` — Pin ChartCard to a wiki page → WikiSurface renders inline chart with correct binding
- `test_assistant_proposes_chart.py` — Question implying tabular comparison → assistant emits dataset_card_proposal → analyst pins → InteractiveCard persisted, CardAction ledgered
- `test_dataset_permission_leakage.py` — project isolation

### Eval (`packages/aleph-evals/datasets/inc6_visualization/`)

- `chart_spec_validity.jsonl` — sample dataset + intent → expected Vega-Lite spec; gate: schema-valid and renders without error
- `dataset_diff_correctness.jsonl` — known dataset pairs → expected diff_summary
- `cell_edit_versioning.jsonl` — verify every cell-edit op produces exactly one new version; previous bindings unchanged

---

## 6.11 Documentation

- `docs/domain/datasets.md` — Dataset/Version/Observation model + inline-vs-parquet policy
- `docs/domain/provenance-data-snapshots.md` — why immutable versions matter for chart-card stability
- `docs/a2ui/visualization-cards.md` — Chart/Table/Map/Graph card props, server backing, refresh semantics
- `docs/connectors/artificialanalysis.md` — connector behavior, rate limits, schema
- `docs/ui/inline-cards-in-surfaces.md` — pin/rebind UX
- `docs/implementation-log.md` — Inc 6 entry

---

## 6.12 Acceptance criteria

1. **AA connector ships.** `/synthesize` flow that touches AA produces a Dataset + DatasetVersion + ChartCard with rows from real AA API (or mocked in CI).
2. **Cards bind to immutable versions.** Pinning a ChartCard to v1 and committing v2 leaves the card showing v1 data. Rebind action moves it.
3. **Versions immutable.** UPDATE/DELETE on dataset_versions raises.
4. **Cell edit = new version.** Single-cell edit produces a new DatasetVersion with diff_summary; old version preserved.
5. **All 4 cards render.** Chart, Table, Map, Graph all render in WikiSurface, NotesSurface, and BriefsSurface contexts.
6. **Assistant can propose.** Chat-flow propose-pin-persist works; CardActions ledgered.
7. **Schema validation.** Invalid Vega-Lite spec rejected at submit; invalid GeoJSON rejected.
8. **Permission leakage zero.**
9. **Eval gates pass.**
10. **Docs complete.**
11. **No placeholders.**
12. **Implementation log written.**

---

## 6.13 Handoff to Increment 7

Inc 7 ships the Builder agent + Artifact export. Inc 7 reuses:
- DatasetVersion (charts in exports snapshot the same immutable version)
- RenderedAsset model (Inc 7 creates it for PNG/SVG snapshots of cards)
- WikiPage / WikiRevision (Builder composes from approved revisions)

No schema changes to Inc 6 entities anticipated.

See `docs/superpowers/specs/2026-05-27-inc-7-builder-artifacts-design.md`.
