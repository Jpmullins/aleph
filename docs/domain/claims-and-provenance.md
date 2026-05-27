# Claims and provenance

A **claim** is an atomic factual assertion on a wiki page. Claims tie
the wiki to the RKS via `citations` rows.

## Shape

```
WikiClaim
  ├── id, project_id, page_id, revision_id
  ├── section_anchor — where on the page the claim lives
  ├── text — the claim itself
  ├── confidence ∈ { well-supported | weakly-supported | contested | uncited }
  └── status ∈ { active | superseded | rejected }
```

## Citation edges

Two kinds of citations exist:

- **Claim → DocumentChunk[]** — strong evidence. Used by the wiki agent
  when it has a specific chunk from a specific source backing the claim.
- **Claim → SourcePage** — coarse evidence. Used when the claim is
  supported by the source as a whole; the analyst can drill into the
  source page to see extracted claims.

The `citations.citation_marker` field stores the rendered marker
(`[c12]`) as it appears in the page body. The Inc 2 retrieval router
preserves these markers in chat output; clicking opens the source chunk.

## Confidence transitions

Inc 1 sets `confidence="cited"` on every claim emitted by the wiki agent
when at least one citation exists, and `"uncited"` otherwise. The
MechanicalReviewer (Inc 5) reclassifies claims as the citation graph
changes:

- Citation removed → `confidence` drops.
- Citation source archived → `weakly-supported`.
- Citation contradicted by another claim → `contested`.
- Citation never existed → stays `uncited`.

## Why claims are first-class

Claims let downstream systems reason about *what* a page asserts, not
just *what it says*. The reviewer queue, hypothesis evidence, and the
artifact builder all operate on claims, not free-form prose.
