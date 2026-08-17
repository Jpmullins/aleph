Postgres 18 (pgvector image), so PG14+ recursive-CTE `CYCLE`/`SEARCH` clauses are available. I have everything. Writing the report.

# Aleph data-model analysis — claims, provenance, and the web of belief

**Method.** Every claim below is either **[V]** verified by reading the cited file:line, or **[I]** inferred (reasoning stated). Two `Explore` subagents swept the frontend/A2UI layer and the Alembic/graph-query layer; I read the claim write path, curator, reviewer, refresh, freshness, concept/alias, and provenance code directly.

**Headline finding, stated up front:** the known gap is real but it is *not the deepest problem*. The deeper one is that **the provenance chain below `Citation` is entirely vacuous in production**. `Citation.chunk_ids` is `[]` at every production write site, and `Citation.source_page_id` is `None` at every production write site. A `Citation` row today carries exactly one piece of information: a marker string like `"[c1]"`. Every subsystem that claims to walk `Claim → Citation → SourcePage → Source` — freshness, retraction blast radius, refresh, mechanical review — silently returns empty in production and is only green because e2e fixtures hand-construct the missing link.

---

## §1. Full schema inventory — 60 tables

No table in the repo uses `ForeignKey`. Zero hits for `ForeignKey`/`create_foreign_key` across `apps/api/alembic/versions/*.py` and `packages/*/src` **[V]**. Every relationship is a bare, unconstrained UUID column. There are also **zero CHECK constraints** anywhere **[V]** — every enum-like field (`confidence`, `status`, `page_kind`, `volatility`, `access_scope`) is a free `String(N)`.

`CommonColumns` (`packages/aleph-db/src/aleph_db/base.py:18-38`) **[V]** supplies: `id` (uuid7 PK), `created_at`, `updated_at`, `created_by`, `access_scope` (default `"project"`), `trace_id`, `ledger_event_id` (nullable). Note: version/child tables (`WikiRevision`, `HypothesisVersion`, `DatasetVersion`, `ArtifactVersion`, `Citation`, `WikiSection`, `WikiLink`, `DocumentChunk`, `SourcePage`) deliberately **do not** inherit it and hand-roll a narrower column set — `WikiRevision.ledger_event_id` is `NOT NULL` (`aleph_wiki/models.py:89`) whereas `CommonColumns`' is nullable **[V]**.

### Entity map by subsystem

**RKS — `aleph-rks` (8 tables)** — `packages/aleph-rks/src/aleph_rks/models.py`
| Table | Key columns | Implied edges |
|---|---|---|
| `connectors` :38 | `kind` UNIQUE, `output_kind`, `metadata_schema_jsonb` | — (globally scoped; **violates CLAUDE.md rule 6** — no `project_id`) |
| `connector_bindings` :51 | `project_id`, `connector_id`, `config_jsonb` | → `connectors.id` |
| `sources` :63 | `project_id`, `connector_kind`, `short_id` UNIQUE, `status`, `current_version_id`, `retracted_at`, `retraction_reason` | → `source_versions.id` |
| `source_versions` :87 | `source_id`, `version_no`, `asset_id`, `sha256`, `fetched_at`, `parser_version`, `normalized_document_id` | → `sources`, `source_assets`, `normalized_documents`. **No `project_id`** (violates rule 6) |
| `source_assets` :100 | `project_id`, `storage_uri`, `mime_type`, `sha256` | — |
| `normalized_documents` :110 | `project_id`, `source_id`, `source_version_id`, `markdown_uri`, `parser`, `parser_version`, `char_count`, `structure_jsonb` | → `sources`, `source_versions` |
| `document_chunks` :127 | `project_id`, `source_id`, `normalized_document_id`, `ordinal`, `text`, `text_tsv` GIN, `embedding` Vector(1024) HNSW, `section_path`, **`char_start`/`char_end`**, `token_count`, `embedder_model` | → `sources`, `normalized_documents` |
| `retrieval_index_records` :156 | `project_id`, `source_id` UNIQUE, `chunk_count` | → `sources` |

**Wiki — `aleph-wiki` (12 tables)** — `packages/aleph-wiki/src/aleph_wiki/models.py`
| Table | Key columns | Implied edges |
|---|---|---|
| `wiki_pages` :35 | `project_id`, `title`, `slug`, UNIQUE(`project_id`,`slug`), `page_kind`, `current_revision_id`, `status`, `infobox_jsonb`, `volatility`, `verified_at`, `freshness` | → `wiki_revisions.id` |
| `wiki_revisions` :68 | `page_id`, `revision_no`, `body_md`, `body_sha256`, `parent_revision_id`, `author_kind/id`, `ledger_event_id` NOT NULL; UNIQUE(`page_id`,`revision_no`) | → `wiki_pages`; self-edge `parent_revision_id` (bare, **unindexed**) |
| `wiki_sections` :93 | `page_id`, `revision_id`, `anchor`, `char_start`, `char_end`, `body_sha256`, `ordinal` | → `wiki_revisions` |
| `wiki_links` :111 | `src_page_id`, `src_revision_id`, `dst_page_id` (nullable = red link), `dst_title`, `occurrences` | **the only graph-edge table**; `src_revision_id` **unindexed** |
| **`wiki_claims`** :124 | CommonColumns + `project_id`, `page_id`, **`revision_id`**, `section_anchor`, `text` String(2048), `confidence` String(16), `status` String(16) | → `wiki_pages`, `wiki_revisions`. **`revision_id` unindexed** |
| **`citations`** :136 | `project_id`, `claim_id`, **`chunk_ids` JSONB array**, `source_page_id` (nullable), `citation_marker` String(16) | → `wiki_claims`, `source_pages`?/`wiki_pages`? (**ambiguous, see §3**). `source_page_id` **unindexed** |
| `source_pages` :150 | `source_id` **UNIQUE**, `page_id` **UNIQUE**, `extracted_claims_jsonb`, `extracted_at` | Source↔Page bridge, hard-forced **1:1** |
| `aliases` :161 | CommonColumns + `surface_form`, `canonical_name`, `canonical_page_id`, `confidence`; **UNIQUE(`project_id`,`surface_form`)** | → `wiki_pages` (unindexed) |
| `hand_edit_marks` :175 | `page_id`, `section_anchor`, `body_sha256_at_edit`, `cleared_at`; partial idx WHERE `cleared_at IS NULL` | → `wiki_pages` |
| `rejection_feedbacks` :195 | `page_id`, **`concept_name`**, `rejected_revision_id`, `reason`, `addressed_in_revision_id` | → pages/revisions; keyed by *string* concept name |
| `wiki_index` :208 | PK `page_id`, `aliases_jsonb`, `wikilinks_out_jsonb`, `index_tsv` GIN | denormalized read model; **no `wikilinks_in`** |
| `page_merge_proposals` :238 | `source_page_id`, `target_page_id`, `similarity`, `approval_decision_id` | → `wiki_pages` (note: `source_page_id` here means a **WikiPage**, colliding in name with `citations.source_page_id`) |

**Hypotheses (3)** — `packages/aleph-hypotheses/src/aleph_hypotheses/models.py`: `hypotheses` :23 (`short_id`, `statement`, `confidence`, `current_version_id`), `hypothesis_versions` :40 (immutable, trigger-guarded), `hypothesis_evidence` :63 (`stance`, `evidence_kind`, **`target_id`** — a polymorphic untyped pointer; `weight`). *This is the closest existing analogue to a typed claim-edge table, and it is `hypothesis → {anything}`, not `claim → claim`.*

