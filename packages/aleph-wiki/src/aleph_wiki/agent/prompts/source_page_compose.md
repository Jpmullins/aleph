You are composing a wiki page that represents ONE source document inside
a research project's wiki. This page is called a `SourcePage`.

Compose a markdown body with this structure:

```
# {source_title}

## Provenance
- Identifier: [[Source:{short_id}]]
- Connector: {connector_kind}
- URL: {url_or_none}
- Ingested: {ingested_iso8601}

## Summary
{two_to_four_sentences}

## Concepts covered
- [[{concept_canonical_name_1}]] — {one_line_descriptor}
- [[{concept_canonical_name_2}]] — {one_line_descriptor}
...

## Key claims
1. {claim_text} [c1]
2. {claim_text} [c2]
...
```

Rules:
- Wrap every internal concept link in `[[...]]` so the wiki agent can build the link graph.
- Citation markers are `[c1]`, `[c2]`, ... — one per claim. You only emit the marker; the system maps markers to source chunks.
- Keep `Summary` to 2–4 sentences.
- Keep `Key claims` to the strongest 5–10 claims supported by the document.
- Do NOT invent facts not present in the document.
- Do NOT include sections beyond the four above.

If `Rejection feedback for this source` is provided in the input, address each
rejection reason before composing. The rejection reasons are constraints on
what NOT to do in the new revision.

If `Hand-edited sections to preserve` is provided, do NOT modify those section
headings or any content under them; the system splices them back in verbatim.
