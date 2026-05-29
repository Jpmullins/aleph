---
name: wiki-style
description: Wiki conventions for Aleph — using [[wikilink]] markers, grounding claims with [cN] citation markers, respecting hand-edit protection, and choosing whether to ingest a source, research a topic, or promote a note (delegated to the wiki_builder subagent).
---

# Wiki conventions

The compiled wiki is the primary knowledge surface. Follow these conventions
whenever you read from or grow it.

## Wikilinks

- Reference any wiki page by its title in double brackets: `[[Page Title]]`.
- When you cite the wiki in an answer, use `[[Page Title]]` markers so the
  analyst can jump to the source page. Use the exact page title.
- Pages connect through wikilinks; the retriever expands one hop along them,
  so linking related pages improves future retrieval.

## Claims and citations

- Every substantive statement on a page is a **claim** grounded in a source.
- Claims carry `[cN]` citation markers (e.g. `[c1]`, `[c2]`) that point to the
  source backing them. Preserve these markers when quoting page content.
- Never assert something the wiki does not support; if a claim lacks a citation,
  it is unverified — say so rather than presenting it as fact.

## Hand-edit protection

- Analyst hand-edits to a page are protected: the wiki agent will not silently
  overwrite a human edit on recompile. Treat hand-edited sections as
  authoritative and do not propose changes that would clobber them.

## Ingest vs. research vs. promote a note

Choose the right way to grow the wiki, then delegate to the `wiki_builder`
subagent via the `task` tool:

- **Ingest a source** — the analyst has a specific URL or document. Ingesting
  fetches, normalizes, chunks, and folds it into the wiki. After it returns,
  render a SourceCard for the ingested source.
- **Research a topic** — no specific source, the wiki lacks coverage of a
  question. Use the `research` skill (delegate to the `researcher` subagent)
  instead; it produces a draft page via a Briefs proposal.
- **Promote a note** — the analyst has an existing note worth turning into wiki
  content. The wiki_builder promotes it to a draft wiki page for review.
