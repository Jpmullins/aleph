# CLAUDE.md

Guidance for Claude Code working in this repository.

**This file describes what is true.** Where something is planned but not built, it says so. Where
something is built but broken, it says so. The previous version of this file asserted invariants that
were false in code and CI enforcement that did not exist, and that is the single reason a broken
retrieval path survived seven work packages. Do not restore that style. If you add a claim here,
verify it first; if you cannot verify it, mark it `PLANNED` or leave it out.

---

## What Aleph is

A **general-purpose, self-improving multi-agent harness.**

The product thesis: an agent that **authors plugins for itself and activates or deactivates them as
needed**, on a kernel whose composability model makes that safe — with guardrails preventing it from
removing load-bearing capability. The kernel is the product.

The existing research capability — ingest, scholarship, a belief layer, the research loop — ships as
the **first plugin suite**, not as the thing itself.

Within that suite, the durable knowledge layer is a **web of belief**: claims are first-class,
evidence-anchored, and revised as sources are added, contradicted, or retracted. Prose (HTML
artifacts, reports) is *rendered from* that layer, never the layer itself.

## Where the project is right now

Aleph is **mid-transition on two axes at once.** Be careful: much of the code predates both.

**Axis 1 — the knowledge layer. TWO knowledge plugins, and both stay.** See `docs/decisions.md` D1,
which supersedes the old decision that the wiki was being deleted.

- **The wiki** — what the project *concluded*. Synthesised pages, claims, citations, category hubs,
  a governed tag vocabulary. Curated and cross-linked; a thing a person reads. It now has a real
  schema (`docs/wiki-schema.md`) with validation on the write path, sixteen lint checks, derived
  hubs and an index, and classification. **It is not legacy and it is not being removed.**
- **RAG over the raw collection** — what the project *collected*. Every ingested source, chunked and
  indexed, searched directly so an answer can be grounded in the actual passage rather than in
  somebody's summary of it. `aleph_rks.retrieval.search_corpus` fuses a dense (pgvector cosine) and
  a lexical (`ts_rank`) ranking with RRF at k=60; the 45-pair eval reports recall@1 0.91 hybrid vs
  0.60 lexical-only.
- **⚠ The RAG is currently dead in production.** `document_chunks` has **0 rows** against 75 ingested
  sources: the profile binds the embedder to `titan-embed-v2` and the gateway serves
  `titan-embed-text-v2`. Chunks are written only *after* the embed returns, so one wrong name also
  killed the lexical leg, which needs no model. The measured 0.91 is against seeded fixtures, not
  against anything a user can search. Fix first — `docs/plan.md` `WS-RS1`.
- **The Claim Spine** (`docs/belief-engine.md`) is the evidence layer *underneath* the wiki, not a
  replacement for it: durable claims with verbatim quotes anchored to exact character offsets, so a
  page's assertions are traceable to a sentence. It has never run — 786 claims, 0 edges, 0 verbatim
  quotes, and `BeliefService` has no callers. See `WS-RS8`.
- **They are different plugins and both are fully accessible.** *"What do we think about X, and on
  what evidence?"* is the wiki. *"What did source 47 actually say?"* is the RAG. Framing them as
  competitors is what produced the removal decision, and it was a false choice.
- **The wiki now has a schema** (`docs/wiki-schema.md`). Ported from the hermes-agent `llm-wiki`
  skill — the harness that built `~/wiki/ai-research` — and stored as data, not as a document, so
  `WikiSchema.validate_page` runs on the write path. Per-project domain, category list, controlled
  tag taxonomy, page thresholds. `POST /wiki/schema/propose` derives one from the corpus that
  actually exists, because the shipped default describes AI/ML research and is the wrong taxonomy
  for any project that is about something else. A wrong taxonomy is worse than none: it gives every
  page a plausible-looking home, so nothing reports a problem while the categories stop meaning
  anything. This is governance for the wiki as it stands, not new capability built on it.

**Axis 2 — the harness.** Aleph is being rebuilt on an own-implemented kernel modelled on the
spatiotemporal-composability paper (revertible effects, reactive coeffects, scoped capability access).
**The kernel is Python** — see `docs/decisions.md` D5, closed 2026-08-21. Also assume Python for the belief/scholarship plugins, which are bound to
Postgres and the transactional ledger.

Today both processes boot on it: `apps/api/src/aleph_api/lifespan.py` and
`apps/workers/src/aleph_workers/arq.py` each mount a boot manifest (`aleph.toml`) onto a `Kernel`, and
the shared services are kernel capabilities declared in `packages/aleph-runtime` — the composition
root, with a live read-path probe per capability and LIFO unwind on shutdown.

