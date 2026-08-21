# Acceptance

The refactor, decomposed into parts, each with a check that **can fail**.

A part is done when its check passes *and* the check has been observed failing —
either historically, or by deliberately breaking the thing. A check nobody has
seen fail is an assumption wearing a green light, which is the exact failure
this project already shipped once: a 31/32 scorecard over a central hypothesis
that was never true.

Run everything: `./scripts/acceptance.sh`

**Do not read a count off this page.** Run `./scripts/acceptance.sh`, which is
the only thing that knows, and `./scripts/status.sh` for the eight numbers in
`docs/plan.md` Part 1. This paragraph used to say "38 pass · 0 fail · 0 red · 3
blocked" while eleven rows named test files that had been deleted — with the
services down those reported SKIP, and the total read as green.

The gate now reports a fourth status, **MISSING**, for a check whose subject is
gone, and MISSING always fails the run. That is the difference between "this
machine cannot run it" and "there is nothing to run".

Four Part D rows are 🟨 rather than ✅ — see the note there. Part E is
withdrawn.

| symbol | meaning |
|---|---|
| ✅ | done, check passes, check has been seen failing |
| 🟥 | check exists and is **deliberately red** — it names the defect and is the acceptance test for the fix |
| ⬜ | not started, or blocked with a stated reason |
| 🚧 | in progress |
| 🟨 | the check passes and proves **less than the row claims**, with the gap stated. Not a failure and not done — the thing this table exists to make impossible to mistake for ✅ |

---

## A — Kernel

The composability substrate. Everything else mounts on it.

| # | part | check | status |
|---|---|---|---|
| A1 | Kernel core: revertible effects, declared capabilities, computed support set | `pytest packages/aleph-kernel/tests` — 142 passed, 1 skipped, incl. partial rollback, at-most-once unwind, undeclared-access refusal, transitive blast radius, cycle reporting | ✅ |
| A2 | API boots on the kernel | `acceptance.sh::kernel_boots_api` — boots all core capabilities against live Postgres+Redis, asserts every probe passes and shutdown leaves nothing active | ✅ |
| A3 | Workers boot on the kernel | same capability set, arq worker entrypoint; asserts no duplicate wiring between API and worker | ✅ |
| A4 | Generation loader — a plugin's behaviour is replaceable with no restart | `packages/aleph-kernel/tests/test_replace.py` — load g1, call it, replace with g2, call it, assert new behaviour AND that a reference captured under g1 still sees g1's code (Theorem 63). In-memory: nothing in the running app calls `replace` | ✅ |
| A5 | Boot manifest — the protected set is declared in one signed file | `acceptance.sh::protected_set_matches_manifest`; asserts the active core set equals the manifest's support set, and that `deactivate` has exactly one call site | ✅ |
| A6 | Agent-facing plugin API | `packages/aleph-kernel/tests/test_agent_api.py` — register, activate, deactivate, and the guard that a protected capability has no addressable id. In-memory: `AgentPluginAPI` has one non-test importer in the tree and it is `scripts/_acceptance/kernel_boot.py` | ✅ |

**A-complete when:** an agent can add and remove a capability at runtime, cannot
remove a protected one, and a faulty plugin cannot take the process down.

---

## B — Retrieval

The central defect. The wiki bet was never placed because none of this worked.

| # | part | check | status |
|---|---|---|---|
| B1 | A page is retrievable by words in its body | `tests/e2e/test_retrieval_finds_body_text.py::test_body_phrase_retrieves_its_page` | ✅ |
| B2 | A natural-language question retrieves its page | `tests/e2e/test_retrieval_finds_body_text.py::test_natural_language_question_retrieves_its_page` | ✅ |
| B3 | Corpus-wide hybrid search exists (`search_corpus`) | `tests/e2e/test_search_corpus.py` — same query returns hits across ≥2 sources, scoped to one project, diversity-capped; asserts the single-source predicate is gone | ✅ |
| B4 | RRF fusion at k=60 over independent rankers | `packages/aleph-core/tests/test_rrf.py` — a doc ranked 1st lexically and 8th densely outranks one ranked 3rd/3rd, the property of `1/(60+r)` | ✅ |
| B5 | Retrieval eval harness actually invokes Aleph | `python -m aleph_evals.retrieval_eval` calls the real retrieval path and prints a number; the eval it replaced read `expected` and `actual` from the same fixture line | ✅ |
| B6 | Retrieval eval dataset, ≥40 hand-labelled (question → must-retrieve source) pairs | `packages/aleph-evals/datasets/retrieval/questions.jsonl` exists, count ≥40, and recall@8 is reported as a number | ✅ |
| B10 | The dense leg is real, and its contribution is measured | eval seeds *and* queries with the bound embedder; `mode:` reports `hybrid` vs `lexical` with the reason, never silently | ✅ |
| B7 | Stale links are not expanded | `tests/e2e/test_retrieval_finds_body_text.py::test_expansion_ignores_links_from_superseded_revisions` | ✅ |
| B8 | An empty search reports honestly instead of confabulating | a probe over `aleph_assistant.retrieval.router` asserting `_MISS_REASON` names the cause and that the `list_pages` fallback is gone | ✅ |
| B9 | Corpus search is *wired*, not merely built | a probe over `aleph_assistant.retrieval.router` asserting it calls `search_corpus` and feeds the hits to the composer | ✅ |
| B11 | An unreachable embedder degrades to keyword search, out loud | `tests/integration/test_chunk_embed_degrades.py` and `packages/aleph-assistant/tests/test_router_degradation.py` — chunks are written before they are embedded, and the reply says which half of search ran | ✅ |

