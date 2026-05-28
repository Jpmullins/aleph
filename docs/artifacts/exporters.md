# Exporters

Four built-in exporters. Add new ones by writing a function that
takes a `BuilderState` and returns `(bytes, content_type, extension)`,
then registering it in `aleph_artifacts/builder/workflow.py`'s
`_node_package`.

## report_markdown_bundle

ZIP containing:
- `report.md` — the composed markdown + bibliography section
- `manifest.json` — outline + lineage + csl_style
- `assets/...` — embedded rendered assets (PNG/SVG/PDF)

This is the lossless export — every other format is derived from this.

## report_pdf

Markdown → HTML (via markdown-it-py) → PDF (via WeasyPrint by default;
swap to Prince via the optional `renderer` argument). Embedded assets
inlined. CSL bibliography rendered as a styled list.

## report_docx

Pending — uses pandoc when the operator installs it. Plumbed in
exporters but not wired into the default `_node_package` switch yet.
Inc 8 wires it once we have the eval harness to detect formatting
regressions.

## source_pack

ZIP of:
- `manifest.json` — full Source metadata (citation, license, retrieval
  timestamp, hashes)
- `raw/<short_id>.{ext}` — original asset bytes
- `normalized/<short_id>.md` — normalized markdown
- `LICENSE-NOTES.md` — attribution notes

This is the "give me everything that backed this analysis" export.