**Unchanged and healthy on both axes:** ingest/RKS, `aleph-scholar`, the action ledger, model routing,
the sandboxed code runner, the asset store.

The wiki is **actively developed**, not legacy. The rule that used to sit here — *"treat wiki code as
legacy under removal; do not extend it, do not fix its cosmetics, do not add tests to it"* — is
deleted. It was made when the Claim Spine was expected to replace the wiki as the retrieval surface.
That replacement never ran, the wiki became the working knowledge layer, and the rule was violated
comprehensively and correctly. `docs/decisions.md` D1 records why.

### Recently landed, and worth knowing before you touch these areas

- **Model discovery is gateway-driven.** Aleph ships no model list and no price list — see
  *Rules that are real but only held by review*, below.
- **The workspace shell** — `Rail`, `ContextBar`, `PipelineStrip`, `ReadingRegion`, `AssistantDock`,
  `GroundingSurface` under `apps/web/src/components` and `apps/web/src/a2ui/components`. The pipeline
  strip is fed by `GET /v1/projects/{id}/pipeline`. The right-hand strip of surface **tabs** is gone:
  the reading region tiles up to three **panes** (`MAX_PANES` in `apps/web/src/lib/workspace-ui.tsx`),
  and `SurfaceStreamProvider` gives the whole region one multiplexed SSE connection, so a pane is
  purely a renderer for one `surfaceId` and owns no transport of its own.
- **One canonical A2UI catalog.** `packages/aleph-a2ui/src/aleph_a2ui/catalog.json` is the only
  editable copy; `apps/web/src/a2ui/catalog.ts` and `apps/copilot-runtime/src/catalog.generated.ts`
  are generated from it by `scripts/gen_catalog.py`.
- **Four page statuses, two of which are queues for a person — and they are not the same queue.**
  `stub` is a red link: something linked to a title nobody wrote, nobody proposed it, it is not work.
  `planned` is a title that earned a page by being cited enough — a queue for WRITING, allowed to be
  long. `draft` has content and is a queue for REVIEW. `approved` is settled. Filing stubs as `draft`
  put 235 empty pages in front of an approver alongside 15 real ones; "approve this" is not a question
  you can ask about a page with no content. The promotion threshold is 5, not the hermes 2, because
  Aleph extracts links mechanically from compiled prose where a mention is free — measured on the real
  corpus, 2 selects 477 of 600 linked stubs, which counts how common a phrase is.
- **Claim → chunk grounding.** `aleph_rks.claim_grounding.chunks_for_claim` (deterministic token
  overlap, no LLM) fills `Citation.chunk_ids` at commit time, so the claim → chunk → char-span chain
  is populated on the real write path instead of only in fixtures. The span at the bottom of that
  chain is exact: `packages/aleph-rks/tests/test_chunk_offsets.py` asserts
  `markdown[char_start:char_end] == chunk.text` over real documents.

### A standing constraint

**Reference implementations are read, not depended on.** `deepseek-harness`, `cordis`, `prime-agent`
and `graphify` are MIT and are blueprints to reimplement and improve on. Do not add any of them — or
any `@deepseek-ai/*` package — as a runtime dependency. Ported code carries a `NOTICE`.

---

## Commands

```bash
# Setup
cp deploy/compose/.env.example deploy/compose/.env   # then set INSIGHTS_LITELLM_API_KEY
./scripts/bootstrap-local.sh                          # boot the stack
uv sync --all-packages --all-extras                   # Python deps (MUST be --all-packages)
pnpm -C apps/web install                              # JS deps

# Quality gates
uv run ruff check .
uv run ruff format --check .
uv run pyright                                        # strict, must be 0 errors
pnpm -C apps/web lint
pnpm -C apps/web build                                # tsc --noEmit && vite build
./scripts/check-catalog-generated.sh                  # generated catalogs match catalog.json
./scripts/check-graph-state-keys.sh                   # no LangGraph node writes an undeclared key

# Tests
uv run pytest -m "not integration" -q                 # unit
uv run pytest -m integration -q                       # needs postgres+redis (see CI for env)
uv run pytest path/to/test_file.py::test_name         # single test

# Acceptance — the per-part table in docs/acceptance.md
./scripts/acceptance.sh                               # everything runnable here
./scripts/acceptance.sh --quick                       # skip anything needing services
./scripts/acceptance.sh --self-check                  # prove the checks can fail

# Retrieval number (needs a gateway with a chat model + an embedder)
uv run python -m aleph_evals.retrieval_eval

# A2UI catalog: edit catalog.json, then regenerate
uv run python scripts/gen_catalog.py

# Migrations (Alembic lives in apps/api)
cd apps/api && uv run alembic upgrade head
cd apps/api && uv run alembic check                   # CI asserts no model drift
cd apps/api && uv run alembic revision -m "<slug>" --autogenerate

# Local stack
docker compose -f deploy/compose/docker-compose.yml up -d
docker compose -f deploy/compose/docker-compose.yml logs -f aleph-api
./scripts/verify-gateway.sh                           # LLM gateway sanity check
```

