# Wiki agent

A LangGraph DAG run by `aleph-workers` when a `NormalizedDocument` lands.
Turns a single source into wiki content: a `SourcePage` plus topic-page
stubs for every concept the source covers.

## Nodes (linear)

1. **`concept_extraction`** — LLM (capability `extraction`) reads the
   normalized markdown and returns canonical concepts.
2. **`alias_extraction`** — LLM (capability `extraction`) takes the
   concept list and proposes additional surface-form aliases. Persists
   `Alias` rows.
3. **`source_page_compose`** — LLM (capability `synthesis`) produces the
   `SourcePage` body + summary + key claims.
4. **`topic_page_stubs`** — LLM (capability `extraction`, per-concept)
   produces a stub page per new concept or extends an existing one.
5. **`wikilink_resolve`** — deterministic. Resolves `[[wikilink]]`
   targets via `AliasService.resolve`; null targets remain unresolved
   until a later compile creates the page.
6. **`commit_revision`** — calls `wiki_service.commit_revision` per
   draft page. Inserts/updates `SourcePage` bridge row. Repairs any
   broken links across the project (cheap).
7. **`wiki_index_update`** — structural step. `IndexService.refresh_page`
   already ran inside `commit_revision`; this node documents the
   pipeline contract.

## Instrumentation

Every node opens an OTEL span with `aleph.node`,
`aleph.agent_kind="wiki"`, `aleph.project_id`, `aleph.source_id`. An
`AgentRun` row is created at workflow start; on success/failure it's
finalized to `succeeded`/`failed` with `result_payload` or `error_text`.

Each LLM call goes through `LiteLLMClient.chat` so the cost is ledgered
and the gateway is the only LLM transport.

## Hand-edit + rejection feedback wiring

- `commit_revision` reads active `HandEditMark`s for the page and
  splices the protected sections back into the new body verbatim. The
  agent prompt also receives the protected section text marked
  "DO NOT MODIFY".
- `source_page_compose` reads pending `RejectionFeedback` rows keyed by
  `Source:<short_id>` and includes them as constraints. On commit, the
  feedback rows' `addressed_in_revision_id` is set to the new revision.

## Live wiki signals

The workflow emits a page-scoped `compile_page` phase event so the Wiki
tab updates the instant the agent writes (no poll lag):

- `phase_started` fires at the start of `source_page_compose`, so the
  "✦ editing…" presence badge lasts through the multi-second LLM compose.
- `phase_completed` fires after `commit_revision`.
- `commit_revision` also adds `page_title` to the `wiki.revision.commit`
  ledger payload (additive), so the commit signal names the page.

These feed the push layer: `GET /v1/projects/{id}/changes/stream`
translates the `compile_page` phase events into `compiling` /
`compile_done` signals and `wiki.revision.commit` ledger events into
`committed` signals. The frontend `useWikiLiveSignals` hook subscribes
and invalidates the `wiki-pages` + `wiki-page` caches, so the index and
the open page refresh in place with an editing badge and an "updated"
pulse. See `docs/domain/wiki.md` for the surface behavior.

## Failure modes

If any node raises, the AgentRun is marked `failed` and the `Source`
moves to `wiki_failed` with `failure_reason` populated. Partial commits
are explicit: source pages and stubs are committed sequentially, so a
failure mid-way leaves the wiki in a consistent state (everything
committed before the failure is durable).
