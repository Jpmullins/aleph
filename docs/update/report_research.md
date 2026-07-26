I have everything. Writing the report.

---

# Aleph — Deep Technical Analysis for a Claim-Centric "Web of Belief"

**Legend:** ✅ = verified by reading the code · ⚙️ = verified by executing code · 🔶 = inferred · ⚠️ = doc/code contradiction. All paths absolute, all line numbers exact.

---

## 0. Headline findings

1. **Quote-level provenance is not merely absent — the substrate that would support it is numerically wrong.** `DocumentChunk.char_start/char_end` exist (`/home/claude/aleph/packages/aleph-rks/src/aleph_rks/models.py:149-150`) but do **not** index the normalized markdown. ⚙️ Verified by execution: `markdown[char_start:char_end] != chunk.text` for 7/12 chunks of the repo's own README at `target_tokens=120`, and 8/8 chunks of a line-wrapped synthetic paper, with monotonically accumulating drift (−1, −4, −7, −10, −14, −17, −20, −23 chars). Root cause: `chunk_markdown` stores `char_end = buf_start + len(text)` where `text = " ".join(buf).strip()` is a **whitespace-normalized re-join, not a source substring** (`chunking.py:129,136`, `:149,156`, `:169,176`), and re-bases the next chunk with a normalized-space heuristic `buf_start = buf_start + len(text) - len(" ".join(buf))` (`:144`, `:164`).
2. **Claims exist in production, but only from one prompt, and every claim's provenance is `NULL`.** The only production claim producer is `_node_source_page_compose` (`/home/claude/aleph/packages/aleph-wiki/src/aleph_wiki/agent/workflow.py:394-427`). Every `CitationDraft` it emits has `chunk_ids=[]` and `source_page_id=None` (`:421-422`), and `confidence` is hardcoded `"cited"` (`:417`) with no validation. The research→synthesis path emits **zero** claims — `build_report` hardcodes `claims=[]` (`/home/claude/aleph/packages/aleph-research/src/aleph_research/research_workflow.py:337`).
3. **The retraction blast-radius, freshness citation-health, and refresh contributing-sources machinery all join through `Citation.source_page_id`** (`/home/claude/aleph/packages/aleph-reviewer/src/aleph_reviewer/retraction.py:74-79`) — which is `None` for every production row. This entire trust layer is exercised only by e2e test seeds.
4. **Tiering exists but only in one place** — the `research.triage` Haiku call (`research_workflow.py:683-691`). Post-registration, **every** document takes an identical unconditional path: `normalize_job → {chunk_embed_job, wiki_ingest_job}` (`/home/claude/aleph/apps/workers/src/aleph_workers/jobs/normalize.py:193-194`), with 5–25 sequential LLM calls per source in `wiki_ingest`.
5. **The sentence splitter shatters on scientific abbreviations.** ⚙️ `_SENTENCE_END = r"(?<=[.!?])[\"')\]]?\s+(?=[A-Z0-9])"` (`chunking.py:29`) — the `[A-Z0-9]` lookahead splits `"Fig. 3"` → `["Fig.", "3 the effect holds."]`, `"Smith et al. 2020"` → `["Smith et al.", "2020 report..."]`, `"vs. 5%"`, `"Eq. 2"`. Verified by execution. For a Claimify-style pipeline whose *select* stage is sentence-atomic, this is load-bearing.
6. **No "web of belief" concept exists anywhere.** ✅ Repo-wide grep for `web of belief|source frame|layer 1|terminology map|purpose-relative` returns only unrelated hits in `docs/archive/`. Layer-1/layer-2 separation, term alignment, and epistemic-status computation are **greenfield**.

---

## 1. Research loop, node by node

`/home/claude/aleph/packages/aleph-research/src/aleph_research/research_workflow.py` (979 lines). Graph construction `:955-971`.

```
START → plan → search → ingest → reflect ─(route_after_reflect :844)→ search
                                        └→ compose → synthesize → END
```

### 1.1 Node table (all ✅)

| Node | fn | Capability | purpose | json | max_tok | temp | Inputs → Outputs |
|---|---|---|---|---|---|---|---|
| plan | `:484` | `SYNTHESIS` | `research.plan` | ✓ | 1200 | 0.2 | `topic` → `subqueries[≤6]`, `iteration` |
| search | `:505` | — (**no LLM**) | — | — | — | — | `subqueries` → `candidates`, `seen_keys` |
| ingest | `:665` | `CLASSIFICATION` | `research.triage` | ✓ | 400 | 0.2 | `candidates` → `ingested`, `last_ingested_count` |
| reflect | `:806` | `SYNTHESIS` | `research.reflect` | ✓ | 600 | 0.2 | `ingested` → `research_done` \| `subqueries`+`iteration` |
| compose | `:849` | `SYNTHESIS` | `research.compose` | ✗ | 4000 | **0.3** | `ingested` → `report` |
| synthesize | `:882` | — (delegates) | — | — | — | — | `report` → `committed_*_ids`, `proposal_ids` |

Shared LLM helper `_chat` `:437-464` — routes through `LiteLLMClient.chat()` with `profile_bindings`, so rule 5 (ModelCall + CostLedgerEvent) holds. ✅

Under `aleph-dev` and `aleph-production` alike, `CLASSIFICATION` → `claude-haiku-4-5` (`/home/claude/aleph/apps/api/alembic/versions/20260527_1200_inc0_initial.py:76`, `:142`). ✅ The triage tier is genuinely cheap; the deep tier is Sonnet/Opus.

### 1.2 Prompts (verbatim, ✅)

`_PLAN_SYS` `:401-409`, `_TRIAGE_SYS` `:411-416`, `_REFLECT_SYS` `:418-424`, `_COMPOSE_SYS` `:426-434`. The compose prompt is the only one that mentions claims:

> "…Cite sources inline with [cN] markers exactly matching the numbered source list you are given…; **cite every substantive claim**; never invent markers beyond the list…"

It asks for markers on claims but **never requests a structured claim list** — consistent with `claims=[]`.

### 1.3 Loop bounds, plateau, termination

`should_stop` `:281-293` (✅) — three independent cutoffs, evaluated at the top of `reflect` `:814-820`:
- `last_ingested_count <= 0` → **plateau cutoff**, stops regardless of iteration.
- `iteration + 1 >= limits.max_iterations`.
- `total_ingested >= limits.max_total_sources`.

Limits from `ResearchLimits` `:102-106` (defaults 3/6/15), overridden per-run in `jobs/research.py:169-177` from settings; `depth=="shallow"` → `max_iterations=1`.

Additional caps: `_SEARCH_RESULTS_PER_TOOL=5` `:89`, `_EXPANSION_SEED_SOURCES=2` / `_EXPANSION_LIMIT=5` `:90-91`, `MAX_FETCH_BYTES = 10 MiB` `:87` (enforced `:732`).

### 1.4 Failure semantics — degradation is silent everywhere

- `_loads_lenient` `:192-215` — direct parse → first `{…}` span → `{}`. Docstring `:195-196`: *"The gateway does not reliably honor `response_format`."* ✅
- `parse_plan` `:239-240` → one topic-wide subquery. `parse_reflect` `:247-248` → **done**. `parse_triage` `:278` → **first N candidates** (i.e. a malformed triage response degrades to *no triage at all*, ingesting the head of the candidate list). ✅
- Per-tool search failure is caught and logged, loop continues `:532-539`. Per-candidate fetch failure likewise `:724-731`.
- `sanitize_markers` `:296-307` strips `[cN]` where `N ∉ [1, n_sources]` **before** `SynthesisWorkflow`'s hard citation gate — so the gate at `/home/claude/aleph/packages/aleph-wiki/src/aleph_wiki/synthesis_workflow.py:196-204` is effectively unreachable from the research path. ✅
- The only hard failure inside the graph: `compose` raises `RuntimeError("research ingested no sources; nothing to compose")` `:856-858`.
- **Zero retries on any node.** ✅

