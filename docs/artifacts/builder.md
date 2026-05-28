# Builder agent

LangGraph workflow that composes approved wiki content + datasets +
rendered card assets + a CSL bibliography into an exportable
`ArtifactVersion`.

## Nodes

1. **`outline`** — collect titles + slugs for the selected wiki pages.
2. **`section_compose`** — concatenate each page's current revision
   body as a `## Title\n\n...` section. Wikilinks and `[cN]` markers
   preserved.
3. **`citation_resolve`** — find every `[[Source:Snnnn]]` reference in
   the composed body, build a CSL-JSON list from the matching `Source`
   rows.
4. **`chart_freeze`** — record `RenderedAsset` rows for each referenced
   `DatasetVersion`. The actual chromium render lives in
   `render_card_job` and is dispatched separately.
5. **`bibliography`** — format the CSL-JSON list via the chosen style
   (`apa-7` / `chicago-author-date` / `ieee` / `vancouver` /
   any custom CSL XML the operator drops in `csl/styles/`).
6. **`package`** — produce the final bytes per `artifact_kind`. Upload
   to MinIO at
   `s3://<bucket>/projects/{project_id}/artifacts/{artifact_id}/{version_no}.{ext}`.
   Records `bytes_size`, `sha256`, and the complete `lineage_jsonb`
   (page ids, dataset version ids, rendered asset ids, agent_run_id,
   csl_style, template_name).

## API

```
POST /v1/projects/{id}/artifacts/build
  body: { title, artifact_kind, template_name, csl_style,
          wiki_page_ids, dataset_version_ids }
```

Returns `{artifact_id, agent_run_id, dispatched}`. The Builder runs
asynchronously; subscribe to `/v1/projects/{id}/agent-runs/{run_id}`
for progress.

## Lineage

Every `ArtifactVersion.lineage_jsonb` carries the complete provenance
chain — which wiki revisions, which dataset versions, which rendered
assets, which agent run. The `data_sha256` of the final bundle plus the
ledger event around the build means the artifact is reproducible:
re-run the same template against the same lineage, and the bytes
match.