Endpoints after bootstrap: web `:5173`, api `:8000`, copilot-runtime `:4000`, Langfuse `:3000`.
An S3-compatible object store is opt-in (`docker compose --profile s3 up -d`); the default asset
backend is the local filesystem at `data/assets`. Aleph serves no models and ships no gateway:
point `LITELLM_BASE_URL` at any OpenAI-compatible endpoint — see `docs/operations.md`.

---

## Layout

`uv` workspace (Python 3.13, pyright strict) + `pnpm` workspace (`apps/web`).

```
apps/
  api/              FastAPI — HTTP + SSE, owns Alembic, hosts the in-process agent. Boots on the kernel.
  web/              React 19 + Vite + Tailwind + @a2ui/react + CopilotKit
  workers/          arq workers — ingest, research loop, reviewers. Boots on the same kernel manifest.
  code-runner/      sandboxed, credential-less, network-partitioned Python executor
  copilot-runtime/  Node bridge (:4000) — thin AG-UI adapter + the generated catalog
packages/
  aleph-core          primitives, Pydantic schemas, UUIDv7, grounding/defang. LEAF — imports nothing else.
  aleph-kernel        the composability kernel — effects, capabilities, manifest, AST gate, skills, spawn ledger
  aleph-runtime       the composition root — shared services as kernel capabilities, each with a probe
  aleph-db            SQLAlchemy ORM + repositories + ledger
  aleph-security      auth, Principal, JWT, role gates, agent tokens
  aleph-observability OTEL + Langfuse + structlog
  aleph-models        LiteLLMClient, gateway discovery + hints, pricing, ModelProfile resolver
  aleph-scholar       Crossref/OpenAlex/Consensus, DOI verification. Pure HTTP, ZERO LLM calls.
  aleph-rks           Raw Knowledge Store — sources, normalization, chunks, embeddings, hybrid retrieval
  aleph-belief        web of belief — patch contract, trust lattice, deterministic reconciliation
  aleph-research      deep-research loop (plan→search→ingest→reflect→compose)
  aleph-connectors    typed connector plugins + encrypted ConnectorCredential
  aleph-assistant     chat orchestration + retrieval
  aleph-reviewer      verification passes
  aleph-hypotheses    analyst hypotheses + the derived-confidence engine
  aleph-artifacts     builder agent, rendered assets, exporters
  aleph-a2ui          A2UI catalog (`catalog.json`) + Python SDK glue
  aleph-notes         analyst notes
  aleph-datasets      Dataset / DatasetVersion / Observation
  aleph-evals         eval runner, scorers, and the retrieval eval that calls the real path
  aleph-wiki          the wiki knowledge plugin — pages, schema, lint, hubs; hosts the Claim Spine write path
```

21 workspace packages. `docs/acceptance.md` E4 asserts the count does not grow; the reduction comes
with the wiki deletion.

**Strict DAG, higher → lower.** `aleph-core` is the leaf. Apps depend on packages; packages never
depend on apps; no cycles. `aleph-scholar` carries no workspace deps.

---

## Rules that are actually enforced

1. **Pyright strict, 0 errors.** CI fails otherwise.
2. **Ruff clean**, line-length 100, `target-version = py313`.
3. **`alembic check` produces no diff.** New schema → new migration; never edit an existing one.
   Pattern: `apps/api/alembic/versions/YYYYMMDD_HHMM_<slug>_<message>.py`.
4. **Tests split by marker.** `@pytest.mark.integration` for anything needing postgres/redis.
5. **The A2UI catalog has exactly one editable copy.** Edit `catalog.json`, run
   `scripts/gen_catalog.py`; `scripts/check-catalog-generated.sh` fails on a hand-edited generated
   copy or a regenerated-but-not-committed one. Three hand-maintained copies previously disagreed
   about `ClaimCard.confidence` in ways no test noticed.
