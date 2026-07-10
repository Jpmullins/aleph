# Research loop

Research is a **native** in-process Arq worker loop in `aleph-research`. There is no separate research subsystem, no HTTP callback surface, no polling, and no slot throttle — the whole loop runs inside `aleph-workers`.

## The native deep-research job

`packages/aleph-research` defines one `ResearchWorkflow` (LangGraph), run by the Arq job `deep_research_job(ctx, agent_run_id, token)` in `aleph-workers`. It runs entirely in-process: no HTTP callbacks, no poll job, no deferred submit.

```
plan → search → ingest → reflect ─(loop ≤ max_iterations, plateau cutoff)→ search
                              └──→ compose → synthesize → END
```

Every node is a `@with_phase` step so progress streams into the Activity card automatically (ContextVar-based ctx, like `SynthesisWorkflow`).

- **plan** (LLM, `Capability.SYNTHESIS`, purpose `research.plan`): topic → 3–6 subqueries, each tagged with preferred tool kinds. JSON response.
- **search** (pure HTTP, no LLM): fans the subqueries across the **bound** tools — the project's enabled connectors plus `ScholarService.search_openalex` / `expand_citations`. Results deduped by URL/DOI.
- **ingest** (LLM triage, purpose `research.triage`, `Capability.CLASSIFICATION`): selects ≤ `research_max_sources_per_iter`; each selected result is fetched via its connector's `fetch()` and registered with `register_uploaded_source`. `connector_kind` = the real connector kind; `source_metadata_jsonb` ⊇ `{doi, openalex_id, doi_verdict}` when scholar verified it (any DOI-bearing result is `verify_dois`-checked; `ok=False` results are dropped, never ingested). Normalize jobs enqueue as usual.
- **reflect** (LLM, purpose `research.reflect`): from accumulated summaries, decide `gaps` → new subqueries, or `done`. **Bounds:** `max_iterations` (deep=3, shallow=1). **Plateau cutoff:** an iteration ingesting 0 new sources stops the loop regardless.
- **compose** (LLM, purpose `research.compose`): writes `body_md` citing sources as `[cN]` markers; `aleph_scholar.style_pass` (LLM-free) tidies whitespace here (its `[N]`/`## References` renumbering is intentionally inert on research bodies — the `[cN]` markers are stable source-order keys aligned to `citations_by_marker`, so renumbering would desync them); builds a `ResearchReport` dataclass (`topic, body_md, summary, sources, citations_by_marker, claims`).
- **synthesize**: hands the report to the **unchanged** `SynthesisWorkflow`, which commits draft pages + a **pending `SynthesisProposal` in Briefs** (never published directly). Enqueues `curate_page_job` per committed page.

Settings (workers): `research_max_iterations_deep=3`, `research_max_iterations_shallow=1`, `research_max_sources_per_iter=6`, `research_max_total_sources=15`.

## Tool binding + allowlist enforcement

- The document-output research connector set — `tavily, openalex, arxiv, semantic_scholar, exa, serper, rss, lens` — lives in the module-level `RESEARCH_CONNECTOR_FACTORIES` map (`aleph_research.tools`). Binding is by direct factory lookup, not a global registry.
- At job start, `resolve_bound_tools(project_id)` resolves `Connector ⋈ ConnectorBinding` where enabled (an explicit binding beats `enabled_by_default`), resolves each connector's credential via `ConnectorCredentialService.decrypt_for_callback` **in-process** (local-mode env fallback included), and instantiates **only** those connectors' factories. **A disallowed connector is never constructed and never bound into the graph.** The search node emits a `research.tools` agent-event listing the bound kinds.
- Consensus is **not** bound into the worker loop — it is the Live researcher subagent's quota-metered screening tool (below). The loop's discovery needs are OpenAlex/web.

## Failure semantics (no strands)

The job wraps the whole graph in try/except; any interruption converges to a terminal `failed`:

- Any in-graph exception → caught → `AgentRun` marked `failed` with `error_text`, `completed_at`, and a `phase_failed` event.
- `asyncio.CancelledError` (arq `job_timeout` 600s or worker shutdown) → marked `failed` best-effort, then re-raised.
- A hard worker kill leaves the run `running`; arq re-enqueues (`retry_jobs=True`) and the job's **retry-guard** (`status != "pending"` at entry) marks it `failed` without re-running the graph — a re-enqueue can never duplicate ingested sources.

The run is enqueued only **after** its row commits, so the worker never sees a missing run; an already-terminal run re-delivered by arq is an idempotent no-op. The deferred-submit + poll + slot-leak failure classes no longer exist in code.

## The `/synthesize` re-target