**B-complete when:** B1 and B2 are green, and B6 reports a recall number that
the belief engine has to beat.

**The number, measured on the 45-pair set (12-doc corpus):**

| | lexical only | hybrid (bge-m3) |
|---|---|---|
| recall@1 | 0.60 | **0.91** |
| recall@3 | 0.93 | **0.98** |
| recall@8 | 0.98 | **1.00** |
| @1 paraphrase | 0.55 | **0.87** |
| @1 verbatim | 0.67 | **1.00** |

The dense leg earns its place: it is worth +0.31 recall@1, and almost all of
that comes from paraphrase — questions whose wording does not overlap the
source, which is precisely the case lexical search cannot serve. Reproduce with
`python -m aleph_evals.retrieval_eval`; unset `LITELLM_BASE_URL` for the lexical
column.

**This is the bar Part E has to beat.** A belief engine that retrieves worse
than 0.91@1 is not an improvement on the thing it replaces.

---

## C — Belief engine (the Claim Spine)

Replaces the wiki as the knowledge substrate. See `belief-engine.md`.

| # | part | check | status |
|---|---|---|---|
| C0 | Patch contract + trust lattice | `packages/aleph-belief/tests` | ✅ |
| C1 | Claims survive a page rewrite | `tests/e2e/test_belief_spine.py::test_reasserting_a_claim_keeps_its_identity`, `::test_citations_accumulate_across_reassertions`, `::test_re_deriving_the_same_span_unions_rather_than_duplicates`, `::test_supersession_keeps_the_old_belief_walkable` | ✅ |
| C2 | Evidence is anchored and verbatim-verified | `packages/aleph-core/tests/test_grounding.py` — a quote absent from the chunk is rejected; one present with different whitespace or accents is accepted via the normalised offset map | ✅ |
| C3 | Confidence is derived, never asserted | `tests/e2e/test_belief_spine.py::test_confidence_rises_with_supporting_evidence`, `::test_contradicting_evidence_moves_a_claim_to_contested`, `::test_support_counts_are_recomputed_not_asserted` | ✅ |
| C4 | Retraction propagates, with a declined branch | `tests/e2e/test_retraction_walk.py` | ✅ |
| C5 | Deterministic reconciliation replaces the LLM identity judge | `packages/aleph-belief/tests/test_reconcile.py` and `tests/e2e/test_belief_spine.py::test_duplicate_beliefs_are_proposed_for_merge_without_a_model` — a scored candidate with a named reject reason and **zero LLM calls** | ✅ |
| C6 | A human can write a claim, and agents cannot overwrite it | `tests/e2e/test_belief_spine.py::test_an_agent_cannot_overwrite_a_user_claim`, `::test_a_user_may_revise_their_own_claim` | ✅ |
| C7 | Every written citation carries a source anchor | `tests/e2e/test_belief_spine.py::test_every_written_citation_carries_a_source_id`, `::test_a_fabricated_quote_is_refused` — `source_id`, `verbatim` and the char span are all set. Replaces `source_page_id`, which no writer ever populated | ✅ |
| C8 | The belief graph is rebuildable from the RKS | `tests/e2e/test_belief_spine.py::test_the_belief_graph_rebuilds_from_its_sources`, `::test_rebuilding_twice_is_idempotent`, `::test_a_rebuild_does_not_destroy_human_corrections` | ✅ |

> **⚠ These rows measure the machinery, not its use.** `BeliefService` has never
> run in production: the live database holds 796 claims with **zero** verbatim
> quotes, **zero** `chunk_ids` and **zero** edges of any kind, and the service has
> no caller outside its own module. Every row above exercises the service
> directly from a test. That is worth having — it is what makes `WS-RS8` a wiring
> job rather than a rewrite — but a ✅ here does not mean the belief layer is
> running. `docs/plan.md` `WS-RS8` gives it its first caller.

