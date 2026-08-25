# The Claim Spine

The knowledge layer, and the first substantial plugin in Aleph's research suite. It replaces the
compiled wiki. **Note:** D1 has since been superseded — the wiki is not being replaced. The Claim Spine is the wiki's evidence layer, not its successor (see [`decisions.md`](decisions.md) D1, 2026-08-21).

**Status: designed, partially built.** `packages/aleph-belief` currently holds the patch contract and
trust lattice and nothing consumes them yet — which is precisely the defect class this codebase is
prone to, and it is tracked as such. Nothing below ships without a consumer in the same change.

## The idea

A **claim** is the durable unit of knowledge, not a page and not a document. It survives every rewrite
of every rendering surface, because no surface owns it.

- **Evidence** is a verbatim-anchored span in a source, carrying a stance and a weight.
- **Confidence is derived**, never asserted by a model — a pure function over stance-weighted evidence.
- **Revision is supersession**, never in-place mutation and never `DELETE`.
- **Prose is a render.** HTML artifacts and reports are generated from claims; they are not the store.

Three properties fall out that the wiki never had: retraction becomes a graph walk, a human edit
survives the next compile, and the whole layer can be rebuilt from the RKS.

## Data model

One migration. Mostly `ALTER` on tables that already exist; exactly one new table.

### `wiki_claims` — the belief node

The table keeps its name to avoid touching six call sites; it stops being page-owned.

```sql
ALTER TABLE wiki_claims
  ALTER COLUMN revision_id DROP NOT NULL,      -- claims outliving revisions IS the fix
  ADD COLUMN claim_key      varchar(64),       -- sha256(normalize(text))
  ADD COLUMN superseded_by  uuid REFERENCES wiki_claims(id),
  ADD COLUMN origin         varchar(16) NOT NULL DEFAULT 'agent',
  ADD COLUMN evidence_tier  varchar(16) NOT NULL DEFAULT 'inferred',
  ADD COLUMN rationale      text NOT NULL DEFAULT '',
  ADD COLUMN embedding      vector(1024),
  ADD COLUMN support_count  int NOT NULL DEFAULT 0,
  ADD COLUMN distinct_source_count int NOT NULL DEFAULT 0,
  ADD COLUMN last_surfaced_at timestamptz;

CREATE UNIQUE INDEX uq_claims_project_key ON wiki_claims (project_id, claim_key)
  WHERE superseded_by IS NULL;
CREATE INDEX ix_claims_live      ON wiki_claims (project_id) WHERE superseded_by IS NULL;
CREATE INDEX ix_claims_embedding ON wiki_claims USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ix_claims_text_fts  ON wiki_claims USING gin (to_tsvector('english', text));
```

- `origin ∈ {user, agent, research, curator}` — **`user` is immutable to agents.** Enforced in the
  write path, not requested in a prompt.
- `evidence_tier ∈ {stated, observed, inferred, derived}` — set by the *writer*, never by the model.
- `confidence` survives but is **recomputed, never written by a model**.
- `text` holds a proposition capped at ~300 chars; reasoning spills to `rationale`. A 2048-character
  "claim" is a paragraph, and a paragraph cannot be contradicted, retracted, or scored.

### `citations` — the evidence edge

```sql
ALTER TABLE citations
  ADD COLUMN source_id    uuid,        -- the retraction join key; NOT NULL after backfill
  ADD COLUMN chunk_id     uuid,        -- one row per span
  ADD COLUMN quote        text,
  ADD COLUMN verbatim     boolean NOT NULL DEFAULT false,
  ADD COLUMN stance       varchar(16) NOT NULL DEFAULT 'supports',
  ADD COLUMN weight       real NOT NULL DEFAULT 1.0,
  ADD COLUMN locator_hash varchar(64); -- sha256(source_id|chunk_id|char_start|char_end)

CREATE UNIQUE INDEX uq_citations_claim_locator ON citations (claim_id, locator_hash);
CREATE INDEX ix_citations_source ON citations (source_id);
```

`stance ∈ {supports, contradicts, contextualizes}` and `weight` are chosen to match
`aleph_belief.confidence.EvidenceRow` **exactly**, so the existing confidence engine consumes
citations with zero adaptation.

