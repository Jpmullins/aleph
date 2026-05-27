You are composing a stub wiki page for a single concept that appears in
the project's sources. The wiki agent uses this when:
  - The concept is new (no existing page yet), OR
  - An existing page needs to be extended with content from a new source.

For a NEW stub, produce:

```
# {canonical_name}

## Definition
{one_or_two_sentence_definition}

## Mentioned in
- [[Source:{short_id_1}]] — {context_snippet_or_section_heading}
- [[Source:{short_id_2}]] — {context_snippet_or_section_heading}
...

## See also
- [[{related_concept_1}]]
- [[{related_concept_2}]]
```

For an EXISTING page being extended, you receive the prior body. Append a
new line under `## Mentioned in` referencing the current source. Do NOT
restructure or rewrite the existing page; the wiki is grown by accretion,
not refactoring. (Refactoring is reserved for `--synthesize`, Inc 3.)

Rules:
- The `## Mentioned in` line is always required.
- Definitions must be derived from the source — don't speculate.
- `## See also` is optional; include only when other concepts in the same
  source are obviously related.
- Stub pages have no `## Key claims` section (those live on SourcePages).
- Respect Hand-edited sections (input section list); do NOT touch them.
- Respect Rejection feedback if provided.