Job-level (`/home/claude/aleph/apps/workers/src/aleph_workers/jobs/research.py`):
- Retry guard `:96-108` — `status != "pending"` at entry → mark `failed`, return. Prevents duplicate ingestion on arq re-enqueue.
- Terminal re-delivery is an idempotent no-op `:91-95`.
- `asyncio.CancelledError` `:205-218` → best-effort `failed`, re-raise.
- Generic exception `:219-222` → `failed` with `error_text[:4096]`.
- Dispatch commits the run **before** enqueue (`dispatch.py:115-119`), with a redis-failure compensator `:123-131`. ✅ The no-strand claim holds.

### 1.5 Tool binding + allowlist enforcement ✅

`/home/claude/aleph/packages/aleph-research/src/aleph_research/tools.py`:
- `RESEARCH_CONNECTOR_FACTORIES` `:57-66` — 8 kinds, **factories not instances**.
- `effective_enabled_kinds` `:106-123` — three-stage filter: (a) `kind ∈ registered`, (b) explicit `ConnectorBinding.enabled` beats `Connector.enabled_by_default` (`:117`), (c) run-scoped `allowed` list (`:120-121`).
- `resolve_bound_tools` `:176-225` — the enforcement point. `facs[kind]()` is called **only inside the loop over resolved kinds** (`:224`). A disallowed connector is never constructed. ✅
- Credentials decrypt in-process via `ConnectorCredentialService.decrypt_for_callback` `:209-213`; failure → skip + `on_skip` callback, never fatal `:214-223`.
- Dev env fallback `dev_credential_defaults` `:82-86` is gated on `auth_mode == "local"`.
- Double enforcement: `dispatch.py:67-69` resolves `enabled` at dispatch time and pins it into `AgentRun.input_payload["allowed_connectors"]` `:89`; the worker re-resolves against that list (`jobs/research.py:112-115, 156`). ✅

⚠️ **Doc/code contradiction:** `docs/research-loop.md:28` says *"The search node emits a `research.tools` agent-event listing the bound kinds."* It is emitted in the **job**, before the graph runs (`jobs/research.py:159-167`), not in the search node.

Scholar is bound *inside* the graph, not through the tool map, and is gated on the `openalex` binding: `if "openalex" in ctx.tools_by_kind` at `:553` and `:571`. ✅ Consensus is not bound (matches doc).

### 1.6 Progress events ✅

`@with_phase(name, ctx_getter=lambda: _ctx())` on all six nodes (`:483, 504, 664, 805, 848, 881`). Implementation `/home/claude/aleph/packages/aleph-db/src/aleph_db/repos/agent_events.py:170-197` → `phase()` `:100-167` → writes `AgentEvent` rows (`phase_started` / `phase_completed` / `phase_failed`) each in **its own short-lived session with immediate commit** (`:136-143`, `:148-155`, `:159-167`) so the SSE poller sees them without coupling to the node's data transaction. `agent_run_id is None` → no-op pass-through (`:129-131`).

Two out-of-band events are emitted as synthetic `phase_completed` with `duration_ms=0`: `research.tool_skipped` (`jobs/research.py:141-147`) and `research.tools` (`:160-166`).

---

## 2. Ingestion pipeline

### 2.1 Trace of one document ✅

```
connector.search()  → list[ConnectorResult]{external_id,title,url,snippet,metadata}
                                      base.py:47-53
connector.fetch()   → RawPayload{data,mime_type,sha256,extension,declared_metadata}
                                      base.py:56-62
register_uploaded_source()            source_service.py:42-161
   asset_store.put_source_asset()     :70-76
   SourceAsset  ────────────────────  :80-90
   Source(status="normalizing")       :93-113   ← source_metadata merged at :100-106
   SourceVersion(version_no=1)        :116-130
   ledger source.create + source_version.create  :133-159
_kick_normalize()  → AgentRun(normalizer) + agent token
                                      research_workflow.py:593-646
enqueue normalize_job                 research_workflow.py:779
────────────────────────────────────────────────────────────────
normalize_job                         jobs/normalize.py:74-200
   asset_store.get(uri, expected_sha256=...)     :124
   normalize_bytes(data, mime)                   :125
   put_normalized_markdown()                     :142-147
   NormalizedDocument                            :148-163
   Source.status="normalized"                    :168
   ledger normalization.completed                :170-186
   enqueue chunk_embed_job AND wiki_ingest_job   :193-194   ← unconditional fan-out
────────────────────────────────────────────────────────────────
chunk_embed_job                       jobs/chunk_embed.py:39-265
   dim guard (zero-spend reject)                 :126-177
   chunk_markdown(markdown)                      :156
   embed_texts(batch=64)                         :180-188
   DocumentChunk rows                            :193-210
   RetrievalIndexRecord upsert                   :212-237
   Source.status="indexed"                       :242-243
```

### 2.2 Parameters (all ✅)

| Knob | Value | Cite |
|---|---|---|
| chunk target | `target_tokens=512` | `chunking.py:106` |
| chunk overlap | `overlap_tokens=64` | `chunking.py:107` |
| tokenizer | `cl100k_base` | `chunking.py:109` |
| embed model | `titan-embed-v2` | `20260527_1200_inc0_initial.py:85, :151` |
| embed dims | `1024` (`EMBEDDING_DIM`) | `aleph-rks/models.py:34, :147` |
| embed batch | `64`, serial loop, no gather | `embedding.py:87, :99-113` |
| vector index | HNSW `m=16, ef_construction=64, vector_cosine_ops` | `20260527_1500_inc1_rks_wiki.py:215-222`; mirrored `models.py:130-136` |
| `hnsw.ef_search` | **never set** → pgvector default 40 | 🔶 grep: no hits repo-wide |
| FTS | GIN on `text_tsv`, `to_tsvector('english',…)` via BEFORE-trigger | `…inc1_rks_wiki.py:223-246` |

⚠️ The tokenizer is `cl100k_base` but the embedder is Amazon Titan. `token_count` is an approximation of the actual embedder's budget. 🔶

### 2.3 What metadata survives — and what dies

**Survives to `DocumentChunk`** (`models.py:124-152`): `ordinal`, `text`, `text_tsv`, `embedding`, `section_path` (dotted heading hierarchy, `chunking.py:79`), `char_start`, `char_end`, `token_count`, `embedder_model`.

**Does not survive:**
- **Page numbers.** `PyPDFNormalizer` iterates pages and joins with `"\n\n"` (`normalization.py:81-94`) — the page boundary is destroyed. `structure_jsonb` retains only a scalar `page_count` (`:95-100`). `PDFMinerNormalizer` counts `\f` (`:136`) but the form-feeds are then collapsed by `_canonicalize_text`. ✅
- **Section structure beyond a slug string.** `heading_count=0` hardcoded for both PDF paths (`:97`, `:137`) — pypdf can't expose it. `section_path` is a slugified `" > "` join, not a structured tree (`chunking.py:79`, `_slugify` `:42-44` truncates to 96 chars).
- **Table/figure structure.** `table_count=0`, `figure_count=0` for PDFs (`:98-99`).
- **`DocumentChunk` has no time column** — it extends bare `Base`, not `CommonColumns` (`models.py:124`). ✅ No age-based pruning is possible without joining back through `normalized_documents`.
- **Rich connector metadata is dropped between `search` and `Source`.** ArXiv's `search` emits `{arxiv_id, primary_category, doi, published, authors, summary}` (`arxiv/register.py:88-95`) and `fetch` re-attaches it as `declared_metadata` (`:115-119`) — but `register_uploaded_source` **never reads `RawPayload.declared_metadata`**; the research loop passes only `cand.metadata` (`research_workflow.py:741, 766`). ✅