**C-complete when:** retraction visibly flags dependent claims — something the
system has never once done — and a human edit survives the next compile.

---

## D — Skills and self-improvement

The product thesis: an agent that authors plugins for itself.

> **⚠ These six rows certify the product thesis against a module the product
> does not load.**
>
> Every one resolves against `packages/aleph-kernel/tests/` — 142 in-memory
> tests, none of which touches the running application. Verified 2026-08-21: the
> agent-facing kernel API has exactly **one** non-test importer in the whole
> tree (`scripts/_acceptance/kernel_boot.py:77`). No HTTP route, no agent tool
> and no graph node constructs or calls it.
>
> The kernel itself is real and works. This is a gap between the kernel and the
> product, not a broken kernel — but a ✅ that reads as "the agent can do this"
> when it means "a unit test can do this" is exactly the failure mode this
> document exists to prevent. Four rows are demoted to 🟨 below, each with the
> specific overstatement named.
>
> `docs/plan.md` `WS-A1a`, `WS-A1b` and `WS-A2` close the gap.

| # | part | check | status |
|---|---|---|---|
| D1 | A skill is prompt + code, loaded under an AST gate | unit: a skill whose module top level has a side effect (import-time network, exec, open) is **refused**; a definition-only module loads. `test_ast_gate.py`, 24 tests | ✅ |
| D2 | A skill registers as a kernel capability | `test_skills.py::test_a_skill_activates_as_a_capability` asserts the kernel **provides the skill's key** after activation. It does **not** touch a tool set: the row used to claim "provides a tool, and shows up in the agent's tool set; deactivating it removes the tool", and no test anywhere asserts any of that. The agent's tools come from deepagents' `SkillsMiddleware` over a different backend entirely | 🟨 |
| D3 | An agent installs a skill it authored, in memory | `test_skills.py::test_an_agent_installs_a_skill_it_authored` drives `AgentPluginAPI` against an in-process `Kernel`. The row used to claim "end to end"; the running assistant cannot reach `AgentPluginAPI` at all — one non-test importer, and it is an acceptance script. Nothing is sandbox-tested and no skill survives the process | 🟨 |
| D4 | Spawn ledger — depth, fan-out and budget brakes | `test_spawn_ledger.py`, 13 **pure in-memory** tests. The row used to claim an integration test asserting "parent/child/budget rows"; there is no database anywhere in that file, and `SpawnLedger` has zero callers outside it, so no subagent Aleph actually spawns is braked by it | 🟨 |
| D5 | Probation and rollback | `test_probation.py`: a capability whose probe fails on the 2nd call is retired automatically and the system is unchanged afterwards. In-memory, and honest about it — degradation is defined against the capability's own probe, not against production behaviour | ✅ |
| D6 | The claude-science research skills are ported (Apache-2.0, with NOTICE) | `test_ported_skills.py`: `skills/literature-review/` loads through the gate and its helpers are callable. **The running agent cannot see it** — the assistant's skills root is `apps/api/src/aleph_api/skills/`, which holds `ach`, `report-authoring`, `research` and `wiki-style`, and not this one. The port is real; the wiring is absent | 🟨 |

**D-complete when:** the assistant — not a unit test — can add a capability, the
capability survives a restart, and the system survives it being bad. Today none
of those three is true, which is why four of these rows are 🟨.

---

## E — Deletion

The refactor is not done until the replaced thing is gone.

| # | part | check | status |
|---|---|---|---|
| E1 | Wiki subsystems removed | `acceptance.sh::wiki_is_gone` — `curator_service`, `index_service`, `alias_service`, `handedit_service`, `citation_verification`, `feedback_service` absent; no importers | ⬜ |
| E2 | `Capability.PAGE_SELECTION` and the page-selector hop removed | grep + a check that no ModelProfile binding references it | ⬜ |
| E3 | Dead tables dropped in a migration | `alembic check` clean after dropping `wiki_index`, `wiki_links`, `wiki_sections`, `hand_edit_marks`, `aliases` | ⬜ |
| E4 | Package count does not grow | `acceptance.sh` asserts ≤21 workspace packages. Currently 21 — `aleph-kernel`, `aleph-belief` and `aleph-runtime` were added; the reduction comes with E1 | ✅ |
| E5 | `aleph-belief/patch.py` is wired or deleted | asserts ≥1 importer outside its own tests. Wired by `BeliefService.propose_merges` | ✅ |

