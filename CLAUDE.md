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
- **The RAG is alive again as of 2026-08-21.** It had been dead: `document_chunks` held **0 rows**
  against 75 ingested sources, because both profile templates bound the embedder to
  `titan-embed-v2` and the gateway serves `titan-embed-text-v2`. Chunks were written only *after* the
  embed returned, so one wrong word also killed the lexical leg — which needs no model at all — and
  45 index jobs sat in `running` with no error recorded anywhere. Measured after the repair:
  **3,451 chunks, all embedded, 0 normalized documents without chunks, 0 stuck runs.**
  Re-measured 2026-08-24 after further ingest: **42,226 chunks, 41,530 embedded (98%)** across
  2,201 sources, and `POST /wiki/search` returns ranked results on the real corpus. Aleph now
  ships no embedder name; the binding is chosen by autoconfigure from what the gateway reports and
  probed before use. `WS-RS1`, landed.
  **The 0.91 laboratory number is gone, and the real one is worse.** `WS-RS5` landed: the eval now
  runs the production chunker, embeds the same string ingest embeds, and reports nDCG and MRR
  alongside recall. Measured against a 738-document set built from this instance's own corpus
  (`python -m aleph_evals.build_retrieval_set`): **MRR 0.443, recall@1 0.34, @3 0.51, @8 0.64,
  @20 0.78.** The nDCG figure first recorded here — 0.681 — was **withdrawn on 2026-08-22**: the
  metric was unbounded above. `_ndcg_at` credited a source once per CHUNK it contributed while the
  ideal denominator counted distinct sources, so ten chunks from the one correct source scored
  4.54 out of a possible 1.00 and scored higher the more the ranking repeated itself. Fixed
  (each source credited once, at its best position) and pinned by
  `packages/aleph-evals/tests/test_ndcg.py`, which asserts the bound exhaustively over every
  ranking of up to four hits. **The nDCG number has not been re-measured on the 738-document set
  since the fix** — the recall figures are unaffected, and no nDCG should be quoted until that run
  happens. The old 12-document set scored **recall@1 = 1.00** — saturated, and unable to
  resolve any change RS6 or RS10 might make. The generated set is NOT committed: the corpus is
  published papers and redistributing them is not the eval's call. Point `ALEPH_RETRIEVAL_DATASET`
  at a generated one; CI measures the small committed set, lexical-only, with a floor on recall@1.

  **Retrieval never abstains.** Asked a question its corpus cannot answer it returns passages
  anyway, 8 times out of 8. A cosine-distance floor does not fix it — measured, the answerable and
  off-corpus distributions overlap (`docs/decisions.md` D10). `ChunkHit` now carries
  `cosine_distance` and `lexical_rank` so a reranker can; both legs had been computing them and
  discarding them.
- **The Claim Spine** (`docs/belief-engine.md`) is the evidence layer *underneath* the wiki, not a
  replacement for it: durable claims with verbatim quotes anchored to exact character offsets, so a
  page's assertions are traceable to a sentence. **It runs now.** `WS-RS8` gave it its first caller:
  `_node_claim_extraction` reads the source's CHUNKS, requires the model to quote them verbatim, and
  writes through `BeliefService`, so a claim carries a quote, a chunk and a document-relative span.
  The research path does too — it had been grounding every quote and then discarding all four values
  one function call later. Claims are also embedded at write time now (`WS-RS10`); the HNSW index on
  `wiki_claims.embedding` had never had anything to index. The 796 pre-existing thin citations stay
  and are re-derivable — `docs/decisions.md` D9. The rebuild has run: **number 3 is 0** in real
  projects as of 2026-08-22. `status.sh` prints the fixture-project count (15,558) beside it on its
  own line, so the scope of the zero cannot hide anything. See `WS-RS8`.
- **They are different plugins and both are fully accessible.** *"What do we think about X, and on
  what evidence?"* is the wiki. *"What did source 47 actually say?"* is the RAG. Framing them as
  competitors is what produced the removal decision, and it was a false choice.