### 2.4 Character-level positions: present but wrong ⚙️

The invariant a quote-level system needs — `normalized_markdown[char_start:char_end] == chunk.text` — **does not hold**.

Executed against the repo's own `README.md`:
```
target=120  chunks=12  drifted=7
target=512  chunks=7   drifted=2
```
Executed against a synthetic line-wrapped (pypdf-shaped) paper, 15,776 chars:
```
ord=0 stored_start=8     true_start=9     drift=-1
ord=3 stored_start=6404  true_start=6414  drift=-10
ord=7 stored_start=14932 true_start=14955 drift=-23    ← monotonic accumulation
```
Slice at stored offsets for ord=3: `'er study. Unique\nclaim 0079 asserts…'` vs actual chunk text `'Unique\nclaim 0079 asserts…'`.

Three compounding causes, all ✅:
1. `_split_sentences` `:47-52` strips each fragment and the `_SENTENCE_END` split **consumes** the inter-sentence whitespace run.
2. Chunk text is `" ".join(buf).strip()` — an N-char whitespace run becomes 1 char (`:129, :149, :169`).
3. `char_end = buf_start + len(text)` measures the *normalized* length (`:136, :156, :176`); the re-base `buf_start = buf_start + len(text) - len(" ".join(buf))` (`:144, :164`) is also computed in normalized space.

Also: dead variable `cursor_in_block` (`:119`, incremented `:167`, never read). ✅

**And the offsets, even if correct, would index the *normalized markdown*, not the original asset.** There is no PDF→markdown offset map, no page/bbox, no `\f` retention. ✅

### 2.5 What it takes to attach verbatim spans with locators

Ordered by dependency, minimal viable set:

1. **Fix the chunker to be offset-exact** (`chunking.py`). Replace re-joining with **source-slicing**: have `_split_sentences` return `(start, end)` pairs into the block, keep `text = markdown[start:end]`, and set `char_start/char_end` to true source offsets. This is a ~40-line rewrite of `chunk_markdown` `:103-181` and makes `markdown[char_start:char_end] == chunk.text` an assertable invariant. Add a property test.
2. **Fix `_SENTENCE_END`** (`:29`) — the `[A-Z0-9]` lookahead must exclude digits after known abbreviations (`Fig.`, `Eq.`, `et al.`, `vs.`, `No.`, `p.`, `Ref.`, `Sec.`, `Tab.`), or move to a proper segmenter (`pysbd`/`syntok`). ⚙️ demonstrated failure above.
3. **Preserve page anchors.** In `PyPDFNormalizer.normalize` (`:81-94`), emit an explicit page-boundary marker (or, better, record a `list[{page_no, char_start, char_end}]` into `structure_jsonb`, which already exists as JSONB at `models.py:120` and is never read for anything). A `char_offset → page_no` bisect then gives page locators for free.
4. **Populate the existing `Citation.chunk_ids` column** (`aleph-wiki/models.py:141`, JSONB, already present, already surfaced by `_resolve_citations` at `/home/claude/aleph/apps/api/src/aleph_api/routes/surfaces.py:524-531`) instead of writing `[]` at `agent/workflow.py:421` and `synthesis_workflow.py:219`.
5. **Add span columns to `Citation`**: `quote_text`, `quote_char_start`, `quote_char_end`, `normalized_document_id`. One migration; `Citation` currently has no FK constraints at all (`aleph-wiki/models.py:135-146`), so this is additive.
6. **Verify the span at write time** — an LLM-emitted quote must be confirmed as a literal substring of the cited chunk before the claim is accepted. This is a deterministic, zero-cost gate and is the single highest-leverage quality control available.

---

## 3. Claim extraction as it exists

### 3.1 Complete inventory of claim-generating sites

| # | Site | Cite | Capability / purpose | Produces |
|---|---|---|---|---|
| 1 | `_node_source_page_compose` | `agent/workflow.py:394-427` | `SYNTHESIS` / `wiki.source_page_compose` | `ClaimDraft[]` → `WikiClaim` + `Citation` rows, **and** `SourcePage.extracted_claims_jsonb` |
| 2 | `_node_commit_revision` (synthesis) | `synthesis_workflow.py:209-231` | — (no LLM) | iterates `report.claims`, **always empty** |
| 3 | `_node_compose` (research) | `research_workflow.py:849-878` | `SYNTHESIS` / `research.compose` | prose with `[cN]` markers only; `build_report` sets `claims=[]` `:337` |
| 4 | `_classify_factdiff` | `jobs/wiki_refresh.py:103-142` | `CLASSIFICATION` / `wiki.refresh.factdiff` | `unchanged\|updated\|contradicted` verdict on a *source*, not a claim |
| 5 | EditorialReviewer `contradiction` subagent | `editorial/workflow.py:214-218` | `SYNTHESIS` / `editorial.contradiction` | free-text `ReviewFinding` with `evidence_refs` |

**Site 1 is the only production claim producer.** ✅

### 3.2 Site 1 in detail

System prompt = `prompts/source_page_compose.md` + an inline JSON schema appended at the call site (`agent/workflow.py:399-401`):
```python
system_prompt=_prompt("source_page_compose")
+ '\n\nReturn JSON: {"body_md": "...", "summary": "...", '
+ '"claims": [{"text": "...", "citation_marker": "[c1]"}]}',
```
Prompt rules, verbatim (`source_page_compose.md:31,33,34`):
> - Citation markers are `[c1]`, `[c2]`, ... — one per claim. **You only emit the marker; the system maps markers to source chunks.**
> - Keep `Key claims` to the strongest 5–10 claims supported by the document.
> - Do NOT invent facts not present in the document.

⚠️ **The prompt promises provenance the system does not deliver.** The system does *not* map markers to chunks; `chunk_ids=[]` at `agent/workflow.py:421` with the comment `# Inc 1 binds claims to the source page itself.` ✅

Parsing + "validation" (`:407-427`), verbatim:
```python
raw_claims = ctx_chat.get("claims") or []
for c in raw_claims:
    text = (c.get("text") or "").strip()
    marker = (c.get("citation_marker") or "").strip() or "[c?]"
    if not text: continue
    claims.append(ClaimDraft(
        text=text, confidence="cited", section_anchor="key-claims",
        citations=[CitationDraft(chunk_ids=[], source_page_id=None, citation_marker=marker)]))
```
Quality controls present: **only the empty-text skip.** No Pydantic model (concepts and aliases *do* get Pydantic validation at `:266`/`:305`; claims do not). No cap on count despite the prompt's 5–10. `confidence="cited"` is unconditional — a claim carrying the literal fallback marker `"[c?]"` is still stamped `cited`. `text` truncated to 2048 only at insert (`wiki_service.py:322`); `marker` to 16 (`:338`). ✅

Input truncation: document body `[:50000]` chars (`:391`). 🔶 A 30-page paper exceeds this; claims are extracted from a prefix.

LLM call mechanics (`_call_llm_json`, `:190-230`): `response_format={"type":"json_object"}`, `temperature=0.0`, `max_tokens=4096`; on parse failure the fallback scan returns `{}` — **silently zero claims, no retry, no error**. ✅

Persistence: `WikiClaim` + `Citation` via `wiki_service.commit_revision` (`:310-341`), then a **lossy duplicate** into `SourcePage.extracted_claims_jsonb` in the same transaction (`agent/workflow.py:627-635`), shape `list[{"text": str, "marker": str}]` — first citation's marker only, no confidence, no anchor, no id. ✅ **Zero readers repo-wide** (only the model at `aleph-wiki/models.py:156`, the migration at `20260527_1500_inc1_rks_wiki.py:410-415`, the one writer, and two test seeds).