**Why E is blocked, and what unblocks it.**

Deleting the wiki services is not a deletion job. `curator_service`,
`alias_service`, `feedback_service` and `citation_verification` are woven through
the ingest→compile pipeline (`agent/workflow.py`, `synthesis_workflow.py`), so
removing them means replacing that pipeline with belief extraction — turning an
ingested source into claim drafts.

`BeliefService.rebuild` already accepts that extractor as an injected callable
and is tested for determinism, idempotence and not destroying human corrections
(C8). What does not exist is the extractor itself, and its acceptance test is
*"does it produce good claims"* — which requires a live LLM gateway to answer.

**⚠ Part E is WITHDRAWN as written.** It asked when the wiki could be deleted.
`decisions.md` D1 (2026-08-21) reverses that decision: the wiki and the RAG over
the raw collection are two knowledge plugins and both stay. There is no deletion
to unblock, so there is no condition to state.

What survives from E, restated honestly:

- The **extractor** — turning an ingested source into claim drafts — still does
  not exist, and it is why the Claim Spine has never run (786 claims, 0 edges,
  0 verbatim quotes, no callers). It is now scoped as the wiki's evidence layer
  rather than as its replacement. `plan.md` `WS-RS8`.
- The comparison *claim-level vs chunk-level retrieval on the same eval* is
  still worth measuring, because it says whether claims add retrieval value on
  top of chunks. It is a measurement, not a gate on a deletion. `plan.md`
  `WS-RS10`.
- The **local gateway** this section used to name no longer exists — that
  directory was deleted. Aleph connects to whatever OpenAI-compatible endpoint
  is configured, and ships none.

**E-complete when:** nothing imports the wiki, and the line count is down, not up.

---

## F — Security

| # | part | check | status |
|---|---|---|---|
| F1 | The agent endpoint is authenticated and derives project scope server-side | `packages/aleph-security/tests/test_request_context.py`, plus `apps/api/tests/unit/test_copilotkit_auth.py` and `test_agent_thread_scope.py` — a forged project id in client state is rejected and the endpoint is off the middleware skip list | ✅ |
| F2 | Untrusted ingested text is defanged at the boundary | `packages/aleph-rks/tests/test_ingest_defang.py` and `packages/aleph-core/tests/test_grounding.py` — homoglyph control tokens, U+2028/2029, bidi marks and NUL are handled at ingest | ✅ |
| F3 | Agent tokens are scoped and expire | `packages/aleph-security/tests` — an expired or wrong-project token is rejected | ✅ |

**F1 blocks any deployment.** Every HTTP route is correctly gated; the agent
reaches around all of them.

---

## H — Model gateway

Rule #7 resolves capability → model. Nothing checked that the resolved model is
one the configured gateway actually serves.

| # | part | check | status |
|---|---|---|---|
| H1 | Every model bound in any `ModelProfile` is served, and the embedder emits the column's dimension | `acceptance.sh::H1` — reads every binding from `model_profiles`, diffs against `/v1/models`, then *calls* the embedder and compares its width to `EMBEDDING_DIM` | ✅ |

Both assertions have been observed failing: binding a nonexistent model reports
it with the capabilities that referenced it, and an embedder whose width differs
from the column is rejected with "every write fails". The second is the one with
teeth — it calls the model rather than reading a list.

**H-complete when:** a rebind to a model the gateway does not carry fails at
boot-adjacent check time rather than at the first ingest.

---

## G — Verification infrastructure

The instruments. Without these, nothing above can be trusted.

| # | part | check | status |
|---|---|---|---|
| G1 | Audit assertions observe behaviour, not shape | each `audit/checks/*.sh` must fail when its subject is broken; `wiki-first-retrieval.sh` rewritten as a known-answer probe | ✅ |
| G2 | CI runs at least one behavioural gate | `ci.yml` contains a job that exercises a running system | ✅ |
| G3 | The comprehensive check | `./scripts/acceptance.sh` runs every part's check and prints a per-part table with an overall verdict | ✅ |

---

## The comprehensive check

`./scripts/acceptance.sh` is the single command. It must:

1. Run every part's check and report per-part pass/fail/skip — **never** collapse
   a skip into a pass.
2. Print the count of deliberately-red checks separately, so "2 failing" reads as
   "2 known defects with acceptance tests" rather than "the build is broken".
3. Fail if any check that was previously green goes red.
4. **Fail if any check cannot fail** — a self-check that mutates each subject and
   asserts its check notices. This is the guard against the predicted failure
   mode: a beautiful system whose probes are `assert len(result) > 0`.

Point 4 is the one that matters. Everything else is bookkeeping.
