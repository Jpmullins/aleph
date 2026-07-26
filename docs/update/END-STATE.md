# END STATE — production-ready conditions and the tests that prove them

**Created:** 2026-07-26. **Basis:** the four `docs/update/` audit documents, a 13-agent adversarial
verification of them against code, a 6-agent framework-currency study, and three independently
confirmed live bugs none of the audits found.

---

## The organizing principle

Every load-bearing defect in this codebase shares one shape:

> **The code takes an empty path and reports success.**

- `Citation.source_page_id` is NULL → `INNER JOIN` returns `[]` → the trust layer computes over
  nothing and renders confident numbers.
- A LangGraph node writes a state key absent from the `TypedDict` → the write is dropped →
  `state.get(k) or []` → the feature silently does nothing. **Three confirmed instances.**
- `zip(chunks, embeddings, strict=False)` over an unvalidated embed response → silent
  misalignment → `status="indexed"`.
- `except Exception` in `_on_skip`, `_enqueue_curate`, the cost callback, and the SSE frame
  parser → failures become absences.

**Therefore every exit test below must assert a NON-EMPTY, CORRECT result produced by the
production path.** A test that asserts a function returns, a row exists, or a status is 200 does
not count. This rule is why the original defects survived a work package: every WP-6 exit
criterion was satisfiable with hand-seeded fixture data.

### The fixture rule (binding)

> No exit criterion may be proved by a test that constructs the value under test.
> The value must be produced by the same code path production uses.

Where a real network/LLM call is impossible in CI, the *boundary* may be faked (an httpx
transport, a canned gateway response) but **never the intermediate domain object**. Faking a
`SourcePage` row, a `CitationDraft.source_page_id`, or a graph's state dict is disqualifying.

---

## E1 — No silent state-drop  ✅ *(the cheapest, highest-value block)*

**Condition.** No LangGraph node in the repo writes a state key that its `TypedDict` does not
declare, and this is enforced by a committed CI sweep.

Three confirmed instances today:

| File | Key | Consequence |
|---|---|---|
| `packages/aleph-wiki/.../synthesis_workflow.py:182,207` | `resolved_wikilinks` | every synthesis-authored page commits with **zero** wikilinks — guts rule #1's 1-hop expansion |
| `packages/aleph-artifacts/.../builder/workflow.py:168,188` | `csl_items` | every exported artifact has an **empty bibliography** |
| `packages/aleph-reviewer/.../editorial/workflow.py:283-287` | `n_c…n_f` | editorial `finding_count` always **0** |

**Exit tests.**

- `E1.1` `scripts/check-graph-state-keys.sh` — parses every `*State(TypedDict)` and every
  `return {...}` in each graph module; fails on any written-but-undeclared key. Committed,
  wired into `.github/workflows/ci.yml`, and **demonstrated to fail** on a deliberately
  reintroduced bug before passing on the fixed tree.
- `E1.2` `test_synthesis_commits_wikilinks` — drive `SynthesisWorkflow` over a report whose
  `body_md` contains `[[Topic A]]` and `[[Topic B]]`; assert `>= 2` `WikiLink` rows exist for the
  committed revision. Must fail on the pre-fix tree.
- `E1.3` `test_builder_emits_bibliography` — drive the builder graph with ≥1 CSL item; assert
  `bibliography_markdown != ""` and contains the author/year. Must fail on the pre-fix tree.
- `E1.4` `test_editorial_finding_count_matches_rows` — assert the persisted `ReviewRun.finding_count`
  equals the number of `ReviewFinding` rows actually written. Must fail on the pre-fix tree.
- `E1.5` Zero `# type: ignore` remaining on any `state.get(` in a graph module (grep).

---

## E2 — The provenance chain carries real, non-tautological grounding

**Condition.** A claim produced by the production ingest path resolves through
`Citation → SourcePage → Source` to a source page **other than the page the claim lives on**, and
every trust mechanism that walks that chain returns non-empty on that data.

**Prerequisite — disambiguate the column.** `Citation.source_page_id` is **dual-typed**: five
readers treat it as a `source_pages` PK (`retraction.py:78`, `mechanical/workflow.py:169,411`,
`curator_service.py:379`, `wiki_refresh.py:167`); `surfaces.py:503,513` treats it as a
`wiki_pages` id. Both are unconstrained nullable UUIDs, so neither errors. Pick one, rewrite the
loser, and record the decision.