### 3.3 Honest comparison against Claimify's 4 stages

| Claimify stage | Aleph status | Evidence |
|---|---|---|
| **1. Selection** — identify verifiable content, discard non-factual | ❌ **Absent.** One prompt line ("strongest 5–10 claims") does all the work. No sentence-level pass, no verifiability classifier, no rejection of opinion/framing. | `source_page_compose.md:33` |
| **2. Disambiguation** — resolve referential/structural ambiguity, or *abstain* when unresolvable | ❌ **Absent.** No ambiguity detection, no abstention path. Every emitted claim is accepted. | `agent/workflow.py:407-427` |
| **3. Decomposition** — split into atomic, independently-verifiable propositions | ❌ **Absent.** The model is asked for "claims" with no atomicity instruction; multi-proposition sentences pass through whole. | `source_page_compose.md:23-34` |
| **4. Decontextualization** — make each claim standalone and self-contained | ❌ **Absent.** No instruction to resolve pronouns, expand acronyms, or inline context. Claim text is whatever the model wrote. | same |
| Quote grounding | ❌ Absent — `chunk_ids=[]` | `agent/workflow.py:421` |
| Coverage / faithfulness check | ❌ Absent | — |
| Human verification | ❌ Absent for claims (ApprovalCards exist for *pages* and *findings*, never for claims) | `catalog.py:188-196` — `target_kind` enum has no claim member |

**Aleph is at "stage 0": a single one-shot compose-and-extract prompt.** All four Claimify stages, plus grounding and human gating, are net-new.

### 3.4 Adjacent structures worth knowing

- **`ReviewFinding.target_claim_id`** exists (`/home/claude/aleph/packages/aleph-reviewer/src/aleph_reviewer/models.py:42`) and is **never written** by any production call site. ✅ The finding→claim edge is pre-built and unused.
- **`HypothesisEvidence`** (`/home/claude/aleph/packages/aleph-hypotheses/src/aleph_hypotheses/models.py:62-72`) has `stance`, `evidence_kind`, `target_id`, `weight` (Float). `stance ∈ {supports, contradicts, contextualizes}` validated at `hypothesis_service.py:184-185`; `evidence_kind` per the analyst subagent docstring includes `claim` (`/home/claude/aleph/apps/api/src/aleph_api/subagents/analyst.py:69`). **A weighted, signed, three-valued claim→hypothesis evidence edge already exists.** ✅
- **`confidence.next_confidence_from_evidence`** (`/home/claude/aleph/packages/aleph-hypotheses/src/aleph_hypotheses/confidence.py:43-66`) is a working epistemic-status computation over signed weighted evidence: `net ≤ -3 → REFUTED`, `net ≤ -1 → CONTESTED`, `net ≥ 3 ∧ max_pos ≥ 1.5 → WELL_SUPPORTED`, `net ≥ 1 → WEAKLY_SUPPORTED`. Six-state `Confidence` StrEnum `:28-34`. **This is a prototype of the belief-status engine, already shipped, applied only to hypotheses.** ✅
- **Vocabulary split:** claims use `cited|uncited|contested|retracted`; hypotheses use the 6-state enum. No mapping exists. ✅
- **Five incompatible claim shapes** coexist: `ClaimDraft` (`wiki_service.py:44-49`), `ResearchClaim` (`synthesis_workflow.py:55-59`, `citation_markers: list[str]`), the inline LLM JSON (`citation_marker` singular), `extracted_claims_jsonb` (`{text, marker}`), and `WikiClaim` (`aleph-wiki/models.py:123-132`). No Pydantic schema in `aleph-core` (`/home/claude/aleph/packages/aleph-core/src/aleph_core/schemas/` contains only `cost, ledger, model_profile, project`). ✅

---

## 4. aleph-scholar

Pure-HTTP, zero LLM (`docs/research-loop.md:53`). 1,468 LOC across 11 modules.

### 4.1 Public API ✅

| Fn | Cite | Upstream | Params |
|---|---|---|---|
| `verify_dois(dois)` | `service.py:76`; impl `dois.py:126-171` | OpenAlex + Crossref | batched ≤50 |
| `crossref_lookup(query, rows=10)` | `service.py:81`; `crossref.py:96-105` | `api.crossref.org/works` | `query.bibliographic`, `rows` |
| `search_openalex(query, per_page=10)` | `service.py:84`; `openalex.py:89-94` | `api.openalex.org/works` | `search`, `per-page` (≤200), `mailto` |
| `expand_citations(ref, direction, limit=25)` | `service.py:87-96`; `openalex.py:128-150` | OpenAlex ×2–3 | `referenced_works[:limit]`, `filter=cites:{id}` |
| `search_consensus(project_id, query, filters)` | `service.py:120-123`; `consensus.py` | MCP `https://mcp.consensus.app/mcp` | tool name `"search"` |
| `extract_dois(text)` | `dois.py:66-75` | — | regex `10\.\d{4,9}/[^\s"'<>]+` (`:27`) |
| `style_pass(markdown)` | `style.py:102-146` | — | deterministic, idempotent |

### 4.2 Tri-state `verify_dois` ✅

`ok=False` is produced by **exactly one line**: `dois.py:166`, reachable only when OpenAlex missed **and** Crossref returned an authoritative 404 (`crossref.py:90-91`) **and** `openalex_answered is True` (set `False` only at `dois.py:144-145` on `ScholarUpstreamError`). Every other failure mode → `ok=None` via `_unverifiable` (`:102-111`, triggered at `:154-160` and `:167-169`). The fail-safe is sound.

Retraction: OpenAlex `is_retracted` (`openalex.py:73`) is primary; Crossref `update-to`/`updated-by` relation types ∈ `{retraction, retracted, withdrawal, removal}` (`crossref.py:18, 57-63`). **⚠️ These are never combined** — `dois.py:149-152` `continue`s on OpenAlex hit, so Crossref retraction detection fires only on the OpenAlex-miss path. `docs/research-loop.md:69` says Crossref relations *"corroborate"*; in code they cannot. ✅

### 4.3 Source-quality signals — the inventory an epistemic-status engine needs

`WorkRef` (`types.py:32-47`) — **nine fields, the only quality carrier**:
```
doi, openalex_id, title, year, venue, authors[], cited_by_count, pdf_url, landing_url
```
`DoiVerdict` (`types.py:23-29`) — **seven fields**: `doi, ok, retracted, title, year, openalex_id, checked_via`.

| Signal you need | Available? | Where it goes |
|---|---|---|
| **Venue / journal** | ✅ captured (`openalex.py:56-58,67`; `crossref.py:73`) | ❌ **dropped at `DoiVerdict`** (`dois.py:79-87` propagates only title/year/openalex_id); ❌ dropped by the research loop's `_candidate_from_work` (`research_workflow.py:382-387` keeps only doi/openalex_id/year/query); ❌ never persisted |
| **Citation count** | ✅ `cited_by_count` (`openalex.py:69`; `crossref.py:75` from `is-referenced-by-count`) | ❌ same three drops. **The sole quantitative quality signal in the package, and it never reaches Postgres.** |
| **Retraction** | ✅ but only via `verify_dois` | ❌ `is_retracted` lives only on `OpenAlexWork` (`openalex.py:24-29`); every `WorkRef` path throws it away (`:94, :139, :150`) — **search and citation-expansion results carry no retraction signal** |
| **Open access** | ❌ **discarded** — `open_access.is_oa` / `oa_status` never read; only `best_oa_location.pdf_url`/`landing_page_url` as URLs (`openalex.py:59-61`) | — |
| **Publication year** | ✅ | ✅ reaches `source_metadata_jsonb` as `publication_year` (`research_workflow.py:385`) |
| **Author affiliations / ORCID** | ❌ only `authorships[].author.display_name` (`openalex.py:52-55`); Crossref `author[].affiliation`/`ORCID` dropped (`crossref.py:43-54`) | — |
| **Concepts / topics** | ❌ never read | — |
| **SJR / h-index / FWCI** | ❌ — the `source` sub-object is read but only `display_name` extracted (`openalex.py:57-58`). 🔶 OpenAlex serves venue-prestige analogues on the `/sources` entity, which this package never calls. | — |

