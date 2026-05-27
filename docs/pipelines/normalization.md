# Normalization pipeline

The `normalize_job` worker takes a `SourceVersion`, reads the bytes from
MinIO/S3, picks a normalizer by MIME type, and produces a canonicalized
Markdown string + structure outline + quality flags.

## Dispatch

| MIME | Primary | Fallback |
|---|---|---|
| `application/pdf` | `pypdf` | `pdfminer.six` |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `python-docx` | — |
| `text/html`, `application/xhtml+xml` | `readability-lxml` + `bs4` | — |
| `text/markdown`, `text/x-markdown` | passthrough | — |
| `text/plain` | passthrough | — |
| `application/epub+zip` | `ebooklib` | — |

If the primary parser raises `NormalizationFailed` and a fallback
exists, the dispatcher tries the fallback. If both fail, the source is
marked `status="failed"` with a `failure_reason` and the UI surfaces it.

## Parser versioning

Every `NormalizedDocument` carries `parser` + `parser_version`
(`pypdf@5.5.0`, `python-docx@1.2.0`, etc.). When parser versions change
in a code-level upgrade, existing sources are NOT auto-re-normalized.
Re-normalization is opt-in via `POST /v1/projects/{id}/sources/{id}/reingest`
(owner-only) which creates a new `SourceVersion`.

## Quality flags

The normalizer emits a list of `quality_flags`:

- `ocr-required` — extracted text is empty or <100 chars/page.
- `tables-not-extracted` — tables detected but couldn't be parsed.

Flags are visible in the source detail view. They don't block the
pipeline.

## Output canonicalization

- Line endings normalized to `\n`.
- Runs of 3+ blank lines collapsed to one.
- Trailing whitespace trimmed; file ends with a single newline.

## Failure handling

Per Inc 0 §0.15 (no silent failures):

- Each parser exception is wrapped in `NormalizationFailed` with the
  underlying error message preserved in `failure_reason`.
- `Source.status = "failed"` is set in the same transaction as the
  ledger event `source.status_change` so the UI sees consistent state.