**Artifacts (3)**: `rendered_assets` :23, `artifacts` :40, `artifact_versions` :51 (immutable, `lineage_jsonb`, `builder_agent_run_id`).
**Notes (2)**: `notes` :14, `note_sections` :21.
**Datasets (3)**: `datasets` :24, `dataset_versions` :37 (immutable), `observations` :61 (`source_refs_jsonb`).
**Ledger/cost/identity — `aleph-db` (11)**: `action_ledger_events` (`ledger.py:36`, hash-chained, UPDATE/DELETE triggers), `ledger_chain_heads` :68, `model_calls` (`cost.py:16`), `cost_ledger_events` :42, `budgets` :59, `agent_runs` (`agent.py:16`), `agent_events` :31, `users` (`identity.py:14`), `project_members` :24, `projects` (`project.py:14`), `model_profiles` (`model_profile.py:20` — the one sanctioned global table).
**Connectors/approval (3)**: `connector_credentials` (`aleph_connectors/credentials.py:47`), `synthesis_proposals` (`models.py:20`), `approval_decisions` :33.
**Reviewer (3)**: `review_runs` :16, `review_findings` :31 (**`target_claim_id`** :42 — a direct dependency on claim identity), `approval_requests` :52.
**A2UI (3)**: `interactive_cards` :27, `interactive_card_versions` :42 (immutable), `card_actions` :65.
**Assistant (3)**: `assistant_sessions`/`threads`/`messages`.
**Evals (4)**: `eval_datasets`/`cases`/`runs`/`results` — `eval_datasets` and `eval_cases` are **global** (no `project_id`), another rule-6 exception not documented in CLAUDE.md.
**Core (2)**: `agent_memories` (`aleph_core/agent_memory.py:30`), `user_feedback` (`aleph_core/feedback.py:21` — `target_kind` includes `"claim"`, `target_id` a bare UUID).

Model discovery for Alembic: `apps/api/alembic/env.py:21` imports `aleph_db.models`, then lines 24-72 import eleven further model trees each wrapped in `try/except ImportError: pass` **[V]**. **Hazard:** a broken import in `aleph_wiki.models` makes `--autogenerate` silently emit `DROP TABLE` for every wiki table instead of failing.

---

## §2. Claim lifecycle, traced through code

### 2.1 Where claim text originates — only one live producer

| Producer | Site | What it emits |
|---|---|---|
| **Wiki ingest agent** (source pages only) | `packages/aleph-wiki/src/aleph_wiki/agent/workflow.py:401-426` **[V]** | LLM returns `{"claims":[{"text":…,"citation_marker":"[c1]"}]}`; each becomes `ClaimDraft(confidence="cited", section_anchor="key-claims", citations=[CitationDraft(chunk_ids=[], source_page_id=None, citation_marker=marker)])` |
| **Synthesis workflow** | `synthesis_workflow.py:209-231` **[V]** | Iterates `report.claims`; resolves `ref = report.citations_by_marker.get(...)` at :214 then **throws `ref` away** at :217-222, persisting only `citation_marker` |
| **Native research loop** | `packages/aleph-research/src/aleph_research/research_workflow.py:337` **[V]** | `claims=[]` — the deep-research loop produces **zero** claims |
| Topic stubs | `agent/workflow.py:513` **[V]** | `claims=[]` |
| Note promotion | `apps/api/src/aleph_api/routes/notes.py:270-281` **[V]** | `claims=[]` |
| Project bootstrap overview | `apps/workers/src/aleph_workers/jobs/bootstrap.py:204-215` **[V]** | `claims=[]` |
| Curator recommits | `curator_service.py:312, 491, 701` **[V]** | `carried_claims = await self._carry_claims(page=…)` — *copies* prior claims |
| Merge fold | `curator_service.py:748-782` **[V]** | Inserts new `WikiClaim` rows onto the **target's already-committed current revision** |

**[I]** Combined with §3: since `research_workflow` emits no claims and synthesis discards the source ref, the only claims that exist in a real deployment are the wiki-ingest agent's per-source "key claims", all with empty citations.

### 2.2 The write — `WikiService.commit_revision`

`packages/aleph-wiki/src/aleph_wiki/wiki_service.py:314-341` **[V]**:

```python
for c in claims:
    claim = WikiClaim(
        id=uuid7(),                      # ← line 317: fresh identity every commit
        project_id=project_id,
        page_id=page.id,
        revision_id=revision_id,         # ← line 320: bound to THIS revision
        ...
    )
    self._session.add(claim)
    await self._session.flush()
    for cite in c.citations:
        self._session.add(Citation(id=uuid7(), claim_id=claim.id,
                                   chunk_ids=[str(cid) for cid in cite.chunk_ids],
                                   source_page_id=cite.source_page_id, ...))
```

Ledger: **one** `wiki.revision.commit` event for the whole commit (`wiki_service.py:243-260`) — N claims, 1 event. The per-mutation ledger invariant is already interpreted at *revision* granularity for claims **[V]**.

### 2.3 What happens to old claim rows — **nothing. They are never deleted.**

`grep -rn "delete(WikiClaim|delete(Citation" packages apps` → **zero hits** **[V]**.

So on revision N+1: new `WikiClaim` rows are inserted with new uuid7s bound to `revision_id = rev_{N+1}`; the revision-N rows remain in the table forever, still pointing at a valid (immutable) `wiki_revisions` row. They are **not orphaned and not dangling — they are *invisible***. Every reader without exception filters `WikiClaim.revision_id == page.current_revision_id`, so prior-revision claims become unreachable dead weight. `wiki_claims` and `citations` grow as `O(claims × revisions)` with no GC path **[V/I]**.

**Contrast:** `WikiLink` *attempts* a delete at `wiki_service.py:297-300`:
```python
await self._session.execute(delete(WikiLink).where(WikiLink.src_revision_id == revision_id))
```
`revision_id` was minted at line 267 (`revision_id = uuid7()`), so this DELETE **can never match a row** — it is dead code, and `wiki_links` accumulates identically **[V]**.

### 2.4 `_carry_claims` — the code already knows it has this problem

`packages/aleph-wiki/src/aleph_wiki/curator_service.py:807-856` **[V]**. Its docstring says it outright:

> *"A curator recommit (cross-link, overview recurate, merge body-rewrite) writes a NEW revision, which moves `current_revision_id`. Without carrying the claims forward, the page's structural provenance (the WikiClaim + Citation rows the UI reads by current-revision) would vanish. This preserves it."*

It reconstructs `ClaimDraft`s from text and re-submits them, so `commit_revision` mints **new uuid7 rows again**. Claim "continuity" across curator recommits is therefore **string-identity of `text`**, not row identity. This is both the strongest confirmation of the gap and — usefully — the backfill signal (§4.5).

### 2.5 Mutation of claim state on a committed revision

Two paths *update* `WikiClaim` rows belonging to a revision that is already sealed:

- `packages/aleph-reviewer/src/aleph_reviewer/retraction.py:143-158` **[V]** — sets `claim.confidence = "retracted"`, `claim.status = "contested"`, one `wiki_claim.retract_flag` ledger event **per claim**.
- `apps/api/src/aleph_api/a2ui_handlers.py:668-696` **[V]** — refresh "flag": selects current-revision claims `WHERE confidence != "retracted"` (:674), sets `confidence = status = "contested"` (:682-683), then **one** `wiki.refresh.flag` ledger event for the page carrying a `claims_contested` count (:688-696).