**Exit tests.**

- `E2.1` `test_ingest_writes_resolvable_citation` — run the real ingest path end-to-end; assert
  `SELECT count(*) FROM citations WHERE source_page_id IS NOT NULL > 0` **and** that the resolved
  target is not the claim's own page. *(The roadmap's proposed criterion omits the second
  clause and therefore passes on a self-citation.)*
- `E2.2` `test_retraction_blast_radius_nonempty` — ingest a real source, retract it, assert
  `>= 1` dependent claim flagged `retracted` **without any fixture constructing a `SourcePage`**.
- `E2.3` `test_freshness_discriminates_groundedness` — assert a grounded page scores strictly
  higher than a claimless one. Today both score **50** (verified by execution: the vacuous
  `_citation_health` branch and the `if not citations: return 0.0` verification branch cancel).
- `E2.4` `test_citation_popover_resolves` — `WikiBodyMarkdown.tsx:27` captures `"c1"`;
  `WikiPageCard.tsx:154` keys on the stored `"[c1]"`. Assert a rendered citation chip resolves to
  a titled source, not "No citation resolved".
- `E2.5` All three `Citation` write sites covered, including `curator_service.py:773`
  (`apply_merge`'s claim fold, which bypasses `WikiService`), plus a backfill for rows
  `_carry_claims` has already propagated as NULL.

**Explicitly out of scope here:** making the research loop emit claims. `_node_compose` builds its
prompt from titles and URLs only (`grep normalized_markdown|DocumentChunk packages/aleph-research/src`
→ zero hits). Emitting claims from that *after* wiring real `source_page_id`s would manufacture
fabricated provenance that passes the revived trust layer — strictly worse than `claims=[]`.

---

## E3 — The seams are closed

**Condition.** No unauthenticated write path into the agent, and no cross-project token reuse.

**Exit tests.**

- `E3.1` `test_copilotkit_endpoint_requires_auth` — `POST /copilotkit/agent/assistant` with no
  credential returns 401/403. Today `auth.py:53-56` exempts the whole `/copilotkit` prefix in
  **both** auth modes and the handler does no verification of its own.
- `E3.2` `test_agent_thread_id_membership_enforced` — a valid principal with a `thread_id`
  naming a project they are not a member of is refused. Today project scope comes from a
  client-supplied string with zero membership check.
- `E3.3` `test_agent_token_project_scope_enforced` — a token minted for project A is refused on
  project B's routes. Today `verify_agent_token` returns a signed `project_id` and the middleware
  discards it.
- `E3.4` `test_ingest_row_committed_before_enqueue` — the worker never observes a missing row.
  Five routes currently enqueue before commit; `normalize_job` raises a plain `RuntimeError`,
  which arq treats as terminal.

---

## E4 — The frameworks we already pay for are actually used

**Condition.** No capability that is free at current pins remains unexploited without a written reason.

**Exit tests.**

- `E4.1` `cached_tokens > 0` appears in `CostLedgerEvent` on a repeated agent turn, **and**
  `pricing.py` bills the 1.25× cache-write premium (it currently models only the 90% read
  discount, so enabling caching without this systematically under-reports cost).
- `E4.2` Agent conversation state survives an API restart (`AsyncPostgresSaver` replacing the
  in-memory checkpointer; the dependency is already declared and the connection pool already open).
- `E4.3` One generated canonical A2UI catalog; `catalog.py` (~579 lines) and the inline
  `ALEPH_A2UI_CATALOG` in `copilot-runtime/src/server.ts` (~250 lines) deleted; a regeneration
  diff check in CI. Proof: the live drift is gone — `server.ts` currently tells the agent
  `ClaimCard.confidence ∈ {…initial}` while `catalog.py` says `{…retracted}`, so the agent
  **cannot emit the WP-6 `retracted` value**.
- `E4.4` `apps/copilot-runtime` has a lockfile, exact pins, and a `tsc --noEmit` CI step.
  `server.ts:301` passes `defaultCatalogId`, which does not exist in the middleware version its
  manifest implies — it works only because an unpinned `npm install` floats forward.
- `E4.5` Five dead JS deps removed; `deepagents` upper-bounded.

---

## E5 — The workspace supports comparative reading

**Condition.** Two provenance-bearing things can be read side by side, and the analyst can see
what the agent sees.

**Exit tests.**

- `E5.1` One semantic token set; the `!important` dark shim (`tokens.css:119-182`) and the
  CopilotKit inline-RGB hack (`styles.css:118-125`) are deleted. Prerequisite: define a Tailwind
  theme — `tailwind.config.ts` currently extends only `fontFamily`, which is *why* the shim exists.
- `E5.2` `:focus-visible` and `prefers-reduced-motion` present; theme applied pre-paint.
- `E5.3` Rail replaces the 5-tab bar (`flex-1` per tab is a hard ceiling at ~6 tabs).
- `E5.4` A context bar renders the same payload already sent to `useAgentContext`.
- `E5.5` A staged pipeline strip shows corpus-level progress — the gap named in `next-steps.md` §4.
- `E5.6` Playwright e2e wired into CI. `apps/web` has **zero** frontend tests today and
  `tests/playwright` is not in `ci.yml`.

---

## E6 — Gates

`ruff check` · `ruff format --check` · `pyright` (0 errors, warnings ≤ 949) ·
`pytest -m "not integration"` · `pytest -m integration` · `alembic check` ·
`pnpm -C apps/web typecheck|lint|build` · all committed sweeps ·
**plus the new `check-graph-state-keys.sh`.**

---

## Sequencing

Ordered by (risk reduction ÷ effort), respecting dependencies:

**W0** E1 — three declarations + the sweep + the three behavioural tests.
**W1** E4.1/E4.2/E4.5 — free capability at current pins.
**W2** E3 — the security seams.
**W3** E2 — the provenance repair (a week, not "two edits": dual-typing, a third write site,
no `revision_id` to join on, a cross-job race, and a backfill).
**W4** E4.3/E4.4 — the catalog collapse (~850 lines deleted).
**W5** E5 — the shell.

**Baseline recorded 2026-07-26** (post budget-removal, pre-W0): ruff clean · pyright 0 errors,
949 warnings · `pytest -m "not integration"` **359 passed**.

---

## Progress

| Item | State | Evidence |
|---|---|---|
| Toolchain | **done** | `uv` 0.11.32 + `pnpm` 11.17.0 installed; the gate suite is runnable for the first time this session |
| Budget removal | **verified** | ruff clean · pyright 0/947 · 377 unit pass · web typecheck+lint+build pass |
| **E1** state-drop | **done** | 4 defects fixed (3 known + `version_no`, found by the sweep). `tests/unit/test_graph_state_keys.py` — 13 tests: static sweep over all graph modules, behavioural round-trip over the *real* production state classes, and two tests pinning LangGraph's drop semantics for both node writes and initial state. **Demonstrated to fail on a reintroduced regression, then pass.** |
| **E3.1** auth hole | **done** | `_SELF_AUTH_PREFIXES` now `()`. `apps/api/tests/unit/test_copilotkit_auth.py` proved 3 agent paths accepted unauthenticated POSTs, then proved they 401. Guard test keeps the tuple empty. |
| **E4.4** runtime pins | **done** | lockfile committed · `@ag-ui/client` 0.0.53→0.0.57 (single copy; was two) · runtime pinned 1.63.2 exactly · `npm install`→`npm ci` in the Dockerfile · `tsc --noEmit` added and **passing for the first time** · CI step wired |
| **E4.5** dead deps | **done** | 5 removed (`@copilotkit/react-ui`, `@copilotkit/a2ui-renderer`, `@tanstack/react-router`, `@xyflow/react`, `maplibre-gl`); `deepagents` bounded `<0.7`; web build green |
| **E3.3** token scope | **done** | `Principal.project_id` carries the signed claim; `_assert_credential_scope` refuses another project *before* the membership query, on both `project_scope_dep` and `assert_stream_access`. 4 tests, using a session stub that raises if touched — reaching the DB at all would prove the cheap refusal did not happen. |
| **E3.2** thread scope | **done** | New `middleware/agent_scope.py`: exhaustive JSON-body walk for every channel a caller can name a project through, all-or-nothing membership check, wired into `AuthMiddleware`. 25 tests including a **reachability** test through the real app and an **anti-vacuity** pair (the first negative test passed for the wrong reason — FastAPI's router also 404s — so refusals are now distinguished by problem body, not status). |
| `alembic check` substitute | **done** | Docker is unavailable here, so `apps/api/tests/unit/test_model_migration_consistency.py` (7 tests) covers what the budget change could break: ORM/migration agreement on dropped tables + columns, the `cost_to_budget` trigger drop, and a single-headed non-forked revision chain. |
| pyright warnings | **949 → 844** | `apps/api/src/aleph_api/py.typed` added — the marker WP-5 gave every `packages/aleph-*` but never gave `aleph_api`; removed ~103 `reportMissingTypeStubs`. |
| **E4.2** durable agent state | **done** | `build_agent_checkpointer(pool)` → `AsyncPostgresSaver` on the store's existing pool, `setup()` in lifespan, threaded through `setup_copilotkit` → `build_assistant_deep_agent`. 8 tests pin the **wiring** (AST-asserted at every hop), because it is the wiring that regresses, not the signature. Also required adding langgraph's `checkpoint*` tables to alembic's `_IGNORED_TABLES` — `setup()` creates them at runtime and autogenerate wanted to drop them, which broke `alembic check` until fixed. |
| **E2** provenance chain | **repaired + proven** | `_link_citations_to_source_page` now keeps the promise the `CitationDraft` comment made and never kept. Dual-typing resolved in favour of the **SourcePage PK** (5 readers to 1); `surfaces.py:_resolve_citations` rewritten to match and now emits the *wiki page* id, the only id with client meaning. `tests/e2e/test_citation_provenance.py` drives the real `_node_commit_revision` and constructs **nothing** on the chain — **verified to fail with the exact defect signature when the fix is removed, and pass when restored.** Also fixed a latent bug found in passing: the bridge lookup did `session.get(SourcePage, result.page_id)` — a PK lookup with a *wiki page* id, which could never hit. |
| **E4.3** catalog drift | **done (the correctness half)** | Took the enforcement, not the codegen. Fixed three live drifts: `ClaimCard.confidence` contained **no** `"cited"` in any catalog — the value both wiki writers hardcode and therefore the most common in the DB, so validating a real card would have rejected it; the agent-facing list offered `"initial"` (recognised by nothing) and omitted `"retracted"`, making the WP-6 state unemittable; and `"dismiss"` was declared dispatchable with no handler. `tests/unit/test_catalog_agreement.py` (11 tests) compares **props and actions**, which the roster sweep never did — **verified to fail on the reintroduced drift.** *Not done: the build-time catalog generator that would delete ~850 lines. The drift is now caught; the duplication remains.* |
| **E5.2** a11y globals | **done** | `:focus-visible` and `prefers-reduced-motion` added — both were absent outright. Guarded against source **and the built bundle**, since `tsc` cannot read a stylesheet and there is no frontend test runner. Pre-paint theme boot already existed. |
| sweep portability | **done** | `check-catalog-roster.sh` used `declare -A` + `mapfile` (bash 4); macOS ships 3.2, so it had never run on the primary dev platform. Rewritten portably — **all five sweeps now pass locally.** (Correcting an earlier claim of mine: only *one* sweep was affected, not three; the other two were failing because `uv` was off PATH.) |
| **E4.1** cache pricing | **done (the correctness half)** | `pricing.py` modelled the 90% cache-READ discount and **no write premium at all**, so enabling caching would have systematically *under*-reported the cost of every cache-priming call — while `cache_savings_usd` grew, i.e. the dashboard number moves the reassuring way for the wrong reason. Added `cache_write_multiplier` (1.25× for caching models, 1× for the rest) and `_cache_write_tokens`, which accepts every known gateway spelling because an unrecognised key silently bills a write as a free token. 14 tests. *Not done: enabling caching and proving `cached_tokens` lands in `CostLedgerEvent` — that needs the live Insights gateway, and a mock would prove nothing about real money.* |
| **E5.4** context bar | **done** | The single highest-value item from the benchmesh read. Aleph already sent `{active_tab, open_page_title, selection}` to the agent every turn and showed the human none of it, so "summarize this page" worked by apparent magic and a stale context was undetectable. `ContextBar.tsx` mirrors that exact payload — read-only, no fetching, so it cannot drift from what the agent was actually sent. 10 unit guards (mount, no-self-fetch, field parity with the agent payload, theme tokens only) + a Playwright spec asserting the bar **moves** with the surface rather than rendering a hardcoded value. **Mount guard verified to fail when the component is unmounted.** |
| **E4.1** cache attribution | **done** | I was initially too conservative here: the fixture rule explicitly permits faking *the boundary*, which makes this closeable. `tests/e2e/test_cache_token_attribution.py` cans the gateway response (as `test_smoke_llm.py` already does) and asserts the real client → real pricing table → real `ModelCall` + `CostLedgerEvent` + the user-visible cost rollup. **Verified to fail on the reverted premium**: `0.001000` instead of `0.001250`, a 20% under-report, caught at all three layers. |
| **E5.6** Playwright in CI | **done** | `apps/web` has no unit-test runner and `tsc` cannot see a CSS class or a mis-wired component, so the frontend was verified only by hand. Added a `playwright-shell` CI job (Postgres + Redis + API + built web, no LLM gateway) running the `@shell` subset. **Not added blind — booted the stack locally and ran it: 10/10 pass.** Doing so found three real bugs: (a) my context bar created a *second* button with the same accessible name as a surface tab — a genuine a11y defect, fixed with `aria-label`; (b) `02-workspace-shell` still asserted an `Artifacts` tab, renamed to `Library` in WP-5 and never updated, i.e. **the spec had been wrong for two work packages because the suite was not in CI**; (c) one chat test needs the copilot-runtime, now tagged `@runtime` and excluded. |
| **Gateway-driven models + pricing** | **done, proven live on both gateways** | **Correction first:** my initial finding — "the committed price table's every entry is wrong" — was measured against the *ODNI* (`gateway.ai-ops`) deployment, because `API_keys.txt` points `LITELLM_BASE_URL` there. Against the **insights** gateway the table's names were right (`claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`, the embedders all exist). What is true, and is the real defect, is narrower and worse: a committed table cannot know which gateway it is pointed at, and `cost_for` returned **`$0` for an unknown model with no trace** — so on the mismatched gateway a live research run would have produced a $0.00 ledger reading as a quiet day, with `test_unknown_model_returns_zero` pinning that behaviour *as the requirement*. Replaced with runtime discovery: `/model/info` when the key may read it (modes, windows, flags, exact rates), else `/v1/models` — **the normal case**, since a LiteLLM *virtual key* is restricted to `llm_api_routes` and the insights gateway 403s the admin route — with `aleph_models/hints.py` filling unreported fields from an operator-editable file that never overrides the gateway. Provenance is tracked separately from metadata (`rates_source`) after my own live assertion caught hint-derived rates being labelled `gateway`; `model_calls` now carries `pricing_source` (`gateway`/`static`/`unknown`), `cache_write_tokens` and the rates used, so a cost is re-derivable from its own row. Defaults come from requirements over metadata, never model names; priced models outrank unpriced ones, and an unpriced binding is reported rather than refused — refusing outright left `embedding` unbindable on the insights gateway, whose only reachable embedder has no published rate. `autoconfigure` probes before binding, which found real breakage on **both** deployments: insights denies access to `claude-opus-4-6` + both Cohere embedders; ODNI advertises two Sonnets that fail on invocation. 69 unit + 9 integration tests. **Live proof, insights gateway:** 31 models discovered via fallback, `claude-haiku-4-5` call → `cost=$0.000044`, `pricing_source=static`, re-derived from stored rates and matching. `verify-gateway.sh` rewritten the same way (it asserted 5 model ids and would have failed bootstrap here). |
| **GroundingSurface** | **done** | E2's missing user-visible half: the provenance chain was repaired but had no viewer. Renderer + both catalog rosters + impl registration + roster producer entry + pane kind. Written so the *negative* states are first-class — an ungrounded claim, a citation with no resolvable source, a source with no quotable passage each render as a stated reason rather than as blank space, because a confident empty chain is what an inspector built before the writers were fixed would have shown. 9 integration tests drive the real `_grounding_messages`; the chunk-hop test asserts `MARKDOWN[char_start:char_end] == text` so the quote shown is provably the span it claims. |
| **E1.2 / E1.4 / E2.3** behavioural | **done, each verified fail→pass** | E1 was closed *statically* (the AST sweep) but three of its named behavioural exit tests did not exist, and neither did E2.3's. Built and each proven against a deliberately reverted tree. **E1.2** drives the *compiled* `SynthesisWorkflow` — not the node, because the defect was the channel between `_node_wikilink_resolve` and `_node_commit_revision`, and calling the node directly passes on the broken tree; reverted, it reports "the committed revision has 0 wikilink rows" against a body containing `[[Topic A]]` and `[[Topic B]]`. **E1.4** drives the compiled editorial graph with a canned gateway; reverted, `ReviewRun.finding_count=0 but 5 ReviewFinding rows exist` — the run reporting a clean review of a page that just failed five checks. **E2.3** was not a missing test but a **live defect**: measured, a grounded page and a claimless one both scored **100**. `_citation_health([])` returned full marks ("vacuously healthy") and `_verification` short-circuited on a human tick *before* checking whether there were claims to verify. Both now return 0 for an empty citation set — grounded 100 > inferred 75 > claimless 50 — and the two tests that asserted the old behaviour as the requirement were corrected, the same shape as `test_unknown_model_returns_zero`. |
| **E5.1** token shim | **done** | Both `!important` hacks are gone. The 26-rule `tokens.css` shim went with the `@theme inline` block that finally gave it a `bg-surface` to migrate *to*. The CopilotKit override was subtler and worse: it matched elements by the literal text of their inline `style` attribute (`[style*="rgb(250, 250, 250)"]`) and repainted them with `!important` — a rule that stops working when upstream changes a colour by one digit, failing as an unreadable dark theme rather than an error. Verified deletable rather than assumed: those literals appear **nowhere** in the installed `@copilotkit` tree (`react-ui`, which shipped them, was removed in E4.5), and the `--cpk-color-*` remapping is the supported mechanism. Guarded by 3 tests asserting no inline-style selectors, `!important` confined to `prefers-reduced-motion`, and that the semantic names actually exist. |
| **E5.5** pipeline strip | **done** | The gap named in `next-steps.md` §4. A source's journey (fetch → normalize → chunk+embed → wiki) ran entirely in workers with no corpus-level view, so "is my library ready to ask questions of?" needed container logs, and a run stalled after normalization looked identical to one that finished. New `GET /v1/projects/{id}/pipeline` + `PipelineStrip.tsx`. Two design choices are the whole test surface: stages are **cumulative** (exclusive counting makes sources appear to *leave* `normalized` as they advance — work reads as lost), and failures are **never folded in** (a vanished `failed` source makes a broken corpus look like a smaller healthy one — the corpus-level form of the silent empty join). 8 integration tests incl. a monotonicity assertion and a guard that the client renders server-sent stages rather than a hardcoded copy that would drift silently. |
| **E6** `check-graph-state-keys.sh` | **done** | E6 named this sweep and it did not exist — the invariant lived only in a pytest module. Now a standalone committed sweep, CI-wired, listed in CLAUDE.md and `architecture.md`. It imports the analyzer from the behavioural test rather than reimplementing it, so the static check and the tests that prove it models LangGraph's real drop semantics cannot disagree. **Verified to fail on a reintroduced regression**: removing `resolved_wikilinks` from `SynthesisState` produces `✗ node(s) write undeclared state key(s): ['resolved_wikilinks']` and exit 1; restoring it returns exit 0. |

**Gate state — all green, including the two blocked all session:**

```
ruff check      : All checks passed!
ruff format     : 370 files already formatted
pyright         : 0 errors, 844 warnings      (baseline 949)
pytest unit     : 421 passed                  (baseline 359)
pytest integ    : 83 passed, 1 skipped        (baseline 80 + 1)
alembic check   : No new upgrade operations detected.
web tc/lint/bld : PASS
runtime tsc     : PASS
sweeps          : PASS
```

Local stack for the DB gates: `pgvector/pgvector:0.8.2-pg18` + `redis:8.8-alpine` as
`aleph-test-pg` / `aleph-test-redis` (the two services CI's integration job uses), under
Docker Desktop.

### New defect found while building the guard

`BuilderState` did not declare `version_no`, which the builder seeds into the
initial state after computing it correctly. LangGraph filters undeclared keys out
of the **initial state** too (pinned by test), so `_node_persist` fell back to
`1` and built the asset key `artifacts/{id}/1.{ext}` on **every** build — while
the outer `build()` wrote an incrementing `ArtifactVersion.version_no` from a
local. Net effect: each rebuild overwrote version 1's bytes and every historical
version row resolved to the newest object. **Versioned artifacts did not
version.** None of the four audit documents found this.