**There is no `select=` parameter on either upstream** (✅ grep: zero hits). Full records are downloaded and ~95% discarded client-side in `parse_work`. **Capturing the missing fields is free bandwidth-wise** — a pure parse-side change.

**What actually persists** is a 4-key blob, written identically in three places — `mechanical/workflow.py:319-343`, `research_workflow.py:741-751`, `subagents/researcher.py:264-277`:
```python
source_metadata_jsonb["doi_verdict"] = {"ok", "retracted", "checked_via", "checked_at"}
```
plus sibling `doi` / `openalex_id`. **No venue, no citation count, no OA status, ever.** ✅

### 4.4 Rate limits, quota, caching

- Token bucket `_TokenBucket` (`http.py:63-92`) — `asyncio.Lock` held across the sleep (`:83,89`), **per-host** (keyed on `httpx.URL(url).host`, `:135-144`), `rate_per_second=1.0`, `burst=5` (`:112-124`). Buckets are **per-`ScholarHttp`-instance**, and the API (`lifespan.py:110`) and workers (`arq.py:95`) each build their own → **rate limiting is per-process, not global**. ✅
- Retry: tenacity, 3 attempts, exponential 1→4s, retryable = transport/timeout/429/≥500 (`http.py:42-48, 146-152`). In-loop `Retry-After` sleep **stacks on top of** the tenacity backoff (`:170-174`). ⚠️ `_RETRY_AFTER_CAP_S = 8.0` (`:39`) vs two docstrings saying "capped at 30s" (`:10`, `:52`).
- Timeouts: 30s total (`http.py:127`); Consensus MCP `Timeout(30.0, read=300.0)` (`consensus.py:201`).
- Batching: `OPENALEX_DOI_BATCH_SIZE = 50` (`openalex.py:20`), `ValueError` above (`:100-102, :113-115`); reviewer per-run cap `DOI_VERIFY_CAP = 50` (`mechanical/workflow.py:55`); route caps `max_length=200` DOIs (`routes/scholar.py:84`).
- **Caching: none.** ✅ No cache module, no memoization, no ETag/conditional requests, no redis GET/SET of any scholar response. The write-back to `source_metadata_jsonb.doi_verdict` is commented "cache" (`mechanical/workflow.py:319`) but **nothing ever reads it to skip a verification**. At 1 req/s per host with no cache, DOI verification is the pipeline's hardest throughput ceiling.
- Consensus quota: redis `scholar:consensus:{project_id}:{YYYY-MM}`, `INCR` + `EXPIRE 5356800` on first (`consensus.py:252-260`), cap 200/month (`settings.py:90`). At cap → `ConsensusResult(status="quota_exhausted")` with zero HTTP calls (`:361-365`). ⚠️ The counter increments **before** the credential load, so a failed search still burns a unit; no decrement path.
- Consensus OAuth: refresh at `token_endpoint` with `{grant_type, refresh_token, client_id}`, public client no secret (`:278-285`); redis lock `scholar:consensus:refresh:{pid}` `SET nx ex=30` with ownership-checked release (`:330-351`); HTTP 400/401 → persist `status="reconnect_required"` + raise `ConsensusReconnectRequired` (`:311-326`).
- ⚠️ **Consensus is API-process-only.** The workers' `ScholarService` is built with no redis and no credential callbacks (`arq.py:94-97`), so `consensus_client()` there raises `RuntimeError` (`service.py:102-107`). ✅ (consistent with `docs/research-loop.md:30`).
- ⚠️ **`ConsensusResult` loses structured metadata on the live path.** The markdown fallback (`consensus.py:167-189`) always sets `doi=None` (`:183`) and flattens authors/year/citations/journal into a prose `snippet` (`:181`). `ConsensusHit` is only `{title, url, doi, snippet}` (`types.py:62-68`).

### 4.5 Second, divergent OpenAlex client ⚠️

`/home/claude/aleph/packages/aleph-connectors/src/aleph_connectors/openalex/register.py:44-82` is an **independent** OpenAlex integration that captures fields `aleph-scholar` drops — `open_access.oa_url` (`:63`), `referenced_works[:50]` (`:77`) — modelled in `OpenAlexMetadata` (`:26-32`). It has **no token bucket and no retry** (`httpx.AsyncClient(timeout=20.0)`, `:42`) and uses `per_page` where the API wants `per-page` (`:48`). Two OpenAlex clients with different field coverage and different politeness. ✅

---

## 5. Cost, tiering, scale

### 5.1 Cost tracking ✅

- Math: `/home/claude/aleph/packages/aleph-models/src/aleph_models/pricing.py:101-124`, cache-discount-aware, `Decimal` quantized to 1e-6. 11-model hardcoded table `:28-88`, default `cache_discount_pct=90` `:25`.
- Writes: `ModelCall` at `/home/claude/aleph/packages/aleph-db/src/aleph_db/repos/cost.py:47-63`, `CostLedgerEvent` at `:65-73`. Called from `client.py:402-440` (`_record_call`), invoked by `chat` `:271-285` and `embed` `:355-369`.
- Budget rollup is a **Postgres AFTER-INSERT trigger** (`20260527_1200_inc0_initial.py:571-589`).

**Three cost defects worth the roadmap's attention:**
1. ⚠️ **`BudgetExceeded` is raised in exactly one place repo-wide** — `/home/claude/aleph/apps/api/src/aleph_api/routes/smoketest.py:58-61`, the *smoke test* route. `LiteLLMClient` never checks a budget; no worker job does. `Budget.soft_pct` (`models/cost.py:58-66`) is stored and never read. **A runaway ingestion has no spend guard.** ✅
2. ⚠️ **Unknown models cost $0 silently** — `pricing.py:110-112` returns `(0,0)`; the docstring at `:92-93` promises a logger warning that does not exist (no logger in the module).
3. ⚠️ **Prompt caching is priced but never requested.** Repo-wide grep for `cache_control|ephemeral|anthropic-beta|prompt_caching` → zero hits. The chat payload (`client.py:223-233`) sets only model/messages/temperature/max_tokens/response_format/tools. 🔶 `cached_tokens` will be 0 on every call; the 90% discount and `cache_savings_usd` column are dead. **For a claim pipeline that re-sends the same long document across 4 Claimify stages, this is the single largest available cost lever.**
4. ⚠️ `aleph_models/__init__.py:3-4` asserts *"Every LLM and embedding call in Aleph routes through `LiteLLMClient`. There is no other path."* — **false**: the agent surface uses `ChatOpenAI` + `AgentCostCallbackHandler` (`copilot_agent.py:1249-1260`), depending on `stream_options.include_usage` for any cost to be recorded.

### 5.2 Model profiles ✅

Seeds at `20260527_1200_inc0_initial.py:29` (dev) / `:95` (prod), inserted `:597-617`.

| Capability | aleph-dev | aleph-production |
|---|---|---|
| synthesis | `claude-sonnet-4-6` (`:31`) | `claude-opus-4-7` (`:97`) |
| judge | `claude-sonnet-4-6` (`:40`) | `claude-opus-4-7` (`:106`) |
| page_selection / extraction / vision | `claude-haiku-4-5` (`:49,58,67`) | `claude-sonnet-4-6` (`:115,124,133`) |
| classification | `claude-haiku-4-5` (`:76`) | `claude-haiku-4-5` (`:142`) |
| embedding | `titan-embed-v2` (`:85`) | `titan-embed-v2` (`:151`) |
| **rerank** | **absent** | **absent** |
| **code** | **absent** | **absent** |

