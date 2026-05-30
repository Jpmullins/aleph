# Wiki

The wiki is Aleph's **primary knowledge surface for both the analyst and
the assistant**. Sources upstream feed it; wiki pages downstream feed
retrieval, hypotheses, artifacts. The Karpathy LLM-Wiki pattern, made
multi-agent and audit-grade.

## Core entities

| Table | Notes |
|---|---|
| `wiki_pages` | One row per page. `page_kind ∈ topic | source | synthesis | stub`. `current_revision_id` points at the head. |
| `wiki_revisions` | **Immutable.** Postgres triggers raise on UPDATE/DELETE. New revision on every commit. |
| `wiki_sections` | Sub-page granularity: a heading anchor + char range. Used by hand-edit and rejection-feedback wiring. |
| `wiki_links` | `[[wikilink]]` edges. Rebuilt per commit. `dst_page_id` null = unresolved (alias pending). |
| `wiki_claims` | Atomic factual assertions with confidence. |
| `citations` | `claim → DocumentChunk[]` or `claim → SourcePage` evidence. |
| `source_pages` | Bridge: one wiki page per `Source`. Carries extracted claims + back-link to the asset. |
| `aliases` | `surface_form → canonical_name`. Drives `[[wikilink]]` repair. |
| `hand_edit_marks` | Analyst-protected region. Wiki agent must not regenerate. |
| `rejection_feedbacks` | Analyst rejection reasons, fed into next compile prompt for the same concept. |
| `wiki_index` | Denormalized retrieval index. One row per page. GIN tsvector index over (title + summary + aliases). |

## Why a wiki, not RAG-over-chunks

- **Cheap deterministic page selection.** The index is a tsvector + LLM
  router (Inc 2). Querying it is O(rows) not O(corpus).
- **Persistent improvement.** Every compile updates pages; the KB grows
  over time. Embeddings don't compound.
- **Auditable.** Revisions are immutable; ledger + Langfuse spans tie
  every change to the agent run that produced it.
- **Hand-editable.** Analysts edit pages directly; `HandEditMark` keeps
  the next compile from clobbering their work.
- **Live.** Wiki updates are pushed sub-second. When an agent compiles or
  commits a page, the Wiki surface reflects it immediately via the
  Postgres LISTEN/NOTIFY push layer (the `changes` stream + `compile_page`
  presence signals) — the index and the currently-open page refresh in
  place, with an "✦ editing…" badge while compiling and an "updated" pulse
  on commit. Limit: hand-edits (`mark_section`) write no ledger event, so
  they are **not** pushed live (the editor still sees their own change
  locally).

## SourcePage bridge

Every `Source` has a `SourcePage`. The page lives at
`[[Source:<short_id>]]`, with structured Provenance / Summary / Concepts
covered / Key claims sections. The wiki agent composes these on first
ingest and updates them on reingest.

## Status

Pages are `draft` until approved (Inc 5 approval workflow). The wiki
agent only commits drafts; the analyst (or the EditorialReviewer in Inc 5)
moves a page to `approved`.