- **The wiki now has a schema** (`docs/wiki-schema.md`). Ported from the hermes-agent `llm-wiki`
  skill — the harness that built `~/wiki/ai-research` — and stored as data, not as a document.
  **`WikiSchema.validate_page` is NOT on the write path**, and this file said it was until
  2026-08-22. Its only non-test caller is `aleph_wiki.lint`, which reports and never repairs, so a
  page carrying a tag nobody declared is committed and then found in a lint rather than refused at
  commit. `schema.py`'s own docstring has said so since it was written; the claim survived here
  because the test that pins it scans `docs/` and `packages/aleph-wiki/src` and this file is in
  neither. Per-project domain, category list, controlled
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
  the reading region tiles **panes** (`MAX_PANES` is 24, in `apps/web/src/lib/workspace-ui.tsx`),
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

### The plugin cluster, end to end

As of 2026-08-22 the thing CLAUDE.md opens by describing actually works, and it is worth knowing the
shape before touching any of it:

1. **Author** — `POST /v1/projects/{id}/plugins`, or the agent's `author_plugin` tool. The AST gate
   runs BEFORE anything is stored, so source with an import-time side effect leaves no row.
2. **Durable** — `plugins` table, `aleph_runtime.plugin_service`. Survives a restart; the workers
   reconstitute from it. A plugin that fails to mount is recorded `failed` with its reason rather
   than blocking every other plugin, which is the failure `Kernel.unregister` exists for.
3. **Reachable** — `aleph_kernel.agent_api.AgentPluginAPI`, five agent tools and five routes.
   `preview_removal` and the refusal read the same graph, so a refusal is always predictable.
   `author_plugin` and `disable_plugin` are withheld from the interpreter loop
   (`interpreter.PTC_WITHHELD`): PTC bypasses `interrupt_on`, and a loop that can force a disable
   can dismantle the system it runs on.
4. **A pane** — `PaneKind(builder=...)`. Each pane builds inside its own try/except, so a plugin's
   exception becomes one error surface rather than ending the multiplexed stream.
5. **Configurable** — `UIContribution` declares a JSON Schema and `settings_card` renders it with no
   browser code. Values land in `plugin_settings`. **A field declaring itself a secret is refused**,
   and secret-shaped keys are redacted before persistence — a settings value reaches `card_actions`
   AND the append-only ledger, so a credential there is plaintext forever. Credentials go through
   `ConnectorCredential`.

Core capability has no `plugin_id` at any layer. It is not refused, it is unnameable.

- **Reranking is wired, and it is a second stage that may never take down the first.**
  `Capability.RERANK` now has a production caller (`aleph_assistant.retrieval.router.
  _search_corpus`), a `CAPABILITY_POLICIES` entry (`mode="chat"` — a listwise reranker
  reorders by asking a chat model, and the old `mode="rerank"` matched nothing on any
  gateway), and a Settings row. Measure it with
  `uv run python -m aleph_evals.retrieval_eval --rerank both`, which prints the control
  and rerank arms side by side. On the committed 45-pair set: **+0.008 nDCG@10, +0.02
  recall@1** — small because that set is saturated (recall@8 = 1.00) and cannot show the
  0.05 the workstream asks for. See `docs/decisions.md` D12 for why an unreadable
  reranker reply is not an abstention; getting that wrong measured **0.970 → 0.133**.

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
  aleph-runtime       composition root — services as kernel capabilities; plugin durability + UI contributions
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
  aleph-evals         eval runner, scorers, and the retrieval eval that calls the real path
  aleph-wiki          the wiki knowledge plugin — pages, schema, lint, hubs; hosts the Claim Spine write path