`locator_hash` with a unique constraint makes re-derivation a **union rather than a clobber** — merging
two extractions never loses or duplicates a span.

`source_id` being `None` on every production write path is the single defect that silently voided
retraction blast-radius, two freshness dimensions, the reviewer's source registry, and the citation
popover. Backfilling it is the highest-value column change in the design.

### `claim_edges` — the only new table

```sql
CREATE TABLE claim_edges (
  id           uuid PRIMARY KEY,
  project_id   uuid NOT NULL,
  src_claim_id uuid NOT NULL REFERENCES wiki_claims(id),
  dst_claim_id uuid NOT NULL REFERENCES wiki_claims(id),
  kind         varchar(16) NOT NULL,   -- supports|contradicts|derived_from|specializes|supersedes
  weight       real NOT NULL DEFAULT 1.0
);
```

This is what `WikiLink(src_page_id, dst_title)` should have been. It is what makes retraction a graph
walk instead of a citation lookup.

## Confidence

Not new work. `packages/aleph-belief/src/aleph_belief/confidence.py` already computes
`net = Σ weight·sign(stance)` → `well_supported | weakly_supported | contested | refuted` as a pure,
tested function with zero LLM calls. `html_compiler.py` already renders per-claim cards with CSS
classes matching those exact strings. **The two were built to fit each other and never connected.**

Confidence is recomputed in the same transaction as any `citations` or `claim_edges` write.

## Retrieval

Hybrid, corpus-wide, fused by reciprocal rank fusion at k=60:

```
score = 1/(60 + rank_dense) + 1/(60 + rank_lexical)
```

The dense and lexical legs already exist in `aleph-rks/retrieval.py` as `0.6·cosine + 0.4·ts_rank`,
constrained to one source by a single predicate. Removing that predicate makes it corpus-wide; swapping
`plainto_tsquery` (ANDs every term) for `websearch_to_tsquery` (OR + phrases) fixes the query gate.

A retrieval miss returns an **honest diagnostic** — naming the likely reason, e.g. vocabulary mismatch
rather than absence — never a degraded fallback of adjacent material.

## Grounding

Every quote passes an **LLM-free verbatim gate** before a citation is written: NFKD deaccent, ligature
fold, lowercase, whitespace-collapse, then a plain substring test against the chunk text the RKS already
stores, with a normalized→raw offset map so the span resolves back to real bytes.

The composer proposes `(quote, claimed_source)`. The **harness** resolves marker → `source_id`/`chunk_id`
from its own registry; the model never supplies an identifier that becomes a foreign key.

## Revision and retraction

- **Supersession, not mutation.** A revision writes a new row and points the old one at it. History is
  walkable without a revisions table.
- **Relational staleness.** A claim is stale because *its subject moved* — the source it rests on is at
  a new version — not because a clock ticked. Age is only the fallback when no subject relation exists.
- **Retraction is a recursive CTE** over `citations.source_id` and `claim_edges.derived_from`, depth
  capped, cycle guarded, **with a declined branch**: a claim with an independent surviving citation stays
  believed, annotated that one support was withdrawn.

## Curation: deterministic first, LLM as adjudicator

See [`decisions.md`](decisions.md) D3. Deterministic passes generate scored merge candidates with named
reject reasons; the LLM only adjudicates the ambiguous band, and its output is a **patch** — proposed,
validated, applied — never a mutation. `packages/aleph-belief` holds that contract.

```
score ≥ high   → auto-apply       (no LLM)
score ≤ low    → auto-reject      (no LLM)
between        → LLM adjudicates  (the entire LLM budget lives here)
```

## What this deletes

`curator_service.py` (929), `index_service.py` (201), `alias_service.py` (153),
`handedit_service.py` (130), `citation_verification.py` (76), `feedback_service.py` (100), the LLM
page-selector hop and `Capability.PAGE_SELECTION`, and the `wiki_index` / `wiki_links` / `wiki_sections`
/ `hand_edit_marks` / `aliases` tables — **but only after the replacement wins on a real retrieval eval.**
Not before.