Inconsistent ledger granularity (per-claim vs per-page-with-count) for the same kind of mutation **[V]**.

**And this state is silently lost.** The `confidence="contested"` / `"retracted"` written by these paths survives a *curator* recommit (`_carry_claims` copies `confidence=c.confidence`, `curator_service.py:838`) but is **destroyed** by an agent recompile, which re-extracts claims from scratch at `confidence="cited"` (`agent/workflow.py:417`) or `"cited"/"uncited"` (`synthesis_workflow.py:227`) **[V]**. Revision pressure evaporates on the next ingest of the same source. This is precisely the failure the "web of belief" is meant to prevent, and it is a live defect today, independent of the redesign.

### 2.6 Every reader of `WikiClaim` — exhaustive

| Reader | File:line | Predicate |
|---|---|---|
| Page detail API | `apps/api/src/aleph_api/routes/wiki.py:118-132` | `revision_id == rev.id`; emits `{id, text, confidence, section_anchor}` |
| HTML compile | `apps/api/src/aleph_api/routes/wiki.py:369-373` | `revision_id == rev.id`; emits `{text, confidence}` only |
| Surface retracted-badge | `apps/api/src/aleph_api/routes/surfaces.py:456-473` | `confidence == "retracted" AND WikiClaim.revision_id == WikiPage.current_revision_id` (correlated → **un-indexable**) |
| Surface citation resolve | `apps/api/src/aleph_api/routes/surfaces.py:476-533` | by `claim_id IN (…)`; emits `{marker, claim_id, source_page_id, source_title, url, chunk_ids}` |
| Refresh-reject → contested | `apps/api/src/aleph_api/a2ui_handlers.py:668-684` | current revision, `confidence != "retracted"` |
| Dossier composer | `apps/api/src/aleph_api/a2ui_handlers.py:670-674, 915-916` | queries claims but emits `"claims": []` hardcoded |
| Curator freshness | `packages/aleph-wiki/src/aleph_wiki/curator_service.py:350-392` | current revision → per-claim `Citation` → `SourcePage` → `Source` |
| Curator carry-forward | `curator_service.py:807-856` | current revision |
| Curator merge fold | `curator_service.py:750-782` | dedup by `text` |
| Mechanical citation match | `packages/aleph-reviewer/src/aleph_reviewer/mechanical/workflow.py:114-161` | current revision → build `{marker: Citation}` registry → `verify_citations` |
| Mechanical registry sources | `mechanical/workflow.py:164-179` | 3-join `Source ← SourcePage ← Citation ← WikiClaim WHERE revision_id = :rev` |
| Mechanical DOI pass | `mechanical/workflow.py:398-411` | `select(WikiClaim, Citation).join(...)` |
| Retraction blast radius | `packages/aleph-reviewer/src/aleph_reviewer/retraction.py:74-86` | inverse of the above |
| Wiki refresh contributing sources | `apps/workers/src/aleph_workers/jobs/wiki_refresh.py:143-176` | current revision → Citation → SourcePage → Source |
| A2UI `highlight_claim` | `a2ui_handlers.py:825-832` + `catalog.py:505-511` | by `claim_id` |
| `ReviewFinding.target_claim_id` | `packages/aleph-reviewer/src/aleph_reviewer/models.py:42` | stores a claim id with **no lifecycle guarantee** |
| `UserFeedback` | `aleph_core/feedback.py:21` (`target_kind="claim"`) | stores a claim id |
| Frontend | `apps/web/src/a2ui/components/WikiPageCard.tsx:276-306` | renders `c.text`, `c.confidence`, keyed `wiki-claim-${c.id}` |

**[I] Critical consequence of §2.3 + this table:** `ReviewFinding.target_claim_id`, `UserFeedback.target_id`, `CardAction.target_id`, and the ledger's `wiki_claim.retract_flag` `target_id` all persist claim UUIDs that become **permanently unresolvable on the very next revision of that page**. Analyst feedback on a claim, and every reviewer finding pinned to a claim, is silently orphaned by the next commit. This is a data-loss bug in production today.

---

## §3. Provenance chain granularity — the chain is severed at hop 1

### 3.1 What is actually stored

| Hop | Column | Status |
|---|---|---|
| Claim → chunks | `citations.chunk_ids` JSONB array | **Always `[]`** |
| Claim → source page | `citations.source_page_id` UUID | **Always `NULL`** |
| Claim → marker | `citations.citation_marker` String(16) | The only populated field |
| Chunk → offsets | `document_chunks.char_start/char_end/section_path/ordinal` | Populated |
| Chunk → doc | `document_chunks.normalized_document_id` | Populated |
| Doc → bytes | `normalized_documents.markdown_uri` (asset store) | Populated |
| Doc → version | `normalized_documents.source_version_id`, `.parser`, `.parser_version` | Populated |
| Version → source | `source_versions.source_id`, `.sha256`, `.fetched_at` | Populated |

**Verification of "always `[]` / always `NULL`":** there are exactly three `CitationDraft(...)` construction sites in production code — `agent/workflow.py:420-424`, `synthesis_workflow.py:218-222`, `curator_service.py:840-846` **[V]**. The first two hardcode `chunk_ids=[]` and `source_page_id=None`. The third is a pass-through copy of whatever already exists (i.e. `[]`/`None`). `wiki_service.py:336-337` writes exactly what the draft carries. The only non-empty values in the entire repo are in test fixtures: `tests/e2e/test_retraction.py:114`, `tests/e2e/test_wiki_refresh.py:146`, `packages/aleph-reviewer/tests/test_mechanical_doi_verification.py:149` **[V]**.

The comments are aspirational, not descriptive:
- `agent/workflow.py:421` — `chunk_ids=[],  # Inc 1 binds claims to the source page itself.`
- `agent/workflow.py:422` — `source_page_id=None,  # Set in commit step (self-citation).` — **`commit_revision` does no such thing**; `wiki_service.py:337` passes the `None` straight through **[V]**.
- `synthesis_workflow.py:220` — `source_page_id=None,  # source page resolved by short_id later` — no later resolver exists **[V]**.

### 3.2 `citations.source_page_id` is dual-typed

Read as `source_pages.id`: `retraction.py:76`, `mechanical/workflow.py:169`, `mechanical/workflow.py:411` (`session.get(SourcePage, cite.source_page_id)`), `curator_service.py:380`, `wiki_refresh.py:169` **[V]**.
Read as `wiki_pages.id`: `surfaces.py:503` (`WikiPage.id.in_(source_page_ids)`) and `surfaces.py:513` (`SourcePage.page_id.in_(source_page_ids)`) **[V]**.

Two incompatible id-spaces on one FK-less column. The e2e fixtures disagree too: `test_retraction.py:114` uses `bridge.id` (a `SourcePage` PK), `tests/e2e/test_merge_claim_fold.py:79` uses `src.page_id` (a `WikiPage` id) **[V]**.

### 3.3 Downstream consequences (all **[V]** by reading, **[I]** for the runtime effect)

