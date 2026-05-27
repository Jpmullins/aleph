# Upload connector

The only connector in Inc 1. Push-only — `search` raises `NotSupported`.

## Endpoint

```
POST /v1/projects/{project_id}/sources/upload
  multipart/form-data: file (required), title (optional)
```

## Pipeline

1. Compute sha256 of the upload.
2. Store bytes at `s3://<bucket>/projects/{project_id}/sources/{source_id}/{sha256}.{ext}`.
3. Create `Source` + `SourceVersion` + `SourceAsset` rows in one
   transaction. Source.status = `normalizing`.
4. Mint a short-lived agent token bound to a new `AgentRun`.
5. Enqueue the `normalize_job` (Arq via Redis).
6. Return the `Source` row.

After return, the worker runs:

- `normalize_job` → `NormalizedDocument` → enqueue `chunk_embed_job` +
  `wiki_ingest_job`.
- `chunk_embed_job` → `DocumentChunk` rows + `RetrievalIndexRecord` →
  set Source.status = `indexed`.
- `wiki_ingest_job` → wiki revisions via the LangGraph workflow → set
  Source.status = `wiki_done` on success or `wiki_failed` on failure.

## Supported types

`application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`,
`text/html`, `text/markdown`, `text/plain`, `application/epub+zip`.

## Future evolution

Inc 3 re-registers this connector as a `nat` function inside AIQ's
`data_source_registry`. The plugin protocol does not change — only the
registration location.