6. **Every surface prop a producer binds is declared in the client's zod schema.**
   `scripts/check-surface-bindings.sh`. The A2UI binder resolves ONLY declared props, so a producer
   binding the client never declares is dropped in silence — the SSE payload is correct, the view
   reads `undefined`, nothing raises. The wiki surface shipped ten categories and a health summary
   this way and rendered as though the project had no categories.
7. **Every key a LangGraph node writes is declared on its state `TypedDict`.**
   `scripts/check-graph-state-keys.sh`. Undeclared writes are discarded silently, so the reader's
   `state.get(k, [])` returns empty and the feature is inert while every step reports success; four
   shipped defects had exactly this shape.
8. **The agent path talks to the gateway and nothing else.** Every agent `ChatOpenAI` — orchestrator
   and every subagent — is built by the one constructor `copilot_agent._gateway_chat_model`, pointed
   at the LiteLLM gateway. `test_subagents.py::test_subagent_model_points_at_gateway` asserts the
   built model's `base_url` is the configured gateway, and `test_agent_gateway_base_url.py` pins the
   URL shaping (`/v1` exactly once, never the raw setting). Neither is a sweep: they pin the
   constructor that exists, not the absence of a new un-pointed one. The wider rule (non-agent code
   goes through `LiteLLMClient`; no provider SDK is called directly) is held by review only.

## Rules that are real but only held by review

These are genuine design commitments with no automated enforcement. Do not describe them as enforced.

- **Every row carries `project_id`**, plus `created_at`, `updated_at`, `created_by`, `access_scope`.
  The only exception is `ModelProfile` templates. No sweep checks this; new models are caught in
  review or not at all.
- **Every state mutation writes an `ActionLedgerEvent` in the same transaction.** Hash-chained,
  append-only. Verified by hand and by targeted integration tests, not by a sweep.
- **Every LLM/embed call writes a `ModelCall` + `CostLedgerEvent`.** The agent path partially bypasses
  this — see Known broken.
- **Agents never write state directly.** They call typed service methods; workers re-enter the API
  over HTTP with a minted agent token.
- **No agent-emitted code runs in the app context.** Agent code runs only in `code-runner`; its output
  is a versioned artifact rendered in a `sandbox` iframe. No agent-emitted SQL.
- **`ModelProfile` resolves capability → model; the gateway decides what those models are.** Call
  sites pass a `Capability` and the project profile; `LiteLLMClient` resolves the binding. Aleph ships
  no model list and no price list: `aleph_models.discovery` reads what it can from the gateway,
  preferring `/model/info` (modes, context windows, capability flags, exact rates) and falling back to
  `/v1/models` (ids only) when the virtual key is restricted to `llm_api_routes`, which is the normal
  case. `aleph_models.hints` fills unreported fields from an operator-editable file
  (`ALEPH_MODEL_HINTS_PATH`), never overriding the gateway and always labelled `static`. This feeds
  the Settings picker and `POST /v1/projects/{id}/model-profile/autoconfigure`, which chooses by
  *requirements over metadata* (mode, context window, tool/vision support, price) — never by model
  name — and probes each model before binding it, because a gateway's list states configuration, not
  reachability. Priced models outrank unpriced; a capability no available model satisfies is left
  **unbound** rather than bound to a guess.
- **Cost provenance is recorded, never assumed.** `aleph_models.pricing` stamps `pricing_source` on
  every priced row — `gateway` (reported), `static` (asserted from hints), `unknown` (unpriced) — plus
  the rates used, so a cost stays re-derivable and an unpriced call is never a silent `$0`.

---

## Known broken — do not trust these

Verified against the tree as merged. Fix or delete; do not build on top of. Entries move to *Fixed*
below only with a test that would have caught them.

- **`commit_revision` is not atomic on the path agents use.** It only row-locks when given a
  `page_id`; the by-title path (`_lock_or_create_page` with `page_id=None`) returns the page unlocked
  and computes `revision_no` as `max+1`.
- **Cost attribution has a hole.** `AgentCostCallbackHandler` writes a `ModelCall` only when the
  response carries token usage *and* a project id is resolvable from the run metadata; a
  `ChatOpenAI` response without usage (no `stream_usage=True`, or a provider that omits it) is
  silently uncosted. `test_skips_when_no_usage` / `test_skips_when_no_project_id` pin the current
  behaviour, not the desired one.