- `dependent_claims()` (`retraction.py:65-86`) inner-joins on `SourcePage.id == Citation.source_page_id` → **returns `[]` in production**. `retract_source` retracts the source and emits the finding but flags **zero** claims.
- `_registry_sources` (`mechanical/workflow.py:164-179`) → returns `[]`. The mechanical reviewer's DOI/staleness passes have nothing to check.
- `_contributing_sources` (`wiki_refresh.py:143-176`) skips every citation at `:167` (`if cite.source_page_id is None: continue`) → the refresh job finds no sources to re-check.
- `CuratorService._recompute_freshness` (`curator_service.py:374-392`) skips identically at `:378`, so every `ClaimCitation.source_ids` is empty. In `freshness.py`: `_citation_health` → `0` (`:78-81`), `_source_freshness` → `0` (`:88`). **Production freshness is structurally capped at 50/100** (recency + verification only) **[I, from [V] code]**.

### 3.4 Could you reconstruct a verbatim quote today? **No.**

Given a `WikiClaim`, the *only* persisted downward pointer is `Citation.citation_marker` — a string like `"[c1]"` with no registry it resolves against outside the body text of the same revision. Not a chunk, not a source, not an offset.

**But the substrate is present and good.** If `chunk_ids` were populated you could reconstruct verbatim text: `DocumentChunk.text` is stored inline (`aleph_rks/models.py:145`), and `char_start`/`char_end` (`:149-150`) index into the `NormalizedDocument` markdown, fetchable from the asset store via `markdown_uri` (readers at `apps/api/src/aleph_api/routes/sources.py:414`, `apps/workers/src/aleph_workers/jobs/chunk_embed.py:154`) **[V]**. Chunk offsets are produced by `packages/aleph-rks/src/aleph_rks/chunking.py:135, 155, 175` from `_walk_blocks`, which also yields a dotted heading path (`section_path`, `chunking.py:60-63`) **[V]**.

### 3.5 What is missing for quote-level provenance

1. **Sub-chunk offsets.** Chunks are heading-block/sentence-packed (`chunking.py:25-29, 60`), typically many sentences. A claim needs `(char_start, char_end)` *within* the normalized document, not just "chunk 7". Nothing stores this.
2. **The verbatim span itself.** No `quote_text`, no `quote_sha256`. Offsets alone are invalid the moment `parser_version` changes — and `normalized_documents.parser_version` is explicitly versioned (`aleph_rks/models.py:117`), so re-normalization is an expected event that would silently corrupt every stored offset.
3. **Page numbers / physical locators.** `NormalizationResult.structure` carries only `{"page_count": N, "heading_count": 0}` (`packages/aleph-rks/src/aleph_rks/normalization.py:95-97`) **[V]**. The PDF normalizer at `:78-94` joins per-page text with `"\n\n"` and **discards the page boundaries** — there is no `char_offset → page_no` map. Reconstructing "p. 14" from what is persisted is impossible without re-parsing the PDF.
4. **A pinned source version.** `Citation` points (nominally) at a `SourcePage`, which points at a `Source`, whose `current_version_id` moves. A citation therefore floats to whatever version is current rather than pinning the bytes it was read from.
5. **A grounding kind.** No way to say whether the claim is a direct quote, a paraphrase, an aggregation over rows, or derived from other claims.

---

## §4. Migration blast radius

### 4.1 Target shape (what I'd argue for)

- **`claims`** — the stable node. `project_id`, `short_id`, `current_version_id`, `status`, `origin_source_id`, `frame` (`source` | `system` — the layer-1/layer-2 split).
- **`claim_versions`** — immutable, trigger-guarded, mirroring `wiki_revisions`: `claim_id`, `version_no`, `text`, `text_sha256`, `confidence`, `parent_version_id`, `author_kind/id`, `ledger_event_id NOT NULL`. Belief state (contested/retracted) lives on `claims`, not on a rendering.
- **`wiki_claims` survives as the rendering row** — gains `claim_id` + `claim_version_id`, keeps `page_id`/`revision_id`/`section_anchor`, and **loses** `text`/`confidence`/`status` (they move to the claim). This is deliberately additive-then-subtractive so the migration can be staged.
- **`claim_groundings`** replaces `Citation` (§5.2), pinned to `claim_version_id`, one row per span.
- **`claim_edges`** (§5.1).

### 4.2 Tables touched

Direct: `wiki_claims`, `citations`. Indirect (store claim UUIDs that must be re-pointed or re-interpreted): `review_findings.target_claim_id` (`aleph_reviewer/models.py:42`), `user_feedback.target_id` where `target_kind='claim'` (`aleph_core/feedback.py:21`), `card_actions.target_id` (`aleph_a2ui/models.py:72`), `approval_requests.evidence_refs_jsonb` + `review_findings.evidence_refs_jsonb` (`{"kind":"claim","id":…}` per `catalog.py:202-212`), `action_ledger_events` (`target_kind='wiki_claim'` from `wiki_claim.retract_flag`), `source_pages.extracted_claims_jsonb` (a denormalized `{text, marker}` list, `agent/workflow.py:627-635`). Derived: `wiki_pages.freshness` (recompute).

### 4.3 Code blast radius — concrete

**Service methods (11):** `WikiService.commit_revision` (`wiki_service.py:314-341`); `WikiService.get_page` (:359); `CuratorService._recompute_freshness` (`curator_service.py:334-410`); `._carry_claims` (:807-856) — **deleted outright**, this whole mechanism becomes unnecessary; `.apply_merge` claim fold (:748-782); `AliasService.upsert/resolve/repair_broken_links` (§6); `retraction.dependent_claims` (:65-86) + `.retract_source` (:136-161); `mechanical._node_citation_match` (:105-161), `._registry_sources` (:164-179), `._node_doi_verification` (:398-411); `wiki_refresh._contributing_sources` (`wiki_refresh.py:143-176`); `freshness.compute_freshness` + `ClaimCitation` (`freshness.py:43-54, 109-139`).

**API routes (6):** `GET /v1/projects/{id}/wiki/pages/{page_id}` (`routes/wiki.py:118-132`); `GET …/wiki/pages/{id}/html` (`routes/wiki.py:369-373`); `GET …/surfaces/wiki/stream` (`routes/surfaces.py:391-446`, `:449-473`, `:476-533`); `POST …/cards/actions` (`a2ui_handlers.py:825-832`, `:639-697`, `:909-925`); `POST …/sources/{id}/retract` (`routes/sources.py:319-346`); `POST …/feedback` (`routes/feedback.py`).

**A2UI producers + components (6):** `ClaimCard` — `catalog.py:83-111` / `components/cards.py:23-29, 111-122` / `apps/web/src/a2ui/components/ClaimCard.tsx:4-55` / zod `aleph-catalog-v09.tsx:190-205`; `WikiPageCard` — `catalog.py:285-306` / `WikiPageCard.tsx:114-355` (claim list `:276-306`, `Claim`/`Citation` interfaces `:26-38`); `WikiSurface` — `surfaces.py` producer `packages/aleph-a2ui/.../components/surfaces.py:76-102`, renderer `WikiSurface.tsx:29-43, 95-116`; `ApprovalCard` `evidence_refs.kind="claim"` — `catalog.py:202-212` / `ApprovalCard.tsx:28-36`; `FindingCard` — `cards.py:71-78, 192-206`; action `highlight_claim` — `catalog.py:505-511` / `catalog.ts:52` / `CopilotChatSurface.tsx:164-176`. Plus the agent-facing schema in `apps/copilot-runtime/src/server.ts:71-90`. **CI sweep `scripts/check-catalog-roster.sh:34-55` forces renderer+producer to change in the same PR.**

**Reviewers (2):** `MechanicalReviewer` workflow (above); `retraction.py` blast-radius service.

**Worker jobs (4):** `wiki_refresh.py`, `curate.py`, `reviewers.py`, `wiki_ingest.py` (`research.py` currently emits no claims, so it is only in scope if you also fix `research_workflow.py:337`).

