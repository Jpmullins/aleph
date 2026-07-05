# Wiki

The compiled wiki is the **primary retrieval surface**. Pages are wikilinked, revisioned, multi-agent-maintained, and — as of the trust layer — freshness-scored and retraction-aware. **Markdown is the only write-format**; every rendering path (reader card, HTML compiler) reads markdown and never writes it.

## Wiki-first retrieval

The primary path is:

```
WikiIndex page-selector LLM  →  load selected pages + 1-hop wikilinks  →  answer composer
```

`IndexService.select_pages` generates FTS candidates over (title + aliases + summary); an LLM page-selector (`Capability.PAGE_SELECTION`) tags each pick `primary`/`supporting`/`peripheral`; deterministic bounded 1-hop wikilink expansion loads neighbors; the composer (`Capability.SYNTHESIS`) answers. Embeddings (`DocumentChunk` over pgvector) are used **only** for intra-source descent — never as first-line RAG.

## Compile, hand-edits, aliases, feedback

- `WikiService.commit_revision` is atomic and idempotent (no-op when body unchanged). It **splices** protected hand-edit section text from the prior revision into the agent's proposed body before commit, writes a ledger event per revision, inserts claims + citations, and refreshes `WikiIndex` in the same transaction. `WikiRevision` is immutable (Postgres triggers).
- `AliasService` — `upsert` / `resolve` / `repair_broken_links`.
- `HandEditMarkService` — `mark_section` / `clear_section` / `list_active_for_page`.
- `feedback_service` — rejection feedback rows whose `addressed_in_revision_id` is set on the next commit, so a re-synthesis sees why a prior draft was rejected.

## Curator

`CuratorService.curate()` is the post-commit maintenance pass (`curate_page_job`, enqueued after synthesis commits a page). Its stage-1 transaction runs deterministic steps including `_recompute_freshness` (below).

## Freshness score (deterministic, curator-computed)

`aleph_wiki.freshness.compute_freshness(page, *, revision, citations, source_versions, now)` is a **pure function** returning an int 0–100 as the sum of four 0–25 dimensions:

- **Recency** (0–25): half-life decay on `verified_at` (fallback `last_compiled_at`), `25 * 0.5**(age/halflife)`.
- **Citation health** (0–25): fraction of the page's claims that are cited (resolvable `Citation` → non-retracted `Source`), scaled to 25.
- **Source freshness** (0–25): half-life decay on the *oldest* contributing `SourceVersion.fetched_at`.
- **Verification** (0–25): 25 if `verified_at` is newer than the current revision's `created_at`, else a partial by claim-confidence mix; **0 if any contributing source is retracted** (a retracted source forces the page unfresh).

Half-lives come from page `volatility`: **hot 30d, warm 90d, cold 365d** (default `warm`). Freshness is a **derived deterministic score**, not authored state — it is written inside the already-ledgered curate transaction and emits no ledger event of its own. Pages carry `volatility` (String(8)), `verified_at` (timestamptz), and `freshness` (SmallInteger 0–100). The workspace shows the badge + sorts the page list by it.

## Refresh job (staleness → ApprovalCard)

`wiki_refresh_job(project_id, page_id, token)` **never auto-recompiles**:

- Re-fetches each contributing source via its connector `fetch()` (a new `SourceVersion`), normalizes, and **fact-diffs** the new normalized markdown against the stored prior version.
- Classifies each source `unchanged` | `updated` | `contradicted` | `unreachable` via `LiteLLMClient.chat(capability=CLASSIFICATION, purpose="wiki.refresh.factdiff")` (bounded JSON verdict; `unreachable` on fetch failure, no LLM call).
- Emits **one `ApprovalCard`** per page (`target_kind="refresh_result"`). **Approve/skip** (unchanged) → bumps `WikiPage.verified_at = now` (freshness rises). **Flag** (updated/contradicted) → downgrades affected `WikiClaim.confidence` to `contested` and sets a page banner flag; **never recompiles the page**. Both write an `ApprovalDecision` + ledger event and enqueue a curator freshness recompute.
- Enqueued by a bounded `refresh_stale_pages_job` scheduler pass (picks pages below a freshness threshold with `verified_at` older than the volatility half-life); the per-page job is also directly invocable.

## Source retraction + blast-radius

`aleph_reviewer.retraction.retract_source(session, ledger, principal, *, source_id, reason)` is the **single path** all retraction triggers funnel through (manual, DOI-verification, and scholar auto-detect):

- Sets `Source.status="retracted"`, `retracted_at`, `retraction_reason`; ledger `source.retract`.
- Walks the blast-radius join `Source → SourcePage → Citation → WikiClaim`. Every dependent `WikiClaim` is set `confidence="retracted"`, `status="contested"` (ledger `wiki_claim.retract_flag` per claim). Each affected `WikiPage` surfaces a `retracted-source` badge derived from having ≥1 retracted-confidence claim.
- Emits a `retracted_source` `ReviewFinding` (severity critical) into Briefs — the same finding kind the WP-2 reviewer `doi_verification` node emits. Network-unverifiable DOIs (`ok=None`) never trigger it.
- `dependent_claims(source_id)` returns the queryable blast-radius set; a freshness recompute is enqueued per affected page.
- Route: `POST /v1/projects/{pid}/sources/{sid}/retract` (EDITOR), ledgered.

## Drift (briefs / artifacts)

At build time `ArtifactVersion.lineage_jsonb` records `source_pages: [{page_id, revision_id, revision_created_at}]` plus the generated-at `created_at`. `drifted` is **computed live** (no stored flag): an artifact/brief is `drifted` iff any recorded upstream page's *current* `current_revision_id` differs from the recorded `revision_id`. It surfaces as a `drifted: bool` prop → an amber "drifted" pill on `ArtifactCard` and pinned brief cards.

## Rendering (WP-4 catalog, delta substrate)

`_wiki_messages` populates `page_meta.freshness` + `volatility` + `verified_at` on the open page and `freshness` on each page-list row. `ClaimCard.confidence` includes `"retracted"`; `WikiPageCard`/`SourceCard` carry a `retracted: bool` prop and show a "⚠ retracted source" banner. `ApprovalCard.target_kind` includes `"refresh_result"`. All trust mutations append ledger events, so the existing `action_ledger_events` NOTIFY trigger wakes the surface stream — no polling, no new trigger.
