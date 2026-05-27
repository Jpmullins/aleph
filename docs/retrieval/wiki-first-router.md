# Wiki-first retrieval router

Implements §4.2 of the top-level spec. The router is the load-bearing
piece of "wiki is the primary KB."

## Stages

1. **FTS candidate generation.** `IndexService.select_pages` over
   `wiki_index.index_tsv` returns the top `top_k_pages * 3` candidates
   ordered by `ts_rank`. Deterministic; no LLM cost.
2. **LLM page-selector** (capability `page_selection`). Picks up to
   `top_k_pages` candidates and tags each `primary | supporting |
   peripheral`. The default `aleph-dev` profile uses
   `claude-haiku-4-5`; `aleph-production` uses `claude-sonnet-4-6`.
3. **1-hop wikilink expansion.** For every `primary` page, follow
   outgoing `WikiLink` rows (with `dst_page_id` set) ordered by
   `occurrences`. Bounded to `top_k_pages` extra. Deterministic.
4. **Composer** (capability `synthesis`). Receives selected page
   bodies + expanded bodies + the user query. Returns
   `{body_md, descent_requests, synthesis_requests}`. The composer
   preserves `[[wikilink]]` and `[cN]` markers from the source pages.
5. **Descent (optional).** When the composer returns
   `descent_requests`, the router embeds each query string via the
   gateway and calls `aleph_rks.retrieval.descend_into_source` against
   that single source. Budget capped at `descent_budget_chunks` total.
   Then re-runs the composer with the descent text added.
6. **Synthesis flag.** When the composer returns `synthesis_requests`,
   `coverage_judgment` is set to `synthesis_needed` and the assistant
   tells the analyst the wiki is missing this. Inc 3 wires the
   `/synthesize` action to AIQ; Inc 2 is honest about the gap.

## Coverage judgments

| Value | Meaning |
|---|---|
| `ok` | The composer answered from the wiki without flags. |
| `descent_used` | The composer requested descent; it ran; the recomposed answer used those chunks. |
| `descent_needed` | Descent was requested but no chunks satisfied; the assistant says so. |
| `synthesis_needed` | The wiki has no coverage; `/synthesize` is the next step (Inc 3). |

## Token budgeting

Composer context is built from selected page bodies first, then
expanded bodies, then descent chunks. If the binding's
`max_input_tokens` is approached, `peripheral`-tagged pages are
dropped first, then long bodies truncated with `[truncated]`. The
truncated-page list is recorded under `retrieval_jsonb.truncated_pages`.

## Why this is the *primary* path

- Cheap deterministic page selection (FTS) sets the candidate pool;
  the LLM selector only chooses among curated candidates.
- The composer is fed *full page bodies*, not chunk excerpts, because
  the wiki is *already* the curated KB. The wiki agent paid the cost
  to compress and structure; the composer benefits.
- Embeddings appear only on descent, scoped to one source. They
  cannot drift across the corpus.

## What it does NOT do

- Cross-source chunk retrieval. The chunk index is intra-source-only;
  cross-source retrieval is the wiki's job.
- Free-form web search. Connectors (Inc 3+) feed the wiki via the
  `/synthesize` flow.
- Generative-UI / arbitrary JS execution. The composer's output is
  Markdown with wikilink/citation markers — Inc 4 layers A2UI cards on
  top declaratively.
