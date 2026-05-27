# Chunking and embedding

The `chunk_embed_job` worker takes a `NormalizedDocument`, chunks the
markdown, batch-embeds via the LiteLLM gateway, and writes
`DocumentChunk` + `RetrievalIndexRecord` rows.

## Chunking

`aleph_rks.chunking.chunk_markdown`:

- Splits on Markdown structural boundaries (ATX headings, lists, fences).
- Preserves heading slugs as `section_path` (e.g. `Methods > Sample Selection`).
- Sentence-segments deterministically (regex; no LLM).
- Greedy-packs sentences into chunks up to `target_tokens=512`.
- Carries `overlap_tokens=64` from the previous chunk forward.
- Tokens counted via `tiktoken.cl100k_base` — stable, model-independent.

## Embedding

`aleph_rks.embedding.embed_texts` batches up to 64 inputs per gateway
call (Cohere embed v3/v4 limit is 96; we leave headroom). Every batch
goes through `LiteLLMClient.embed(..., purpose="rks.embed")` so:

- An OTEL span is opened per batch.
- A `ModelCall` + `CostLedgerEvent` row is written per batch.
- Tenacity retry kicks in on 5xx/429/connection errors.

`DocumentChunk.embedder_model` records which gateway model produced the
embedding. `RetrievalIndexRecord.embedder_model` mirrors this at the
source level.

## Indexes

- **`ix_chunks_embedding_hnsw`** — HNSW vector index with cosine
  similarity. `m=16`, `ef_construction=64`. Tuned for intra-source
  retrieval.
- **`ix_chunks_text_fts`** — GIN tsvector index on the `text_tsv`
  column, maintained by a `BEFORE INSERT/UPDATE` trigger.

## Re-embed on profile change

When a project's `ModelProfile.embedding` binding changes (e.g.
`cohere-embed-english-v3 → cohere-embed-v4`), the `RetrievalIndexRecord`
becomes stale. Inc 1 ships the detection (`aleph_rks.retrieval.needs_reembed`)
and the worker job; Inc 2's acceptance criteria includes the triggered
re-embed working end-to-end.

## Intra-source descent

`aleph_rks.retrieval.descend_into_source(session, project_id, source_id,
query_text, query_embedding, top_k=8)` returns a hybrid-scored hit list
within a single source. Score = `0.6 * cosine_similarity + 0.4 *
fts_rank` (both normalized). The wiki retrieval router (Inc 2) calls
this when the answer composer requests more detail from a specific
`SourcePage` cited in the wiki body.

**Embeddings never cross source boundaries.** This is intentional —
embedding-based retrieval over the whole corpus is the failure mode
Aleph avoids.