⚠️ `Capability.CODE` is used at `subagents/viz_builder.py:157`; it survives only because `_resolve_agent_model` catches bare `Exception` and returns hardcoded `"claude-sonnet-4-6"` (`copilot_agent.py:1203, 1220-1223`). `RERANK` is never referenced — **there is no rerank stage in retrieval.**

### 5.3 Tiering — one tier, one place

✅ The **only** cheap-triage-then-deep-pass in the system is `research.triage` (Haiku, `research_workflow.py:683-691`), which filters search *candidates* before fetch.

Everything after `register_uploaded_source` is unconditional:
- `normalize_job` enqueues **both** `chunk_embed_job` and `wiki_ingest_job` with no gate (`jobs/normalize.py:193-194`). ✅
- `wiki_ingest_job` runs 3 EXTRACTION calls + 1 SYNTHESIS call + **one SYNTHESIS-class stub call per extracted concept in a Python loop** (`agent/workflow.py:481-517`) — 5–25 concepts per the prompt (`concept_extraction.md:28`), i.e. **8–29 LLM calls per document**.
- `wiki_ingest.py:206-214` then enqueues `curate_page_job` for **every** committed revision.
- `NormalizedDocument.quality_flags_jsonb` is computed (`normalize.py:159`) — including the `ocr-required` flag from `normalization.py:89-92` — written to the ledger (`:183`), and **never read to skip or downgrade anything.** ✅
- **Manual uploads bypass triage entirely** — `routes/sources.py:176` goes straight to `normalize_job`.

**Deduplication:** ✅ Three mechanisms, none at the ingest boundary.
- Research-loop pre-ingest `dedup_key` (`research_workflow.py:341-349`, DOI → URL → kind:external_id) seeded from `state["seen_keys"]` — **per-run state, not the DB**. Two runs on the same topic re-ingest and re-embed identical sources.
- `register_uploaded_source` computes `sha` (`source_service.py:78`) and **never queries for it**. `SourceAsset.sha256` is `index=True` but **not unique** (`aleph-rks/models.py:106`); `Source` has no unique constraint on `url` (`:64-67`).
- `duplicate_source` is a *post-hoc reviewer finding* (`mechanical/workflow.py:452-471`) — after full spend.

**LLM result caching: none.** `LiteLLMClient`'s `idempotency_key` path (`client.py:199-221`) returns an **empty-content stub** with `finish_reason="idempotent_replay"` and **no caller in the repo passes it**. ✅

### 5.4 Concurrency / throughput

- 14 arq jobs registered (`/home/claude/aleph/apps/workers/src/aleph_workers/arq.py:125-140`). **All 14 are bare `async def`s with no per-job overrides** — every one inherits `job_timeout = 600` (`:144`) and arq's default `max_tries = 5`. ✅ `max_tries` is never configured repo-wide.
- ⚠️ `deep_research_job` and `bootstrap_project_job` make many LLM calls; a timeout re-runs the whole job up to 5 times, **re-spending everything**. The retry-guard at `jobs/research.py:96-108` converges the *run* but does not stop the *re-enqueue*.
- `WorkerSettings` (`arq.py:124-146`): `max_jobs = arq_max_jobs` (default 10, `settings.py:53`), **compose overrides to 4** (`deploy/compose/docker-compose.yml:411`). `keep_result = 3600`.
- **`queue_name` is not set** → all 14 jobs share `arq:queue` with no prioritization. A 10-minute research job holds one of four slots while cheap curate jobs queue behind it. ✅
- **No `deploy:`/`replicas:` in compose** → exactly **one worker container, one process** (`docker-compose.yml:400-452`, `mem_limit: 2g`). **Total stack ingest concurrency = 4 concurrent jobs.** ✅
- ⚠️ **No `cron_jobs` attribute anywhere in the repo.** `refresh_stale_pages_job` (`jobs/wiki_refresh.py:386`) documents itself as "the bounded scheduler pass" but **nothing schedules it**. The freshness system has no trigger. ✅
- Embedding is a serial `for` loop over batches of 64 with no `gather` (`embedding.py:99-113`). ✅

### 5.5 Bottleneck ordering at 10k / 100k / 1M documents

Assume ~40 chunks/doc (a 20k-token paper at 512 tokens with overlap). 🔶 arithmetic; all component facts ✅.

| Scale | Chunks | First thing that fails |
|---|---|---|
| **10k docs** | ~400k | **The arq worker.** 4 concurrent slots × 8–29 LLM calls per doc in `wiki_ingest_job`. At ~3s/call that is ~10⁵ LLM calls serialized 4-wide ≈ **days**. Second: the **ledger chain-head lock** (below). Third: `_next_short_id` — `SELECT count(*) FROM sources` **with no project filter** on every registration (`source_service.py:36-39`) — an O(N) full count per ingest, and a global uniqueness collision surface. |
| **100k docs** | ~4M | **The Action Ledger.** `LedgerRepo._lock_or_create_head` (`/home/claude/aleph/packages/aleph-db/src/aleph_db/repos/ledger.py:118-138`) takes `SELECT … FOR UPDATE` on **one row per project**, held until the enclosing transaction commits. In `chunk_embed_job` the `LedgerWriter` is created at `chunk_embed.py:192` and appends at `:245-258` — **inside the same transaction that bulk-inserts every chunk** (`:193-210`). Lock hold time scales with document size. All project writes serialize. Additionally: `verify_project_chain` (`ledger.py:185-243`) loads **every event for the project into Python memory** (`:194-202`) — unrunnable well before this point. |
| **1M docs** | ~40M | **The HNSW index.** 40M × 1024-dim float4 = **~164 GB of raw vectors** plus graph edges, in an unpartitioned table on a 1.5 GB-limit Postgres container (`docker-compose.yml:15`). HNSW build is not incremental-friendly at this size, `maintenance_work_mem` is never set (✅ grep: no hits), and `ef_search` is never tuned. **Mitigating factor:** every query is `WHERE source_id = ?` (`retrieval.py:60-66`) — intra-source descent only — so the HNSW index is *never actually used for its ANN properties*; a plain btree on `(source_id, ordinal)` plus an exact scan would serve the current access pattern. 🔶 **The vector index is the largest object in the system and the current retrieval design does not need it.** |

**Also unbounded, no partitioning, no retention anywhere** (✅ grep for `PARTITION` → only `str.partition()` false positives): `model_calls`, `cost_ledger_events` (1:1 with model_calls), `action_ledger_events` (**immutable by trigger**, `20260527_1200_inc0_initial.py:396-403` — cannot be pruned at all), `document_chunks` (no time column), `wiki_revisions` (full `body_md` per revision, immutable), `agent_runs` (one per `chunk_embed_job`).

Index gaps: `source_versions.sha256` has **no index** (`aleph-rks/models.py:93`) — the content-identity column. `model_calls` has no composite `(project_id, timestamp)` despite every rollup filtering and ordering on exactly that (`repos/cost.py:118-122`). `ix_sources_short_id` (`models.py:66`) is redundant with the `unique=True` at `:75`.

**Migration age:** 16 migrations total; only **two** touch chunks/vectors, both from 2026-05-27. The HNSW parameters are original and have never been revisited.

---

## 6. What a claim-centric ingestion pipeline requires

### 6.1 Target shape