`POST /v1/projects/{id}/synthesize` (EDITOR) keeps its contract `{topic, depth}` → `{agent_run_id}` but now creates `AgentRun(agent_kind=depth_kind, status=pending)`, writes ledger `synthesize.dispatch`, and enqueues `deep_research_job`. `agent_kind` is `"deep_research"` | `"shallow_research"`. Proposal list/approve/reject routes are unchanged. `bootstrap_project_job` per-topic fan-out enqueues the same native job.

## Cost + provenance

Every research LLM call goes through `LiteLLMClient.chat(...)` (gateway), auto-writing a `ModelCall` + `CostLedgerEvent` with `purpose="research.*"` and the run's `agent_run_id` (rule 5). Every ingest writes `source.create` + `source_version.create` ledger events (rule 4). Credentials decrypt in-process and never leave the worker.

---

# Scholar (`aleph-scholar`)

A pure-HTTP service package — **zero LLM calls** (no `LiteLLMClient`, no `ChatOpenAI`, no provider SDK; enforced by grep). Deps: `httpx`, `tenacity`, `mcp` (the codebase's first MCP client). No workspace deps — credentials and persistence are injected as async callbacks; redis is duck-typed via a `RedisLike` Protocol.

## Public API

```python
async verify_dois(dois) -> list[DoiVerdict]
async crossref_lookup(query, *, rows=10) -> list[WorkRef]
async search_openalex(query, *, per_page=10) -> list[WorkRef]
async expand_citations(ref, *, direction="both", limit=25) -> CitationExpansion
async search_consensus(project_id, query, *, filters=None) -> ConsensusResult
def   extract_dois(text) -> list[str]
def   style_pass(markdown) -> str
```

## Tri-state DOI verification

`DoiVerdict.ok` is `True` (resolves), `False` (**authoritative 404 on both** Crossref and OpenAlex), or `None` (network-unverifiable — timeout/429/5xx). Consumers MUST treat `None` as "do not flag." Retraction detection: OpenAlex `is_retracted` (Retraction-Watch-backed) is primary; Crossref `retraction` relations corroborate. `expand_citations` returns backward (`referenced_works`) and forward (`cites:{id}`) lists. `style_pass` is a deterministic, property-tested, idempotent citation-renumber + reference-rebuild pass.

Politeness: one shared `ScholarHttp` wrapper sends `mailto` (`ALEPH_SCHOLAR_MAILTO`), a per-host 1 req/s token bucket, and `gateway_retry`-style tenacity retry. OpenAlex batches up to 50 DOIs per request.

## Consensus over MCP + OAuth

Transport is the MCP streamable-HTTP client against `https://mcp.consensus.app/mcp`. The credential is a `consensus` connector kind; its encrypted `cipher_blob` holds `{client_id, token_endpoint, refresh_token, access_token, access_token_expires_at, status}`, stored/rotated exclusively through `ConnectorCredentialService`. Bootstrap via `scripts/connect-consensus.py` (RFC 9728/8414/7591 discovery + PKCE loopback auth — **requires the user at a browser**). On expiry scholar refreshes at the token endpoint; a rotated refresh token re-upserts the blob (ledgered, redis-locked per project). An authoritative refresh rejection (HTTP 400/401) sets `status="reconnect_required"` (queryable, never a 500). A redis monthly counter caps searches at `ALEPH_CONSENSUS_MONTHLY_SEARCH_CAP` (default 200); at cap the tool returns a quota message and performs zero HTTP calls. Consensus is for screening/evidence questions; OpenAlex for bulk discovery.

## Routes + researcher wiring

`apps/api/.../routes/scholar.py` (VIEWER read, EDITOR ingest): `verify-dois`, `search`, `expand-citations`, `consensus-search` (enforces the project's `consensus` `ConnectorBinding` → 403 `connector_disabled` when off). The **researcher subagent** holds `verify_dois`, `search_openalex`, `search_consensus`, `expand_citations`, and `ingest_paper(...)`. `IngestUrlIn` + `register_uploaded_source` accept optional `connector_kind` + `source_metadata` so scholar-ingested papers carry real provenance.

## Reviewer citation pass

A deterministic `doi_verification` node in the **MechanicalReviewer** graph runs `extract_dois` over the revision body + source metadata, batches `verify_dois` (≤50/run), and emits `fabricated_doi` (high; `ok=False`) and `retracted_source` (critical; `retracted=True`) findings. `ok=None` yields **no finding**. Verdicts cache back into `source_metadata_jsonb.doi_verdict` (same transaction, ledger `source.update`). The `retracted_source` finding funnels through the same `retract_source` service as manual and scholar-auto retraction (see `wiki.md`).