```

20 workspace packages. `docs/acceptance.md` E4 asserts the count does not grow. There is no
reduction coming: `docs/decisions.md` D1 reversed the wiki deletion, and both knowledge plugins
stay.

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
8. **Every path named in a load-bearing doc, in a gate, or in a source docstring resolves.**
   `scripts/check-dead-refs.sh`. Four of this file's own "Fixed, with the test that pins each"
   entries named test files deleted in the harness reset, `pnpm-workspace.yaml` declared a member
   directory that was gone, and a dangling symlink under `audit/` made six e2e checks SKIP forever
   while the runner reported no failures. A citation that names nothing reads as evidence and is an
   assertion.
9. **Every ✅ row in `docs/acceptance.md` names a test that exists and that pytest can collect.**
   `scripts/check-acceptance-claims.sh`. It cannot check what a test asserts — nothing can — but a
   row claiming "49 tests" against a suite of 142, or citing a node id pytest cannot find, is the
   scoreboard asserting rather than measuring.
10. **The agent cannot write under `/skills`.** `scripts/check-agent-fs-permissions.sh`, which reads
   the *effective* permission decision rather than the presence of a `permissions=` kwarg: matching
   is first-match-wins, so an allow rule ahead of the deny reopens everything while a grep stays
   happy.
11. **Every agent and subagent survives a raising tool.** `scripts/check-agent-middleware.sh`.
   deepagents lets a subagent spec REPLACE the parent's middleware rather than extend it, so "the
   orchestrator has it" is not "the subagents have it".
12. **The agent path talks to the gateway and nothing else.** Every agent `ChatOpenAI` — orchestrator
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

- **`normalize_job` is not idempotent, and the health number reads the wreckage as
  lost documents.** It builds `NormalizedDocument(...)` unconditionally
  (`jobs/normalize.py:168`) — no upsert, no uniqueness on `source_version_id` — so
  re-normalizing a source, which is exactly what a parser change requires, leaves the
  previous row behind with no chunks. Measured 2026-08-22 after the docling rollout:
  12 source versions in `[RS11] docling ingest`, each with **two** normalized
  documents, one chunked and one dead. `status.sh`'s `unindexed_documents` counts
  those 12 and calls them *"ingested documents with no chunks at all"* — which reads
  as twelve sources nothing can find, and all twelve are searchable through their
  sibling. So there are two defects here and they should not be conflated: the write
  path has no idempotency, and the number does not measure what its sentence says.
  Neither is fixed. The number should count SOURCE VERSIONS with no chunked
  normalization, and print the superseded count beside it the way it already does for
  fixtures; the write path needs a decision on whether normalization history is worth
  keeping at all, which is not a decision to make while clearing a dashboard.

- **`commit_revision` is not atomic on the path agents use.** It only row-locks when given a
  `page_id`; the by-title path (`_lock_or_create_page` with `page_id=None`) returns the page unlocked
  and computes `revision_no` as `max+1`.
- **A green `audit/run.sh` is weaker evidence than it looks.** It probes a live stack, and a check
  whose precondition is missing exits `skip` (e.g. "no project has recorded LLM spend yet"). `run.sh`
  labels skips rather than hiding them, but a run over an empty stack exercises almost nothing while
  reporting no failures. `scripts/acceptance.sh` — which counts skips separately and can verify its
  own checks fail (`--self-check`) — is the gate to trust.
### Fixed, with the test that pins each

**2026-08-24 (the browser path, and three fixed defects that had stopped being guarded)**

- **Browser chat renders an assistant reply.** The only path a user actually has was dead — the
  spec timed out at 120s with the assistant dock rendered and neither the user's message nor a
  reply inside it, while `aleph-copilot-runtime` reported **healthy** throughout, because its
  healthcheck probes `/health`, the one route that is not the product. Three independent faults,
  none of them in the port/origin wiring that was inspected repeatedly and was correct the whole
  time: the interpreter middleware sat AFTER `CopilotKitMiddleware`, so it saw CopilotKit's
  frontend tools as plain dicts and `filter_tools_for_ptc` called `.name` on them; CORS sat inside
  `ErrorMiddleware`, so every 500 reached the browser with no `Access-Control-Allow-Origin` and
  presented as a network error rather than the 500 it was; and `CopilotKitProvider` was mounted
  with no `headers`, so there was no credential to forward. Green at ~1.2s, 4 runs.
  → acceptance **P4a**, now a hard-fail row.
- **Three FIXED defects were still on the expected-red path, where a regression does not fail the
  gate.** `run_expected_red_shell` records RED, and RED is reported as "known defects under test"
  rather than as a failure — correct while the defect stands, and silent cover the moment it is
  fixed. E5 (the belief patch contract now has four consumers), F6 (6 of 6 images run non-root),
  and P4a were all green and all unguarded. Promoted to `run_shell`. The gap is structural, not a
  one-off: fixing an expected-red row and leaving it expected-red is the default outcome, since
  the row goes green on its own and nothing prompts the change.
- **P4a was mutation-tested, not assumed.** A 1.2s pass on a path that used to take a 120s timeout
  is the shape of a check that stopped checking. Stopping `copilot-runtime` fails the spec and
  starting it passes, so the check is measuring the bridge rather than a cached page.

**2026-08-22, later (batch 8 — the gate stopped over-reporting, and PDFs stopped being flat)**

- **`acceptance.sh` ran 64 checks and `docs/acceptance.md` listed 46.** The whole plugin cluster
  A7–A11 — what this file's opening section calls the product — appeared on no scoreboard, and eight
  of this file's own "here is the evidence" citations named rows that did not exist (`C10` and `F7`
  exist nowhere at all). A row id is not a path, so `check-dead-refs.sh` could not see it, and rule 9
  runs doc → test. `check-acceptance-rows.sh` closes both missing edges: gate → doc, and prose → doc.
  → acceptance P13.
- **A ✅ row claimed "142 passed" against a suite of 165, through three audits**, because
  `check-acceptance-claims.sh` resolved paths and node ids and never counted anything. It counts now,
  for rows whose check is a bare `pytest <path>`. Two further citation shapes were invisible to it —
  a real test cited under the wrong directory, and a bare `::name` with no path — and both run as
  pytest exit 4 rather than as a pass or a fail, so a reader scanning for red sees neither.
- **A CI step with an empty `run:` reported success.** A bad merge put the web-test step's body
  inside the copilot-runtime step, so `npm ci` and `tsc` ran nowhere while their step went green.
  And `python-integration` supplied 3 of the 11 settings the app needs to boot; it has not gone red
  only because origin/main is behind. Both fixed; the eight added are load-bearing one at a time.
- **This instance's backup was not restorable, and the drill that would have said so had no caller.**
  postgres inherited Docker's 64 MB `/dev/shm`; `pg_restore` died building the HNSW index reporting
  "No space left on device" with 40 GB free. Fixed, pinned against the RENDERED compose config, and
  the drill is now acceptance row **P8a** — 68 tables, 1,064,741 rows, every count and content digest
  identical, the ledger hash chain re-derived on both sides.
- **Every sweep now has a self-check probe (0 unprobed, from 7),** which closes Part 0. Writing them
  found two defects in their own subjects: a class declared above the first `{` in a stylesheet was
  invisible to the dead-CSS sweep, and the restore drill above. `scripts/_acceptance/` probes had the
  same gap and the first three now have probes too.
- **Rule 11 was listed as enforced and could not fail.** `check-agent-middleware.sh` decided the
  subagent half with `if "AlephAgentMiddleware" not in text` — a MODULE-level string search that the
  import line satisfies. Emptying the retriever's middleware left it rc=0 with every tool that
  subagent carries one exception away from ending the turn. Now an AST walk of each builder's returned
  dict, plus `test_agent_tool_guard.py::test_every_subagent_spec_really_carries_the_guard`, which
  builds every subagent `pkgutil` can find.
- **PDFs were flat in production and four separate faults kept them that way**, each presenting as no
  error at all because `normalize_bytes` falls back to pdfminer: extras not installed, `do_ocr=True`
  with no OCR engine, `opencv-python` rather than headless (missing `libGL`), and a torch dlopen that
  ABORTS the process on arm64 so no exception is raised and arq records no failure. docling now runs:
  0 → 12 documents, null `section_path` NULL (unevaluable) → 0.0000 over 760 chunks. `WORKERS_MEM_LIMIT`
  2g → 4g, because docling peaks at 2,263 MB and an OOM kill is not an exception either.
  → `docs/measurements/pdf-layout-retrieval.md`: nDCG@10 +0.011 against a four-times-smaller noise
  floor, and section-label rate 0.02 → 1.00, which is the row no ranking metric can see.
- **A reranker switched itself off for the life of the process.** `skipped_reason` was set on an
  unreadable reply and never cleared, and `_search_and_rerank` reads it BEFORE calling `rank`; on the
  208-question set the first of 8 unreadable replies silently degraded the other 200.
- **Twelve kernel invariants the code upheld and nothing measured**, found by 84 mutations — shutdown
  ordering across a chain, a probe that RAISES, `has()` on an undeclared key, a PROBE-refused install,
  a negative spend. Almost every one was invisible because a test's ARRANGEMENT made the property
  unobservable, not because nobody thought of it.

**2026-08-22 (the plugin cluster, and the instruments)**

- **Cost attribution closed, in three deploys, and the third was found by a probe rather than by
  reading.** The agent path had no price list and no run to attribute to. Fixing it took: pricing
  (`WS-MEP-1`); reading the run id from `get_config()` rather than `runtime.config`, which
  LangGraph's `Runtime` deliberately does not have; and threading it into the retrieval router,
  whose three further model calls happen a layer below the middleware. Number 5 is **0 since the
  recorded cutoff** (`docs/attribution-cutoff.txt`), with the all-time count printed beside it —
  D9 keeps the historical rows rather than editing an append-only ledger to improve a number.
  → `tests/integration/test_chat_turn_is_recorded.py`,
  `apps/api/tests/unit/test_agent_cost_attribution.py`, acceptance H2.
- **The kernel is reachable.** `grep -rn "AgentPluginAPI" apps/api/src` returned **0**: the guardrail
  this project calls the product had no HTTP route, no agent tool and no graph node. Five tools and
  five routes now, with preview and refusal reading the same declaration graph.
  → `tests/integration/test_plugin_routes.py`, acceptance A8.
- **A plugin survives the process that installed it.** There was no plugin table anywhere in the
  schema, so an agent that improved itself forgot at the next deploy. The AST gate runs BEFORE the
  row is written; a failed mount rolls it back; one bad row cannot stop a process starting.
  → `tests/integration/test_plugin_durability.py`, acceptance A7.
- **A plugin can add a pane, and a broken one cannot blank the workspace.** `PANE_REGISTRY.extend()`
  advertised itself as the plugin seam while the thing that BUILT a pane was an if/elif chain that
  raised on unknown names — inside an unguarded loop feeding the one multiplexed SSE connection every
  pane reads from. → `tests/integration/test_plugin_panes.py`, acceptance A9.
- **A declared schema becomes a settings screen you can open.** `settings_card.py` was 279 working
  lines with no importer outside its own tests; its first caller was the SAVE handler, so the screen
  could only be seen by writing to it. → `tests/integration/test_plugin_settings_contract.py`,
  acceptance A11.
- **A settings secret no longer lands in plaintext in two append-only tables.** A field declaring
  itself a password rendered `variant: "obscured"` — hidden on screen, and persisted verbatim to
  `card_actions` and the ledger. Refused at the generator, redacted at the persistence boundary.
  → `packages/aleph-a2ui/tests/test_secret_redaction.py`,
  `tests/integration/test_action_params_are_redacted.py`, acceptance F8.
- **The chat bridge forwarded nobody's credential, and port 4000 answered every origin.**
  `WS-D3`. `CopilotKitProvider` was mounted with no `headers` prop at all, so there was nothing for
  the bridge to forward; the bridge itself ran `cors: true` — every origin, with credentials — so any
  page the user had open could drive their assistant, spend their tokens and write to their wiki,
  indistinguishably from the real UI. The browser now attaches the caller's bearer as a headers
  **object**: the installed CopilotKit accepts a *function* without complaint and serialises it to
  nothing, so the test pins the shape rather than the intent. Origins come from `ALEPH_CORS_ORIGINS`
  and the port is published on loopback. The probe checks forwarding in BOTH directions — a bridge
  that substitutes a credential of its own when the caller sent none passes a forwarding check that
  only ever sends one, and every anonymous request then reaches the API looking authenticated,
  attributed in the ledger to whoever that credential belongs to.
  → `apps/web/src/lib/copilot.test.tsx` (`it("passes headers as an object, not a function")`),
  `scripts/_acceptance/runtime_bridge_probe.mjs`, acceptance F4/F5.
- **The Inspector.** A chat turn is a recorded run with a tool timeline, and there is a pane that
  shows it — the only place an agent failure had been legible was the API container's stderr.
  → `tests/integration/test_inspector_surface.py`, acceptance C11.
- **The composition root has tests.** 783 lines deciding what a running Aleph consists of, and zero
  tests over it. A probe now demonstrably notices a dead dependency — an engine constructs fine
  against an unreachable host, so only a probe issuing a real query can tell.
  → `tests/integration/test_capability_probes.py`, acceptance A10.

**Instruments that could not fail, and now can**

- The eval **scorers graded the fixture's own answer** — `_run_dataset` loaded a JSON file and scored
  it without executing anything. `pass_rate: 1.0` meant the file agreed with itself.
- `python -m aleph_evals` printed `selected_datasets: []` and **exited 0**.
- `--min-recall` gated the top-k hit rate, which is 1.00 on the committed set whatever retrieval
  does. It gates recall@1 now, and the plan's own suggested mutation turns it red.
- Two **self-check probes mutated nothing** — a `^` anchor under `perl -0`, and a probe naming a
  migration a newer one displaced. A no-op mutation is now a hard failure.
- **Seven correct sweeps had no consumer**, across three batches. `check-sweeps-are-wired.sh` fails
  the build on the eighth.

**2026-08-21 (executing `docs/plan.md`)**

- **Retrieval was dead in production and nothing said so.** `WS-RS1`. Chunks are now written and
  committed *before* the embed call, so a dead embedder degrades to keyword-only search instead of an
  empty index; `RetrievalIndexRecord.state` makes `lexical_only` a real state instead of representing
  it by the absence of rows, which is indistinguishable from "never ingested". The dense leg filters
  `embedding IS NOT NULL` explicitly, because ordering by `cosine_distance` over NULL is undefined
  rather than last. `search_corpus(query_embedding=None)` means lexical-only — the degraded caller
  used to pass a zero vector, which is *not* equivalent: cosine distance to the zero vector is
  degenerate, so the dense leg returned an arbitrary page of rows and RRF fused that noise as a
  ranking. → `tests/integration/test_chunk_embed_degrades.py` (5),
  `packages/aleph-assistant/tests/test_router_degradation.py` (6), `tests/e2e/test_search_corpus.py`.
- **A run whose owning process died claimed to be running forever.** 45 `chunk_embed` runs sat in
  `running`, every one a failed index nobody had been told about. `reap_stale_runs` runs at API boot,
  fails them with a reason, and ledgers each in the same transaction — and deliberately does not reap
  a young run or one with no `started_at`, because an over-eager reaper kills live work.
  → `tests/integration/test_agent_run_reaper.py` (7).
- **A NUL byte made a document permanently unindexable and took the batch with it.** Postgres `text`
  cannot hold `\x00` at all, so one PDF aborted a 34-document repair at whatever position it
  occupied. `defang` now replaces NUL with U+FFFD — one character for one character, so grounding
  offsets into the stored document are unchanged — and the repair pass survives a single bad
  document instead of dropping everything behind it.
- **The agent could rewrite its own standing orders.** `WS-K1`. `create_deep_agent` was called with
  no `permissions=`, `FilesystemBackend` implements `write`, and deepagents allows any operation no
  rule matches — so the assistant could silently edit the bundled `SKILL.md` files on the live
  container, and text in an ingested page could in principle instruct it to.
  → `tests/unit/test_agent_skill_write_gate.py`, `scripts/check-agent-fs-permissions.sh`, which
  checks the EFFECTIVE decision rather than the presence of a kwarg (matching is first-match-wins,
  so an allow ahead of the deny reopens everything).
- **A failed agent run produced a stream that just stopped.** `WS-E1a`. Aleph owns the AG-UI route
  (`agui_endpoint.py`): an `except` emitting RUN_ERROR as the final frame, a terminal latch (upstream
  falls straight through from RUN_ERROR to RUN_FINISHED, and a client seeing both may reasonably
  conclude the run recovered), and one id shared by the frame, the log record and the
  `X-Aleph-Run-Id` header. → `apps/api/tests/unit/test_agent_endpoint_errors.py` (6).
- **Any one of 27 tools raising killed the conversation.** `WS-E1b`.
  `AlephAgentMiddleware.awrap_tool_call` turns an exception into a `ToolMessage(status="error")` the
  model can route around. `PermissionDenied` is handled distinctly and tells the model to stop and
  ask rather than try another id, so a friendly sentence does not become a way to keep probing.
  → `apps/api/tests/unit/test_agent_tool_guard.py` (11), `scripts/check-agent-middleware.sh`, which
  asserts all six subagents carry it — deepagents lets a spec REPLACE the parent's middleware.
- **`search_wiki` returned no identifier, so `open_page` had no reachable success path.**
  → `apps/api/tests/unit/test_search_wiki_returns_ids.py` (5).
- **The agent's Postgres pool held exactly one connection.** `WS-E1c`. `AsyncConnectionPool` defaults
  `max_size` to `min_size`. Every checkpoint, memory read and concurrent subagent queued behind it
  and gave up after 30s — "slow, then fails", with no error message. Timeout and retry are now
  settings, the SDK's own retry is **0** so two budgets cannot stack, and `awrap_model_call` backs
  off with jitter and honours `Retry-After`. → `test_agent_store_pool.py`, `test_agent_model_retry.py`.
- **A failed install left a ghost that broke every later install.** `WS-A1a`. `Kernel.unregister`,
  `install` cleaning up both failure branches, and `disable` handing back the handle it was given.
  → eight tests in `packages/aleph-kernel/tests/test_agent_api.py`; the suite is 150, was 142.
- **The gate itself was the thing it was built to prevent.** Part 0. `acceptance.sh` gained a
  MISSING status (always fatal), a preflight that resolves every path it names before anything runs,
  `--strict` / `--max-skip`, and a `services_up` that reads the configured host and port instead of
  probing localhost:5432 unconditionally. `scripts/check-dead-refs.sh` found 27 dead references
  including four of this file's own defect pins; `scripts/check-acceptance-claims.sh` found 23 ✅
  rows citing nothing at all. The self-check went from 6 mutations to 19, one per sweep by name.


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

1. **`docs/plan.md`** — the work. 58 workstream headings (two withdrawn) and 339 criteria, each with
   what it is in plain language, why, how, a review step and an iteration step. **Not '348 criteria
   that can fail'** — that was the old wording and it was the same mistake the plan exists to catch.
   Three audits found roughly fifteen criteria that can NEVER fail (a grep matching the prose that
   documents the fix, a SQL predicate returning NULL, a mutation string absent from its file, a node
   id whose command exits 4) and about as many that can never pass. They are corrected as they are
   found; assume more remain, and when a criterion looks green, check that it could have been red.
   Read **Part 0 first** — but read its status table, not its prose. Part 0 said the acceptance gate
   was certifying things that are false, and it was: `run_shell` read `tail`'s exit status through 24
   of 33 pipes, so a failing check behind a pipe reported PASS. That is fixed, and **five of Part 0's
   six items are done**. The one left is a self-check probe for the last seven sweeps. Part 1 is the eight numbers that mean "done". Part 7 lists every LangChain and
   CopilotKit doc to read, mapped to the workstreams that need it — **do not work from memory on
   those APIs**, use the `docs-langchain` and `copilotkit-mcp` MCP servers.
2. **`docs/decisions.md`** — the dated decisions and their reasoning. D1 (two knowledge plugins, the
   wiki stays), D5 (kernel is Python), D6 (OIDC removed), D7 (three unread concepts deleted).
3. **`docs/backlog.md`** — the inventory the plan was built from, including the measured UI audit
   and the Deep Agents adoption table (sync/async, install, beta status).

**Two commands before doing anything:**

```bash
./scripts/status.sh        # the eight numbers of docs/plan.md Part 1
./scripts/acceptance.sh    # the gate; MISSING is fatal, SKIP is counted separately
```

`status.sh` is the honest picture and it prints `n/a` — never zero — for a number it cannot
compute, because a zero meaning "no defects" and a zero meaning "nothing was measured" look
identical on a dashboard and this project has already shipped one of those. As of 2026-08-22 **one**
of the eight is failing and two are not yet measurable; each says which. The one is the acceptance
gate itself, and it is the number to read first — the other seven can all be green while it is red,
which is exactly the state this project was in when a broken retrieval path survived seven work
packages.

**Reference:**

- `docs/architecture.md` — what exists today, honestly, including the security posture
- `docs/acceptance.md` — the parts table. Note Part E is withdrawn (there is no wiki deletion)
- `docs/belief-engine.md` — the Claim Spine design. It RUNS now (`WS-RS8`): claims carry verbatim
  quotes and document-relative spans, and are embedded at write time (`WS-RS10`)
- `docs/wiki-schema.md` — the wiki's governance: schema, statuses, thresholds, lint, links
- `docs/operations.md` — stack, migrations, gates, pointing Aleph at any OpenAI-compatible endpoint
- `docs/html/` — `plan.html` and `backlog.html`, the same content as readable standalone pages
- `docs/update/` — pre-refactor audit reports (2026-07-26, against `bcc478a`). History only;
  **superseded** wherever they disagree with the documents above.