```
                    ┌─ TIER 0 (deterministic, $0) ─────────────────────────┐
fetch → sha256 dedup│→ mime/size/lang gate → quality_flags gate (ocr-req?) │
                    └──────────────────────────────────────────────────────┘
                                        ↓ pass
                    ┌─ TIER 1 (Haiku, ~1 call/doc) ────────────────────────┐
                    │  relevance + doc-type triage → {drop | shallow | deep}│
                    └──────────────────────────────────────────────────────┘
                          ↓ shallow                    ↓ deep
                   normalize+chunk+embed        normalize+chunk+embed
                   (no claim extraction)                ↓
                                          ┌─ TIER 2: Claimify (extraction cap.) ─┐
                                          │ select → disambiguate → decompose →  │
                                          │ decontextualize                      │
                                          └──────────────────────────────────────┘
                                                       ↓
                                          ┌─ TIER 2.5: LAYER-1 source frame ─────┐
                                          │ per-source: stance, hedging, framing,│
                                          │ terminology-as-used, scope claims    │
                                          └──────────────────────────────────────┘
                                                       ↓
                                          ┌─ TIER 3: deterministic grounding ────┐
                                          │ verbatim span match into chunk text  │
                                          │ → reject ungrounded claims, $0       │
                                          └──────────────────────────────────────┘
                                                       ↓
                                          ┌─ TIER 4: relation proposal (judge) ──┐
                                          │ supports/contradicts/refines/dup     │
                                          │ over the claim graph                 │
                                          └──────────────────────────────────────┘
                                              ↓ non-dispute      ↓ dispute-class
                                          auto-accept      ApprovalRequest → ApprovalCard
                                                       ↓
                                          ┌─ TIER 5: epistemic status ───────────┐
                                          │ signed weighted evidence → 6-state   │
                                          └──────────────────────────────────────┘
```

### 6.2 New nodes/stages — concrete placement

| # | Stage | Where it slots | Reuses |
|---|---|---|---|
| **T0** | Content dedup + gate | In `register_uploaded_source` (`source_service.py:78`) — query `SourceVersion.sha256` before writing. Needs an index (currently absent, `models.py:93`). | existing sha computation |
| **T1** | Document triage | A new node between `normalize_job` and its fan-out (`jobs/normalize.py:193-194`). Set a `Source.processing_tier` column; `wiki_ingest_job` becomes conditional. | `Capability.CLASSIFICATION` → Haiku, exactly like `research.triage` |
| **T2** | Claimify 4-stage | Replaces the claim half of `_node_source_page_compose` (`agent/workflow.py:394-427`). **Split it**: keep page composition as-is, extract claims in a new sub-graph. Operates per-chunk (offset-exact after the chunker fix), not on a 50k-char prefix. | `Capability.EXTRACTION`, `_call_llm_json` `:190-230` (add retries) |
| **T2.5** | **Layer-1 source frame** | A **new node in the same sub-graph, before decontextualization**, and — critically — persisted on a **new `SourceFrame` table keyed by `(source_id, claim_id)`**, never merged into `WikiClaim`. Decontextualization is exactly where a source's own framing is currently *destroyed*; capture it there. | `SourcePage` is the natural home (`aleph-wiki/models.py:149-157`); `extracted_claims_jsonb` is dead and re-purposable |
| **T3** | Verbatim-span grounding | Deterministic post-T2 gate: `assert quote in chunk.text`. **Zero LLM cost, highest quality leverage.** Requires the offset-exact chunker (§2.5). | `Citation.chunk_ids` (exists, always `[]`) |
| **T4** | Relation proposal | New job `propose_claim_relations_job`, batched per topic-cluster. `Capability.JUDGE`. | `HypothesisEvidence.stance` semantics (`supports/contradicts/contextualizes`) already validated at `hypothesis_service.py:184-185` |
| **T5** | Epistemic status | New service; **port `next_confidence_from_evidence`** (`confidence.py:43-66`) from hypotheses to claims. | the shipped 6-state `Confidence` StrEnum `:28-34` |
| **Term alignment** | Purpose-relative mappings | Extend `AliasService` (`synthesis_workflow.py:118`) — currently a flat surface-form→canonical map — with a `purpose` discriminator. `concept_extraction.md` already emits `surface_forms[]` per source, which is the raw material for layer-1 terminology. | `ExtractedConcept`/`ExtractedAlias` Pydantic models `agent/workflow.py:74-85` |

### 6.3 Human-review gating on dispute-class relations — the wiring already exists

The ApprovalCard path is complete and generic. To gate `contradicts`/`disputes` relations:

1. `create_request(session, project_id=…, target_kind="claim_relation", target_id=relation_id, title, summary, severity, proposed_patch, evidence_refs, requested_by)` — `/home/claude/aleph/packages/aleph-reviewer/src/aleph_reviewer/approval_service.py:30-61`. **No change needed.**
2. Add `"claim_relation"` to the catalog's `target_kind` enum — `/home/claude/aleph/packages/aleph-a2ui/src/aleph_a2ui/catalog.py:188-196` (currently `synthesis_proposal | review_finding | wiki_revision | agent_action | refresh_result`). One-line schema bump; **the CI roster sweep requires producer+renderer in the same PR** (`scripts/check-catalog-roster.sh`), but `ApprovalCardView` already renders every `target_kind` uniformly (`apps/web/src/a2ui/components/ApprovalCard.tsx`), so only a producer is new.
3. Add an `if target_kind == "claim_relation":` branch to the approve/reject handlers — `/home/claude/aleph/apps/api/src/aleph_api/a2ui_handlers.py:149` (approve) and `:414` (reject). The file already has six such branches; `:401` raises `approve handler not wired for target_kind=…` for unknowns.
4. `evidence_refs` **already supports `kind: "claim"`** — `catalog.py:207` enum is `["claim","source","chunk","page"]`. A dispute card can cite both claims and both source chunks with no schema change. ✅
5. `decide()` (`approval_service.py:64-130`) writes the `ApprovalDecision` row + ledger event `approval_request.{approved|rejected}` and mirrors status onto a linked `ReviewFinding`. Add the same mirror for `ClaimRelation.status`.
6. Cards land in Briefs automatically — `_briefs_messages` (`/home/claude/aleph/apps/api/src/aleph_api/routes/surfaces.py:581`) builds the surface, pushed as `updateDataModel` deltas over SSE. **No frontend polling to add.**

Set severity from the relation type so `medium+` auto-raises, matching `editorial/workflow.py:140-149`.

### 6.4 Reusable unchanged

- **The whole ingest chain** (`connector.search/fetch → register_uploaded_source → normalize_job`) needs no structural change — only a triage gate inserted at the fan-out.
- **Tool binding + allowlist** (`tools.py:176-225`) is correct and generic; new claim-stage connectors bind through the same path.
- **`with_phase` progress streaming** (`agent_events.py:170-197`) — decorate every new node; the Activity card works for free.
- **The agent-token + `AgentRun` lifecycle** (`_kick_normalize`, `research_workflow.py:593-646`) is the template for any new worker stage.
- **Ledger + `access_scope` + `project_id`** discipline — new tables inherit `CommonColumns` and get audit for free.
- **The embedding dimension guard** (`chunk_embed.py:126-177`) — a genuinely good zero-spend pre-check pattern, worth copying for the claim stages.
- **`ScholarHttp` politeness wrapper** (`http.py:112-183`) — the token bucket + tenacity + `ensure_ok` triple is reusable for any new upstream.

### 6.5 Infrastructure the owner may not realize exists