- **A green `audit/run.sh` is weaker evidence than it looks.** It probes a live stack, and a check
  whose precondition is missing exits `skip` (e.g. "no project has recorded LLM spend yet"). `run.sh`
  labels skips rather than hiding them, but a run over an empty stack exercises almost nothing while
  reporting no failures. `scripts/acceptance.sh` — which counts skips separately and can verify its
  own checks fail (`--self-check`) — is the gate to trust.
- **The Node runtime bridge is an open proxy on port 4000.**
  `apps/copilot-runtime/src/server.ts` constructs `new HttpAgent({ url: AGENT_URL })` with no
  headers, and the port is published. Anything that can reach it can drive the agent. This is a
  real problem in the only mode Aleph runs, and is `WS-D3` in `docs/plan.md`.

### Fixed, with the test that pins each

**2026-08-15 → 2026-08-17 (the harness refactor, and the gateway/shell merge)**

- **The agent endpoint bypassed auth middleware.** `/copilotkit` sat on the middleware skip list on a
  comment promising the handler verified callers; it performed no verification, and the agent's tools
  took their project scope from a client-supplied thread id. Two independent defences now stand:
  `_SELF_AUTH_PREFIXES` is empty, so the endpoint is authenticated like every other route
  (`test_copilotkit_auth.py::test_copilotkit_is_not_exempt_from_auth`,
  `::test_no_blanket_auth_exemption_prefixes`); and the project a request names is checked at both
  ends — at the HTTP boundary by `middleware/agent_scope.py` (`test_agent_thread_scope.py`, incl.
  `test_thread_parsers_agree`, which pins the extractor to the agent's own thread-id parser) and
  inside the graph by `copilot_agent`'s `_authorized` helper against the task-local principal
  (`test_agent_project_authorization.py`). Acceptance check F1.
- **An agent token's signed `project_id` was discarded.** The middleware now carries it onto the
  `Principal`, and `project_scope.py::_assert_credential_scope` refuses a token used against another
  project — for streams too. → `test_agent_token_project_scope.py`.
- **Retrieval was body-blind.** `wiki_index` now carries `body_text` with weighted `ts_rank`
  (title A / summary+aliases B / body C), the query gate ORs terms instead of ANDing them, and
  `search_corpus` searches the whole corpus. → `tests/e2e/test_retrieval_finds_body_text.py`
  (`test_body_phrase_retrieves_its_page`, `test_natural_language_question_retrieves_its_page`),
  `tests/e2e/test_search_corpus.py`. Acceptance B1–B4.
- **The eval scorers had no harness.** `python -m aleph_evals.retrieval_eval` runs the real retrieval
  path against a 45-pair labelled set and reports recall as a number, naming `hybrid` vs `lexical`
  mode and why. Acceptance B5/B6/B10.
- **A citation carried no anchor.** Retraction blast-radius no longer hangs on `source_page_id`,
  which was `None` at every write site: the join key is now `Citation.source_id`, written at commit
  time and what `aleph_reviewer.retraction` selects on. `source_page_id` survives as the older bridge
  key the citation popover, the refresh job and the curator resolve, and the legacy wiki ingest path
  links it after commit (`_link_citations_to_source_page`, which returns the row count so a caller can
  assert the write happened). Claim Spine citations also record the verbatim quote and a char span
  (`tests/e2e/test_belief_spine.py::test_every_written_citation_carries_a_source_id`, acceptance C7),
  and the legacy wiki write path fills `Citation.chunk_ids` via `aleph_rks.claim_grounding`
  (`packages/aleph-rks/tests/test_claim_grounding.py`). NOTE: the surface that renders that chain
  end to end is unpinned — its test went in the harness reset and `docs/plan.md` `WS-UI-4` restores
  it along with the pane's only route in.
- **Freshness could not tell a grounded page from a claimless one** — both scored 50, because
  `_citation_health([])` returned full marks and `_verification` short-circuited before checking
  whether any claim existed. Both now return 0 for an empty citation set. NOTE: the regression test
  went with the wiki test suite; the fix lives in `packages/aleph-wiki/src/aleph_wiki/freshness.py`
  and is currently unpinned.

**2026-08-14**

- **Stale-link expansion** — `retrieval/router.py` expanded links with no `src_revision_id` filter, so
  it walked every historical revision's links forever.
  → `test_expansion_ignores_links_from_superseded_revisions`.
- **Empty-search confabulation** — an FTS miss fell back to the most-recently-indexed pages and handed
  them to the composer as if they were matches. Now short-circuits with a diagnostic naming the likely
  cause. This is also what kept the retrieval audit check green.
- **`style_pass` ran on nothing** — it matched `[N]` while the research composer emits `[cN]`, so every
  report passed through unchanged. → six `[cN]` tests in `test_scholar_style.py`.
- **Citation expansion returned arbitrary papers** — backward sliced OpenAlex storage order before
  resolving; forward sent no `sort`. Both now rank by citation count.
  → three ordering tests in `test_scholar_citations.py`.
- **Rejection feedback never carried a row** — schema defaulted the reason to `""`, both handlers wrote
  feedback only `if reason`, and the UI hardcoded `""`. Reason is now required end to end.

---

## When adding to the system

- **New service method that mutates state** → write the `ActionLedgerEvent` in the same transaction,
  and add an integration test asserting the ledger row.
- **New LLM call site** → `LiteLLMClient.chat()`/`.embed()` with a `Capability` and a `purpose`.
- **New row type** → `project_id` + `access_scope`. No globally-scoped tables.
- **New A2UI component** → edit `packages/aleph-a2ui/src/aleph_a2ui/catalog.json`, run
  `scripts/gen_catalog.py`, and ship the renderer and the producer in the same change. Surface
  components render only from bound props — no self-fetch. The sweep that enforced that is gone; the
  pane model makes it structural, since a pane owns no transport.
- **New connector** → implement `search`/`fetch`/`normalize`, register in `get_registry()`, declare
  `output_kind`. Credentials come from `ConnectorCredential`, never from container env.
- **New Python package** → add to `[tool.uv.workspace] members`, `[tool.uv.sources]`, and both the
  ruff `src` and pyright `include` lists in the root `pyproject.toml`; then `uv sync`.
- **New kernel capability** → declare it in `packages/aleph-runtime/src/aleph_runtime/capabilities.py`
  with an inverse next to the thing it undoes and a probe that exercises its real read path. A
  capability that cannot answer a live query must not come up.
- **Ported code** → add a `NOTICE` recording upstream, license, and per-file lineage. See
  `packages/aleph-belief/NOTICE`.

**Ship a consumer with every producer.** The dominant defect class in this codebase is a column,
table, or service that is written correctly and read by nothing. A contract with no caller is not
progress. If you add a write path, add the read path in the same change or do not add it.

## Naming

Distribution `aleph-xxx` · module `aleph_xxx` · tables plural snake_case · action kinds
`<entity>.<verb>` · OTEL spans `<subsystem>.<op>`.

## Docs

**Start here if you are picking this up fresh:**

1. **`docs/plan.md`** — the work. 59 workstreams, 348 criteria that can fail, each with what it is in
   plain language, why, how, a review step and an iteration step. Read **Part 0 first**: the
   acceptance gate is currently certifying things that are false, and repairing it comes before any
   other work. Part 1 is the eight numbers that mean "done". Part 7 lists every LangChain and
   CopilotKit doc to read, mapped to the workstreams that need it — **do not work from memory on
   those APIs**, use the `docs-langchain` and `copilotkit-mcp` MCP servers.
2. **`docs/decisions.md`** — the dated decisions and their reasoning. D1 (two knowledge plugins, the
   wiki stays), D5 (kernel is Python), D6 (OIDC removed), D7 (three unread concepts deleted).
3. **`docs/backlog.md`** — the inventory the plan was built from, including the measured UI audit
   and the Deep Agents adoption table (sync/async, install, beta status).

**The single most important fact before doing anything:** retrieval is dead on the deployed
instance. `document_chunks` has 0 rows against 75 sources — the profile binds an embedding model
name the gateway does not serve. Everything that measures retrieval is measuring an empty table.
`docs/plan.md` `WS-RS1`.

**Reference:**

- `docs/architecture.md` — what exists today, honestly, including the security posture
- `docs/acceptance.md` — the parts table. Note Part E is withdrawn (there is no wiki deletion)
- `docs/belief-engine.md` — the Claim Spine design; note it has never run
- `docs/wiki-schema.md` — the wiki's governance: schema, statuses, thresholds, lint, links
- `docs/operations.md` — stack, migrations, gates, pointing Aleph at any OpenAI-compatible endpoint
- `docs/html/` — `plan.html` and `backlog.html`, the same content as readable standalone pages
- `docs/update/` — pre-refactor audit reports (2026-07-26, against `bcc478a`). History only;
  **superseded** wherever they disagree with the documents above.
