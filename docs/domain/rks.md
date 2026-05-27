# Raw Knowledge Store (RKS)

The RKS is the upstream of the wiki: ingested source material, normalized
text, parser outputs, and intra-source chunk embeddings. The wiki agent
reads from the RKS; the assistant only reaches into the RKS via
**intra-source descent** (and never as the primary retrieval path).

## Entities

| Table | Purpose |
|---|---|
| `connectors` | Global registry of typed source-kind plugins. Inc 1 seeds `upload`. |
| `connector_bindings` | Per-project allowlist + config. |
| `sources` | One row per ingested source. `short_id` (`S0042`) is the human handle used in `[[Source:S0042]]` wikilinks. |
| `source_versions` | Each refetch/reupload is a new version. Old versions preserved. |
| `source_assets` | Raw bytes in MinIO/S3; this row carries pointer + sha256. |
| `normalized_documents` | Canonicalized markdown + structure outline + quality flags. |
| `document_chunks` | Intra-source pgvector + FTS rows. Used for descent only. |
| `retrieval_index_records` | Per-source embedding state; `embedder_model` tracks the gateway model used. |

## Storage layout

```
s3://<bucket>/projects/{project_id}/sources/{source_id}/{sha256}.{ext}
s3://<bucket>/projects/{project_id}/normalized/{source_id}/{version_no}.md
```

The `AssetStore` wrapper verifies sha256 on read; mismatch raises.

## Status lifecycle

`ingested → normalizing → normalized → chunking → indexed → wiki_done`

Failures land at any phase as `failed` (with `failure_reason`) or
`wiki_failed` (with the AgentRun.error_text). UI surfaces both.

## Chunking and embeddings

See [`docs/pipelines/chunking-and-embedding.md`](../pipelines/chunking-and-embedding.md).
Embeddings are **intra-source only** — the wiki, not these chunks, is the
primary retrieval surface.

## Permissions

All RKS tables are project-scoped. `GET /v1/projects/{id}/sources/...`
routes are guarded by the project-scope dep; non-members get 404.
