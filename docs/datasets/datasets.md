# Datasets

Tabular, geo, and graph datasets with immutable version snapshots.
Cards bind to a specific `DatasetVersion`; refreshes don't silently
move charts under the analyst's feet.

## Entities

| Table | Notes |
|---|---|
| `datasets` | `dataset_kind ∈ tabular \| geo \| graph`. `short_id` (D0001) for `[[Dataset:D0001]]` wikilinks. |
| `dataset_versions` | **Immutable** (Postgres triggers). Per-version `column_schema`. Inline rows ≤ 1000 OR ≤ 100 KB; larger versions write parquet to MinIO. |
| `observations` | One row per dataset row when `rows_inline=true`. Out-of-line rows live in `parquet_uri`. |

## Schema inference

`aleph_datasets.schema_inference.infer_column_schema(rows)` walks the
rows and produces `[{name, type}]` with type promotion
`null → int → float`, `bool → int → float`, anything heterogeneous →
`string`. GeoJSON-shaped values get type `geometry`.

## API

```
GET    /v1/projects/{id}/datasets
POST   /v1/projects/{id}/datasets
GET    /v1/projects/{id}/datasets/{id}
POST   /v1/projects/{id}/datasets/{id}/versions
GET    /v1/projects/{id}/datasets/{id}/versions
GET    /v1/projects/{id}/dataset-versions/{id}/observations
POST   /v1/projects/{id}/dataset-versions/{id}/chart-spec
```

`chart-spec` compiles a Vega-Lite v6 spec from the version + axis
hints. Inline-row versions inline their data; parquet versions return
a placeholder data ref the client resolves via streaming.

## Cards

The Inc 4 catalog defined the schemas. Inc 6 lights them up:

- **`ChartCard`** — bound to a `DatasetVersion` + Vega-Lite spec.
- **`TableCard`** — bound to a `DatasetVersion` for sortable/filterable display.
- **`MapCard`** — geo-typed `DatasetVersion`; MapLibre GL.
- **`GraphCard`** — graph-typed `DatasetVersion`; React Flow.

## Pinning

A card binds to one `DatasetVersion` and never moves. To update the
view, commit a new `DatasetVersion`, then pin a new card to it (or
update the card's `dataset_version_id`). The audit trail makes the
"chart used to say X" inquiry trivial.

## artificialanalysis.ai

The first `dataset_rows`-output connector. Returns rows shaped
`{model, metric, value, date}`. Calls into the same connector
framework the document connectors use; the `output_kind` switch routes
the result through the dataset import path instead of the source
ingest path.