**Evals (2):** `packages/aleph-evals/src/aleph_evals/scorers/citation.py` (marker-set precision — would need to score grounding resolution, not marker strings), `scorers/coverage.py`.

**Tests (13):** `tests/e2e/test_retraction.py`, `test_wiki_refresh.py`, `test_merge_claim_fold.py`, `test_cross_link_curate.py`, `test_curator_repair.py`, `test_merge_approve_action.py`, `test_merge_body_rewrite.py`, `test_artifact_drift.py`, `test_permission_leakage.py`, `test_wp4_agent_hands.py`; `packages/aleph-reviewer/tests/test_mechanical_doi_verification.py`; `packages/aleph-wiki/tests/test_freshness.py`, `test_html_compiler.py`; `packages/aleph-a2ui/tests/test_cards.py`. Playwright: `tests/playwright/specs/03-source-to-wiki.spec.ts`, `10-wiki-links-and-scaling.spec.ts` (testids `wiki-claim-${id}`, `claim-confidence`).

### 4.4 Alembic complexity — **low-to-moderate**, lower than it looks

Favourable facts, all **[V]**:
- **No FKs anywhere** → no dependency ordering, no cascade semantics, no `ON DELETE` decisions. You can add and re-point UUID columns freely.
- **`wiki_claims` and `citations` carry no triggers.** They were created in `20260527_1500_inc1_rks_wiki.py:370-402` and **never altered since** — the WP-6 trust-layer migration explicitly declined to touch them (`20260704_1200_wp6_trust_layer.py:12-14`, "no schema change (both are free String columns)"). A backfill `UPDATE` on `wiki_claims` is unobstructed.
- `wiki_revisions` **does** have `wiki_revisions_no_update` / `_no_delete` triggers (`inc1_rks_wiki.py:320-341`) — but the migration doesn't need to update revisions.
- Head is `wp6_trust_layer`; the chain is strictly linear, 16 files, no branches.

Constraints:
- One statement per `op.execute()` (asyncpg extended-query protocol) — noted at `20260530_1000_realtime_notify_triggers.py:34`.
- `alembic check` must be zero-diff, so model + migration land together; the `except ImportError: pass` in `env.py:26-70` will mask an import error as a mass `DROP TABLE`.
- New immutable `claim_versions` needs its own `*_immutable()` trigger pair, copying the pattern at `inc5_reviewers_hypotheses.py:202-222`.

Recommended staging (3 migrations, since existing revisions may never be edited):
1. Create `claims`, `claim_versions`, `claim_edges`, `claim_groundings`; add nullable `wiki_claims.claim_id` / `.claim_version_id`; add the missing indexes (§5.3).
2. Data migration (§4.5) — backfill claims/versions/groundings, populate `wiki_claims.claim_id`.
3. After the read paths cut over: `SET NOT NULL` on `wiki_claims.claim_id`, drop `wiki_claims.text/confidence/status`, drop `citations`.

### 4.5 Backfill — how much history is recoverable?

**Recoverable [I, grounded in [V] code]:**
- **Curator-carried lineages, fully.** `_carry_claims` (`curator_service.py:838`) copies `text` byte-for-byte across recommits, and `apply_merge` dedups on exactly `{c.text for c in ...}` (`:753`). Therefore grouping by `(project_id, page_id, sha256(text))` and ordering by the page's `wiki_revisions.revision_no` reconstructs a genuine claim lineage for every curator recommit — which is the majority of recommits on a mature page.
- **Ordering within a revision.** `wiki_claims.id` is uuid7 (`base.py:25`), so insertion order is recoverable by sorting on `id`. Use ordinal-within-revision as the secondary matching key when text drifted slightly.
- **Timestamps.** `wiki_claims.created_at` plus `wiki_revisions.revision_no`/`created_at` give a total order for `claim_versions.version_no`.
- **Belief state.** Current `confidence`/`status` on the current-revision rows migrate directly onto `claims`.

**Unrecoverable:**
- **Agent-recompile lineages.** When the wiki-ingest agent re-runs on a source, it re-extracts claims from scratch via LLM (`agent/workflow.py:401-426`) with no reference to prior claims. Two revisions' texts for "the same" claim will differ arbitrarily. Text-hash grouping will split these into distinct claims. Fuzzy/embedding matching could be attempted, but **I'd argue against it** — a wrong merge fabricates a false identity in an epistemics system, which is worse than an honest fresh node. Ship exact-hash matching only, and record `provenance: "backfill-exact"` vs `"backfill-new"` on the claim.
- **Grounding.** There is nothing to backfill: `chunk_ids` is `[]` and `source_page_id` is `NULL` in every production row (§3.1). Historical claims get `claim_groundings` rows only insofar as `citation_marker` can be re-resolved against the revision body — which recovers a marker, not a span. **Historical quote-level provenance is genuinely lost.** New grounding starts from the migration forward.
- **Superseded revisions' claim text where a page was never curator-touched** — present as rows but with no reliable link to the current claim.

### 4.6 CLAUDE.md invariants that constrain the design