1. **`HypothesisEvidence` is already a signed, weighted, three-valued evidence edge** (`aleph-hypotheses/models.py:62-72`) — `stance ∈ {supports, contradicts, contextualizes}`, `weight: Float`, `evidence_kind` explicitly including `"claim"` (`subagents/analyst.py:69`). This is the claim-relation table, already migrated.
2. **`confidence.next_confidence_from_evidence`** (`confidence.py:43-66`) is a **working, tested epistemic-status function** over exactly that edge type, with a 6-state output enum. Point it at claims and Tier 5 is largely done.
3. **`ReviewFinding.target_claim_id`** (`aleph-reviewer/models.py:42`) — the finding→claim edge is migrated and never written.
4. **`Citation.chunk_ids`** (`aleph-wiki/models.py:141`) is a JSONB array, already read and surfaced by `_resolve_citations` (`routes/surfaces.py:524-531`) and by `ClaimCard` (`catalog.py:104`). The quote-provenance wire format exists end-to-end; only the writer passes `[]`.
5. **`NormalizedDocument.structure_jsonb`** (`aleph-rks/models.py:120`) is a written-but-never-read JSONB — the natural home for a page-offset map with no migration.
6. **`SourcePage.extracted_claims_jsonb`** (`aleph-wiki/models.py:156`) — one lossy writer, **zero readers**. Free real estate for the layer-1 source frame.
7. **`Capability.RERANK`** is defined (`aleph-core/schemas/model_profile.py:13-22`) and bound nowhere — a pre-declared slot for claim-cluster reranking, needing only a seed row.
8. **`AliasService` + `WikiAlias`** already implement surface-form→canonical resolution with confidence scores (`concept_extraction.md:8-20`, `alias_extraction.md:10-19`). Adding a `purpose` discriminator turns it into the purpose-relative terminology map.
9. **`RejectionFeedback` → prompt injection** (`agent/workflow.py:363-380`, `source_page_compose.md:37-39`) is a working human-feedback loop: rejections on a page are fed back as constraints on the next compose. **This is the LLM-proposes/human-verifies loop, already built**, currently applied only to page composition.
10. **The `deterministic HTML compiler`** `_render_claims` (`aleph-wiki/html_compiler.py:107-121`) reads `{text, confidence}` and renders byte-deterministically — a claim-status surface that needs only richer input.

### 6.6 Ordering recommendation

**Do these first; everything downstream depends on them.**

| Order | Item | Why |
|---|---|---|
| 1 | Offset-exact chunker + abbreviation-safe sentence splitter (`chunking.py:29, 103-181`) | ⚙️ Demonstrably wrong today. Every quote, span, locator, and Claimify *select* stage sits on it. Also fixes chunk quality for the existing embedder. |
| 2 | Populate `Citation.chunk_ids` + add span columns; write `source_page_id` | Unblocks retraction blast-radius, freshness citation-health, refresh contributing-sources — three shipped subsystems currently inert on production data. |
| 3 | A single Pydantic `Claim` schema in `aleph-core/schemas/` | Five incompatible shapes today. Consolidate before adding a sixth. |
| 4 | T0 sha256 dedup + T1 triage gate at `normalize.py:193-194` | Without it, T2's 4× LLM cost multiplies against an unfiltered corpus at 4-wide concurrency. |
| 5 | Enable prompt caching in `client.py:223-233` | The 4-stage pipeline re-sends the same document 4×; the discount is already priced (90%) and already parsed (`client.py:252-255`) — only the request flag is missing. |
| 6 | `max_tries=1` on `deep_research_job` + a real budget check in `LiteLLMClient` | Before scaling spend, close the two paths that silently multiply it. |
| 7 | Capture `venue`, `cited_by_count`, `is_retracted`, OA status in `WorkRef`/`DoiVerdict` and persist them | 🔶 Free (records are already downloaded in full); these are the required inputs to a decomposed epistemic-status computation, and none of them currently reach Postgres. |

---

## 7. Doc/code contradictions (all ✅)

| # | Doc claim | Code reality |
|---|---|---|
| 1 | `research-loop.md:20` — compose "builds a `ResearchReport` dataclass (…, `claims`)" | The field exists but `build_report` hardcodes `claims=[]` (`research_workflow.py:337`). No research-path claim ever exists. |
| 2 | `research-loop.md:28` — "The **search node** emits a `research.tools` agent-event" | Emitted in the job before the graph runs (`jobs/research.py:159-167`). |
| 3 | `research-loop.md:69` — Crossref retraction relations "corroborate" OpenAlex | Unreachable when OpenAlex resolves the DOI (`dois.py:149-152`). One-directional only. |
| 4 | `source_page_compose.md:31` — "the system maps markers to source chunks" | It does not: `chunk_ids=[]` (`agent/workflow.py:421`). |
| 5 | `source_page_compose.md:41-42` — "If `Hand-edited sections to preserve` is provided…" | Never injected; `_node_source_page_compose` builds the payload at `:382-392` and appends only `rejection_block`. Dead prompt clause. |
| 6 | `aleph_models/__init__.py:3-4` — "Every LLM call routes through `LiteLLMClient`. There is no other path." | The agent surface uses `ChatOpenAI` + callback (`copilot_agent.py:1249-1260`). |
| 7 | `pricing.py:92-93` — unknown model "raise[s] a warning via the logger" | No logger in the module; returns `(0,0)` silently (`:110-112`). |
| 8 | `http.py:10, :52` — `Retry-After` "capped at 30s" | `_RETRY_AFTER_CAP_S = 8.0` (`:39`). |
| 9 | `CLAUDE.md:106` — "`ModelProfile` resolves capability → model" for 9 capabilities | Seeds bind 7; `code` and `rerank` are absent, `code` silently falls back to hardcoded Sonnet (`copilot_agent.py:1220-1223`). |
| 10 | `CLAUDE.md:105` — "Every row carries `project_id` + `created_at`…`access_scope`" | `SourcePage` (`aleph-wiki/models.py:149`) and `DocumentChunk` (`aleph-rks/models.py:124`) extend bare `Base`, not `CommonColumns`. |
| 11 | `GOAL.md:143-146` — refresh job "emits ApprovalCards"; freshness "computed by the curator" | `refresh_stale_pages_job` (`wiki_refresh.py:386`) has **no scheduler** — no `cron_jobs` exists repo-wide. |
| 12 | `CLAUDE.md:139` — "No placeholder code in production paths" (CI greps TODO/FIXME) | The grep passes, but `synthesis_workflow.py:221` carries `source_page_id=None,  # source page resolved by short_id later` — there is no "later"; nothing backfills it. |

---

## 8. Additional verified defects worth a line in the roadmap

- `agent/workflow.py:137` — the wiki ingest workflow uses a **module-global** `_active_ctx`, where `research_workflow.py:158` and `synthesis_workflow.py:84` use `ContextVar`. Concurrent runs in one worker process race. With `max_jobs=4` this is live.
- `agent/workflow.py:610` — `session.get(SourcePage, result.page_id)` looks up by **primary key** but is passed a `page_id`; always misses, masked by the correct guard at `:616-619`.
- `citation_verification.py:15` — `CITATION_RE = r"\[(c\d+)\]"` matches only `[cN]`, but `style_pass` (`style.py:24`, `_MARKER_RE = r"\[(\d+)\](?!\()"`) renumbers to bare `[N]`, and the e2e tests seed `"[1]"`. The verification regex and the normalizer disagree on marker syntax.
- `freshness.py:76-77` — "No claims → nothing unbacked; vacuously healthy." Combined with `claims=[]` on the synthesis path, **every synthesized page scores a perfect citation-health dimension.** 🔶
- `sanitize_report()` (`citation_verification.py:65-76`) is defined and **never called in production**.
- `dois.py:171` — `verify_dois` output is not positionally alignable with its input (duplicates duplicated, empty strings dropped). Both callers key by `verdict.doi`, so latent not live.
- `_next_short_id` (`source_service.py:36-39`) — `SELECT count(*) FROM sources` with **no project filter**; O(N) per ingest and a global-uniqueness collision surface under concurrency (`Source.short_id` is `unique=True`, `models.py:75`).agentId: a8e9bf5cbc460da21 (use SendMessage with to: 'a8e9bf5cbc460da21', summary: '<5-10 word recap>' to continue this agent)
<usage>subagent_tokens: 210339
tool_uses: 38
duration_ms: 913008</usage>