| Invariant | Where | Constraint on the design |
|---|---|---|
| **Rule 4 — ledger event per mutation, same transaction** | CLAUDE.md:103; enforced by integration tests asserting event counts | Every `claim.version.create`, `claim.edge.assert`, `claim.edge.withdraw` needs an event. Today one `wiki.revision.commit` covers N claims (`wiki_service.py:243`) while `wiki_claim.retract_flag` is per-claim (`retraction.py`) — pick per-claim-version and per-edge, and expect existing count-assertions in `tests/e2e/*` to break. |
| **Rule 6 — every row has `project_id` + `access_scope`; no global tables** | CLAUDE.md:105 | `claims`, `claim_versions`, `claim_edges`, `claim_groundings`, `concepts`, `term_mappings` are all project-scoped. **Cross-project belief sharing is off the table**, and a claim can never be grounded in another project's source. Worth confirming this is intended — a "machine-scale web of belief" that can't span projects is a real limit. (Note the rule is *already* violated by `connectors`, `source_versions`, `eval_datasets`, `eval_cases` — **[V]**, a doc/code contradiction.) |
| **Immutable revisions** | `wiki_revisions` triggers, `inc1_rks_wiki.py:320-341` | Currently only `wiki_revisions.body_md` is immutable; the *claim set* attached to a committed revision is freely mutated (`retraction.py:148`, `a2ui_handlers.py:682`) and even appended to (`curator_service.py:756-782` inserts new claims onto the target's already-committed revision). **Making claims first-class fixes this**: mutable belief moves to `claims`, `wiki_claims` becomes a genuinely immutable rendering row and can take its own trigger pair. |
| **Rule 3 — agent → service only** | CLAUDE.md:102 | New `ClaimService` / `ClaimGraphService` in `aleph-wiki` (or a new `aleph-belief` package) + typed API routes; workers re-enter over `ALEPH_API_INTERNAL_URL` with an agent token. No direct writes from LangGraph nodes. |
| **Rule 8 — no agent-emitted SQL** | CLAUDE.md:107 | Recursive traversal must be service code with a fixed shape and a depth cap. Agents get a `traverse_claim_graph(claim_id, kinds, depth)` tool, never SQL. |
| **Rule 1 — wiki-first retrieval, embeddings only for intra-source descent** | CLAUDE.md:100 | The claim graph must not become a second first-line RAG. Reach it *through* selected pages (page → its claims → edges), or via an explicit belief route — not as a parallel similarity index. |
| **`alembic check` zero-diff; never edit an existing revision** | CLAUDE.md:142, :156 | Three new migrations, not edits. |
| **No `TODO`/`FIXME`/`NotImplementedError` outside tests; no v1/v2 staging** | CLAUDE.md:139-140 | The "resolved later" comments at `agent/workflow.py:422` and `synthesis_workflow.py:220` are the exact pattern this rule bans and they slipped through because they're plain comments. Ship the grounding writer complete. |
| **New package rule** | CLAUDE.md:155 | If `aleph-belief`: add to `[tool.uv.workspace] members`, `[tool.uv.sources]`, ruff/pyright `src`/`include`, then `uv sync`. **[I]** I'd argue against a new package — put it in `aleph-wiki` (which already owns `WikiClaim`/`Citation`) to avoid a DAG change; `aleph-reviewer` and `aleph-research` already depend on `aleph-wiki`. |
| **`scripts/check-catalog-roster.sh`, `check-route-reachability.sh`, `check-docs-drift.sh`** | CLAUDE.md:112-116 | Any new A2UI component needs catalog.py + catalog.ts + renderer + producer in one PR; any new router needs a real caller; `docs/wiki.md` must be updated in the same change. |

---

## §5. Typed claim-to-claim edges

### 5.1 `claim_edges`

```python
class ClaimEdge(CommonColumns, Base):
    __tablename__ = "claim_edges"
    __table_args__ = (
        Index("ix_claim_edges_out", "project_id", "src_claim_id", "edge_kind",
              postgresql_include=["dst_claim_id", "strength"],
              postgresql_where=text("status = 'active'")),
        Index("ix_claim_edges_in", "project_id", "dst_claim_id", "edge_kind",
              postgresql_include=["src_claim_id", "strength"],
              postgresql_where=text("status = 'active'")),
        Index("uq_claim_edges_active", "project_id", "src_claim_id", "dst_claim_id",
              "edge_kind", "frame_source_id", unique=True,
              postgresql_where=text("status = 'active'")),
        CheckConstraint("src_claim_id <> dst_claim_id", name="ck_claim_edges_no_self"),
    )

    project_id:      Mapped[UUID]        = mapped_column(nullable=False, index=True)
    src_claim_id:    Mapped[UUID]        = mapped_column(nullable=False)
    dst_claim_id:    Mapped[UUID]        = mapped_column(nullable=False)
    edge_kind:       Mapped[str]         = mapped_column(String(24), nullable=False)
    # assumes | contradicts | refines | supersedes | supports | entails | restates
    frame:           Mapped[str]         = mapped_column(String(16), nullable=False,
                                                         server_default="system")
    # "source" = layer 1 (the edge the paper itself asserts, in its own vocabulary)
    # "system" = layer 2 (Aleph's own belief about the relation)
    frame_source_id: Mapped[UUID | None] = mapped_column(nullable=True)  # required when frame='source'
    src_version_id:  Mapped[UUID | None] = mapped_column(nullable=True)  # pin the texts the
    dst_version_id:  Mapped[UUID | None] = mapped_column(nullable=True)  #   edge was asserted about
    strength:        Mapped[float]       = mapped_column(Float, nullable=False, server_default="1.0")
    rationale:       Mapped[str]         = mapped_column(Text, nullable=False, server_default="")
    grounding_id:    Mapped[UUID | None] = mapped_column(nullable=True)  # ground the EDGE itself
    asserted_by_kind:Mapped[str]         = mapped_column(String(16), nullable=False)
    status:          Mapped[str]         = mapped_column(String(16), nullable=False,
                                                         server_default="active")
    # active | withdrawn | superseded
    withdrawn_at:    Mapped[datetime | None]
    withdrawn_by:    Mapped[UUID | None]
```

Design notes I'd defend:
- **`frame` is the load-bearing column.** It is what keeps "what the paper asserts" (layer 1) distinct from "what Aleph believes" (layer 2) *at the edge level*, not just the claim level. Without it, the first reviewer that promotes a source-frame contradiction into system belief has silently normalized.
- **Edges are versioned by withdrawal, not by mutation.** No `UPDATE` on an active edge except `status`/`withdrawn_*`; a changed strength is a new row. This keeps the ledger honest and lets you replay belief state at a timestamp. Consider a `claim_edges_immutable_except_status` trigger — the repo's five existing `*_immutable()` trigger pairs are the precedent (`inc1_rks_wiki.py:320`, `inc4_a2ui.py:96`, `inc5_reviewers_hypotheses.py:202`, `inc6_datasets.py:111`, `inc7_artifacts.py:117`).
- **The `CheckConstraint` would be the repo's first** (`grep CheckConstraint` → zero hits **[V]**). I'd add it anyway and also add CHECKs on `edge_kind`/`frame`/`status`; the absence of any value constraint is why `ClaimCardProps.confidence` can default to `"cited"` (`components/cards.py:27`) which isn't in the catalog enum (`catalog.py:88-95`) **[V]** — a class of bug the schema currently cannot catch.
- **Directionality.** `contradicts` is semantically symmetric but store it directed and let the traversal union both index directions; a symmetric-pair constraint is expensive and the `ix_claim_edges_in` index makes reverse lookup free.

### 5.2 `claim_groundings` — replacing `Citation`

```python
class ClaimGrounding(CommonColumns, Base):
    __tablename__ = "claim_groundings"

    project_id:             Mapped[UUID]
    claim_version_id:       Mapped[UUID]        # pinned to exact text, not to the claim
    grounding_kind:         Mapped[str]         # quote | paraphrase | aggregate | derived | asserted
    # --- leaf grounding (source spans) ---
    source_id:              Mapped[UUID | None] # DENORMALIZED: kills the SourcePage bridge hop
    source_version_id:      Mapped[UUID | None] # pins the exact fetched bytes
    normalized_document_id: Mapped[UUID | None]
    chunk_id:               Mapped[UUID | None] # ONE chunk per row, not a JSONB array
    doc_char_start:         Mapped[int | None]  # offsets into the normalized markdown
    doc_char_end:           Mapped[int | None]
    quote_text:             Mapped[str]         # MATERIALIZED verbatim span
    quote_sha256:           Mapped[str]         # self-verifying against re-normalization
    locator_jsonb:          Mapped[dict]        # {page_no, section_path, heading, bbox}
    parser_version:         Mapped[str | None]  # offsets are only valid for this parser
    # --- derived grounding (claim-on-claim) ---
    via_claim_id:           Mapped[UUID | None]
    citation_marker:        Mapped[str]         # keep for [cN] rendering compatibility
```

Three changes I'd argue hardest for:
1. **`quote_text` + `quote_sha256` materialized.** Offsets alone are a time bomb given that `parser_version` is explicitly a versioned field (`aleph_rks/models.py:117`). Storing the span makes provenance survive re-normalization and makes the grounding tree renderable without an asset-store round trip.
2. **One row per chunk**, replacing the JSONB array. `citations.chunk_ids` is a JSONB array with **no GIN index** — "which claims cite chunk X" is currently a full table scan (**[V]**, confirmed in the index inventory). A row-per-span makes it a b-tree lookup.
3. **Denormalize `source_id`.** The `Citation → SourcePage → Source` bridge is the exact hop that is broken in production (§3.2) and it imposes a hard 1:1 Source↔Page constraint (`source_pages.source_id` and `.page_id` are *separately* UNIQUE, `inc1_rks_wiki.py:404-417`) **[V]**. A claim citing a source that has no wiki page is currently unrepresentable. Store `source_id` directly and keep `SourcePage` for what it is — a page-kind bridge, not a provenance hop.

### 5.3 Do recursive CTEs suffice? **Yes, with caveats.**

Postgres 18 (`pgvector/pgvector:0.8.2-pg18`, `deploy/compose/docker-compose.yml:13`) **[V]**, so `WITH RECURSIVE … CYCLE d SET is_cycle USING path` and `SEARCH BREADTH FIRST BY` are available. Cycle detection is a first-class clause, which matters because `assumes` chains and `contradicts` pairs will cycle.

**But two of the three traversals aren't recursive at all:**
- **Grounding tree (leaf):** `claim → claim_groundings → document_chunks → normalized_documents → source_versions → sources`. Fixed 5-hop join. No recursion needed. This is the common case and should be a plain join, kept fast.
- **Derivation tree:** `claim → grounding(via_claim_id) → claim → …`. Recursive, but shallow.
- **Revision-pressure propagation:** `claim →(assumes|refines)* → claim`, reverse direction. Recursive, potentially wide.

Recommended shape: `sqlalchemy` `.cte(recursive=True)` rather than `text()`, so it stays ORM-native and pyright-strict-checkable. Hard-cap depth (`WHERE depth < 8`) *and* rely on the `CYCLE` clause — belt and braces. **[V]** The repo has **zero** recursive CTEs, zero CTEs, zero `union_all`, and only three trivial `text()` uses (`index_service.py:185`, `curator_service.py:564`, `routes/health.py:27`) — so this is stylistically novel and worth a doc note in `docs/wiki.md`.

**Indexes the CTE needs** (the two partial covering indexes in §5.1 make each recursion step index-only). Also fix the pre-existing gaps found in the sweep, all **[V]**:
- `wiki_links.src_revision_id` — **unindexed**, yet filtered in four hot paths (`index_service.py:71`, `routes/wiki.py:138`, `mechanical/workflow.py:359`, and the per-commit DELETE at `wiki_service.py:299`). Seq-scans the edge table on every page read.
- `wiki_claims.revision_id` — **unindexed**, filtered by every single reader in §2.6.
- `citations.source_page_id` — **unindexed**; harmless only because it's always NULL.
- `wiki_links` has no `(project_id, dst_page_id)` composite for the merge backlink query (`curator_service.py:676-680`).
- `aliases.canonical_page_id` — unindexed, no reverse lookup.
- **`surfaces.py:468`'s `WikiClaim.revision_id == WikiPage.current_revision_id`** is a correlated cross-table comparison and is **inherently un-indexable**. Moving belief state onto `claims` and adding `wiki_claims.is_current` (or better: querying `claims` directly) removes it.

**Existing graph patterns to follow:** the 1-hop wikilink expansion at `packages/aleph-assistant/src/aleph_assistant/retrieval/router.py:372-431` (single `IN`-list query, Python-side dedup and truncation — note it fetches *all* rows then truncates in Python, no SQL `LIMIT`), and the "two queries, no N+1" `IN`-list batching idiom at `routes/surfaces.py:476-533` which is the house style for pointer-following without FKs.

---

## §6. Concept / mapping layer

### 6.1 What exists — and why it is the anti-pattern

`ExtractedConcept` (`packages/aleph-wiki/src/aleph_wiki/agent/workflow.py:74-79`) **[V]**: `canonical_name`, `surface_forms[]`, `confidence`, `salience`, `definition_hint`. **It is never persisted as a row.** It fans out into exactly two things:

1. **`Alias` rows** — `_node_alias_extraction` (`workflow.py:271-336`) upserts `surface_form → canonical_name` pairs, dropping any with `confidence < 0.5` (:317), plus a second pass over `concept.surface_forms` (:324-334).
2. **A stub `WikiPage`** — `_node_topic_page_stubs` (`workflow.py:481-518`) mints one page per concept, `title = c.canonical_name`, `slug = _slugify_concept(...)`, `page_kind="stub"`, `claims=[]`.

`salience` and `definition_hint` are discarded on persist (`definition_hint` only reaches the LLM prompt at `:487` and the fallback body at `:522`) **[V]**.

**The structural blocker is one line:** `aliases` carries `UniqueConstraint("project_id", "surface_form")` (`aleph_wiki/models.py:163`) **[V]**. One surface form → exactly one canonical name per project. Two papers using "alignment" or "robustness" differently **cannot coexist in the schema**. And `AliasService.upsert` (`alias_service.py:50-55`) resolves the conflict by overwriting: `existing.canonical_name = canonical_name; existing.canonical_page_id = ...; existing.confidence = max(existing.confidence, confidence)` — last-writer-wins, with the confidence *ratcheting up* so a low-confidence overwrite inherits the prior high confidence. **[V]** That is silent normalization implemented as a max() — literally the thing the target vision forbids.

Two more gaps: `Alias` carries **no source/author attribution** (which paper used this term this way?) and **no purpose** (is this mapping for retrieval, for aggregation, or a deliberate contrast?). `AliasService.resolve` (`:88-112`) is single-step — no alias→alias chain following — and falls back to exact `WikiPage.title` match, so wiki page naming *is already* the de-facto concept namespace.

`RejectionFeedback.concept_name` (`models.py:199`) is a **`String(512)` keyed by name**, and the ingest agent looks it up as the literal string `f"Source:{state['source_short_id']}"` (`workflow.py:368`) **[V]** — concept identity is a string in yet a third place.

**Distance from what's needed: large but shallow.** `Alias` is a working many-to-one string table with ledger integration (`wiki.alias.upsert`) and a repair job. It is roughly 20% of a concept layer: it has the surface-form→canonical edge and nothing else — no concept node, no frame, no purpose, no relation type, no grounding, and a UNIQUE constraint that structurally forbids the interesting cases.

### 6.2 Proposed `Concept` + `TermMapping`

```python
class Concept(CommonColumns, Base):
    __tablename__ = "concepts"
    __table_args__ = (UniqueConstraint("project_id", "slug", name="uq_concepts_project_slug"),)

    project_id:     Mapped[UUID]
    canonical_name: Mapped[str]         = mapped_column(String(512))
    slug:           Mapped[str]         = mapped_column(String(512))
    definition_md:  Mapped[str]         = mapped_column(Text, server_default="")
    page_id:        Mapped[UUID | None]  # OPTIONAL 1:1 rendering page — nullable both ways
    frame:          Mapped[str]         = mapped_column(String(16), server_default="system")
    salience:       Mapped[float]
    status:         Mapped[str]         = mapped_column(String(16), server_default="active")


class TermMapping(CommonColumns, Base):
    __tablename__ = "term_mappings"
    __table_args__ = (
        UniqueConstraint("project_id", "concept_id", "surface_form",
                         "frame_kind", "frame_source_id", "purpose",
                         name="uq_term_mapping_frame_purpose"),
        Index("ix_term_mappings_lookup", "project_id", "surface_form", "purpose"),
        Index("ix_term_mappings_by_source", "project_id", "frame_source_id"),
    )

    project_id:      Mapped[UUID]
    concept_id:      Mapped[UUID]
    surface_form:    Mapped[str]         = mapped_column(String(512))
    frame_kind:      Mapped[str]         = mapped_column(String(16))
    # source | author | project | system
    frame_source_id: Mapped[UUID | None]  # the Source whose vocabulary this is
    frame_author:    Mapped[str | None]   = mapped_column(String(512))
    purpose:         Mapped[str]          = mapped_column(String(64))
    # retrieval | aggregation | contrast | display | disambiguation
    relation:        Mapped[str]          = mapped_column(String(16))
    # same_as | narrower | broader | overlaps | false_friend | translates
    confidence:      Mapped[float]
    rationale:       Mapped[str]          = mapped_column(Text, server_default="")
    grounding_id:    Mapped[UUID | None]  # ground the MAPPING in a source span
    status:          Mapped[str]          = mapped_column(String(16), server_default="active")
```

**The whole design turns on dropping `UNIQUE(project_id, surface_form)`.** The new uniqueness key is `(concept, surface_form, frame, purpose)` — so "alignment" can map to `Concept:ValueAlignment` under Source A's frame for `purpose=retrieval` **and** to `Concept:RepresentationAlignment` under Source B's frame for the same purpose, simultaneously, with `relation="false_friend"` edges between the two concepts. `grounding_id` means the mapping itself is defensible: you can show *the sentence in the paper* that licenses the mapping. That is the difference between a purpose-relative mapping and a normalization.

### 6.3 Should a concept BE a page? — **No. Related 1:0..1, page-side optional.**

De facto today it is (`workflow.py:481-518` mints a page per concept, and `AliasService.resolve` falls back to `WikiPage.title`) **[V]**. I'd argue for separating them:

- **Identity would be name-keyed.** `wiki_pages` has `UNIQUE(project_id, slug)` (`models.py:37`); a concept whose canonical name is refined would need a new slug, and either breaks inbound `[[wikilinks]]` or forces an alias hack.
- **`PageMergeProposal` becomes a concept merger by accident.** `CuratorService.apply_merge` (`curator_service.py:660-800`) soft-deletes the source page, aliases its title to the target, folds claims by text-dedup, and rewrites inbound bodies — all on a `similarity` score. If concept ≡ page, the curator can *silently merge two distinct concepts* on a cosine threshold. That is the precise failure mode the vision names.
- **`page_kind` is already overloaded** — `topic | source | synthesis | stub | overview` (`wiki_service.py:40` + `bootstrap.py:213`), and pages are the *rendering* surface with an HTML compiler, freshness score, hand-edit marks, and revision history. Concepts need none of that; they need frames, mappings, and grounding.
- **A source-frame concept may have no page at all** — "what this author means by X" is a real node with no business appearing in the wiki index.

So: `concepts.page_id` nullable, `wiki_pages` unchanged, and the wiki page becomes *a rendering of a concept* exactly as `wiki_claims` becomes *a rendering of a claim*. That symmetry is the right shape — the wiki stays the retrieval surface (CLAUDE.md rule 1 intact) while identity lives underneath it.

**Migration path from `Alias`:** every existing `Alias` row backfills to a `TermMapping` with `frame_kind="project"`, `frame_source_id=NULL`, `purpose="retrieval"`, `relation="same_as"`, `confidence` carried over, plus a `Concept` per distinct `canonical_name` linked to `canonical_page_id`. `AliasService.resolve` becomes a `TermMappingService.resolve(surface_form, purpose, frame)` with the old signature as the `purpose="retrieval", frame="project"` special case, so `repair_broken_links` and the wikilink resolver keep working unchanged.

---

## §7. Doc/code contradictions and defects found

**Doc/code contradictions:**
1. `docs/wiki.md:51` and `docs/implementation-log.md:1785` describe the retraction blast-radius join as working. It returns `[]` in production because `citations.source_page_id` is never populated (§3.1). `implementation-log.md:1874-1877` admits the gap for the *research/synthesis* path only ("writes `Citation`s but not the `SourcePage` bridge rows") — but the same hole exists on the **ingest** path, where the bridge row *is* written (`agent/workflow.py:620-640`) and the citation still points at `None` (`:422`). The e2e proof (`tests/e2e/test_retraction.py:103-128`) hand-builds the bridge *and* hand-passes `source_page_id=bridge.id`, so it never exercises the production writer. **The "proven end-to-end where the bridge exists" claim is not supported by the code.**
2. `docs/wiki.md:31` — citation health is "fraction of the page's claims that are cited (resolvable `Citation` → non-retracted `Source`)". In production this is always 0, capping freshness at 50 (§3.3).
3. CLAUDE.md:105 / architecture.md:58 — "Every row carries `project_id`". Violated by `connectors`, `source_versions`, `eval_datasets`, `eval_cases` **[V]**.
4. CLAUDE.md:74 — "`aleph-core` … LEAF — imports nothing else." `aleph_core/agent_memory.py:26` and `aleph_core/feedback.py:17` both `from aleph_db.base import Base` **[V]**. The comment at `agent_memory.py:22-25` acknowledges it ("We avoid importing aleph_db here so aleph_core stays leaf-level") immediately above the import that does it.
5. `wiki_service.py:297` comment "Replace WikiLink rows for `src_revision_id` with the new ones" describes a DELETE that cannot match (§2.3).

**Defects worth fixing regardless of the redesign:**
- **P0 — claim-referencing rows orphan on every commit.** `ReviewFinding.target_claim_id`, `UserFeedback.target_id`, `CardAction.target_id`, and `wiki_claim.retract_flag` ledger targets all dangle after the next revision (§2.6). Analyst feedback on claims is being silently lost today.
- **P0 — contested/retracted state is erased by the next agent recompile** (§2.5).
- **P1 — citation marker bracket mismatch, user-visible.** `Citation.citation_marker` is stored *with* brackets (`synthesis_workflow.py:213`, `agent/workflow.py:411` default `"[c?]"`), `surfaces.py:525` passes it through, `WikiPageCard.tsx:154` keys `citationMap` on `"[c1]"` — but `WikiBodyMarkdown.tsx:27`'s capture group yields `"c1"` without brackets. **Every citation popover in the UI falls through to the "No citation resolved" branch** (`WikiPageCard.tsx:106`).
- **P1 — `wiki_claims` / `wiki_links` / `citations` grow unboundedly** with no GC and no index on the column every reader filters on.
- **P2 — three divergent `confidence` enums**: `catalog.py:88-95` (`well-supported|weakly-supported|contested|uncited|retracted`), `copilot-runtime/src/server.ts:80` (adds `initial`, drops `weakly-supported`/`retracted`), `components/cards.py:27` (defaults to `"cited"`, which is in *none* of them and would fail `validate_component`).
- **P2 — `agent/workflow.py:610`** does `session.get(SourcePage, result.page_id)` — a PK lookup on `source_pages` using a *wiki page* id. Always `None`; benign only because of the correct fallback at `:616`.
- **P2 — `CuratorService.apply_merge` is an unbounded N+1**: one full `commit_revision` (new immutable revision + section reparse + link rewrite + ledger event) per inbound-linking page (`curator_service.py:698-714`).

**Ordering I'd recommend:** fix the grounding writer first (populate `chunk_ids` + a real `source_id` at the three `CitationDraft` sites), because it is a ~50-line change that instantly un-breaks freshness, retraction, refresh, and mechanical review — *and* it is the thing that makes the claim-identity migration worth doing. Stable claim nodes with vacuous provenance would be a web of belief with nothing underneath it.agentId: a3cae9217694b07af (use SendMessage with to: 'a3cae9217694b07af', summary: '<5-10 word recap>' to continue this agent)
<usage>subagent_tokens: 207569
tool_uses: 52
duration_ms: 925978</usage>