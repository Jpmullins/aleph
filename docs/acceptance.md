# Acceptance

The refactor, decomposed into parts, each with a check that **can fail**.

A part is done when its check passes *and* the check has been observed failing —
either historically, or by deliberately breaking the thing. A check nobody has
seen fail is an assumption wearing a green light, which is the exact failure
this project already shipped once: a 31/32 scorecard over a central hypothesis
that was never true.

Run everything: `./scripts/acceptance.sh`

**Current: 38 pass · 0 fail · 0 red · 3 blocked.** Run `./scripts/acceptance.sh`
to reproduce, and `--self-check` to verify the checks can fail.

Parts **A** (kernel), **B** (retrieval), **C** (belief engine), **D** (skills and
self-improvement), **F** (security) and **H** (model gateway) are complete.

The remaining three are all Part E. They were blocked on the absence of a live
gateway; that blocker is now gone — see below for what remains.

| symbol | meaning |
|---|---|
| ✅ | done, check passes, check has been seen failing |
| 🟥 | check exists and is **deliberately red** — it names the defect and is the acceptance test for the fix |
| ⬜ | not started, or blocked with a stated reason |
| 🚧 | in progress |

---

## A — Kernel

The composability substrate. Everything else mounts on it.

| # | part | check | status |
|---|---|---|---|
| A1 | Kernel core: revertible effects, declared capabilities, computed support set | `pytest packages/aleph-kernel` — 49 tests incl. partial rollback, at-most-once unwind, undeclared-access refusal, transitive blast radius, cycle reporting | ✅ |
| A2 | API boots on the kernel | `acceptance.sh::kernel_boots_api` — boots all core capabilities against live Postgres+Redis, asserts every probe passes and shutdown leaves nothing active | ✅ |
| A3 | Workers boot on the kernel | same capability set, arq worker entrypoint; asserts no duplicate wiring between API and worker | ✅ |
| A4 | Generation loader — a plugin's behaviour is replaceable with no restart | integration test: load g1, call it, replace with g2, call it, assert new behaviour AND that a reference captured under g1 still sees g1's code (Theorem 63) | ✅ |
| A5 | Boot manifest — the protected set is declared in one signed file | `acceptance.sh::protected_set_matches_manifest`; asserts the active core set equals the manifest's support set, and that `deactivate` has exactly one call site | ✅ |
| A6 | Agent-facing plugin API | integration test: agent registers a plugin, activates it, deactivates it; asserts it cannot name a protected capability | ✅ |

**A-complete when:** an agent can add and remove a capability at runtime, cannot
remove a protected one, and a faulty plugin cannot take the process down.

---

## B — Retrieval

The central defect. The wiki bet was never placed because none of this worked.

| # | part | check | status |
|---|---|---|---|
| B1 | A page is retrievable by words in its body | `test_body_phrase_retrieves_its_page` | ✅ |
| B2 | A natural-language question retrieves its page | `test_natural_language_question_retrieves_its_page` | ✅ |
| B3 | Corpus-wide hybrid search exists (`search_corpus`) | unit: same query returns hits across ≥2 sources; asserts the single-source predicate is gone | ✅ |
| B4 | RRF fusion at k=60 over independent rankers | unit: a doc ranked 1st lexically and 8th densely outranks one ranked 3rd/3rd — property of `1/(60+r)` | ✅ |
| B5 | Retrieval eval harness actually invokes Aleph | `python -m aleph_evals --dataset retrieval_recall` must call the real retrieval path; check asserts the scorer's `actual` did NOT come from the fixture | ✅ |
| B6 | Retrieval eval dataset, ≥40 hand-labelled (question → must-retrieve source) pairs | dataset file exists, count ≥40, and recall@8 is reported as a number | ✅ |
| B10 | The dense leg is real, and its contribution is measured | eval seeds *and* queries with the bound embedder; `mode:` reports `hybrid` vs `lexical` with the reason, never silently | ✅ |
| B7 | Stale links are not expanded | `test_expansion_ignores_links_from_superseded_revisions` | ✅ |
| B8 | An empty search reports honestly instead of confabulating | covered by the router short-circuit; audit probe asserts the diagnostic names the cause | ✅ |

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
| C1 | Claims survive a page rewrite | integration: commit revision 2 over revision 1, assert the claim keeps its id, its citations and its edges | ✅ |
| C2 | Evidence is anchored and verbatim-verified | unit: a quote absent from the chunk is rejected; a quote present with different whitespace/accents is accepted via the normalised offset map | ✅ |
| C3 | Confidence is derived, never asserted | integration: add a contradicting citation, assert confidence moves `well_supported → contested` with no LLM call in the trace | ✅ |
| C4 | Retraction propagates, with a declined branch | integration: retract a source; a claim resting only on it is flagged, a claim with an independent surviving citation stays believed and is annotated | ✅ |
| C5 | Deterministic reconciliation replaces the LLM identity judge | unit: near-duplicate claims produce a scored candidate with a named reject reason and **zero LLM calls**; `curator_service.dedup_detect` is gone | ✅ |
| C6 | A human can write a claim, and agents cannot overwrite it | integration: `POST /claims` with `origin=user`, then run the extractor; assert the user claim is untouched | ✅ |
| C7 | Every written citation carries a source anchor | `test_every_written_citation_carries_a_source_id` — `source_id`, `verbatim` and the char span are all set. Replaces `source_page_id`, which no writer ever populated | ✅ |
| C8 | The belief graph is rebuildable from the RKS | integration: rebuild into a scratch project, assert claim set equivalence | ✅ |

**C-complete when:** retraction visibly flags dependent claims — something the
system has never once done — and a human edit survives the next compile.

---

## D — Skills and self-improvement

The product thesis: an agent that authors plugins for itself.

| # | part | check | status |
|---|---|---|---|
| D1 | A skill is prompt + code, loaded under an AST gate | unit: a skill whose module top level has a side effect (import-time network, exec, open) is **refused**; a definition-only module loads | ✅ |
| D2 | Skills are kernel plugins | integration: a skill registers, provides a tool, and shows up in the agent's tool set; deactivating it removes the tool | ✅ |
| D3 | An agent authors a skill end to end | integration: agent writes a skill, it is gated, sandbox-tested, activated, and its probe runs | ✅ |
| D4 | Spawn ledger — the agent family tree is recorded | integration: spawn a subagent; assert parent/child/budget rows and that depth is capped | ✅ |
| D5 | Probation and rollback | integration: activate an authored plugin whose probe fails on the 2nd call; assert automatic revert and that the system is unchanged afterwards | ✅ |
| D6 | The claude-science research skills are ported (Apache-2.0, with NOTICE) | `literature-review` skill loads and its `kernel.py` helpers are callable | ✅ |

**D-complete when:** D3 and D5 both pass — an agent can add a capability and the
system survives it being bad.

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
- The **local gateway** referenced here (`deploy/local-gateway/`) no longer
  exists — that directory was deleted. Aleph connects to whatever
  OpenAI-compatible endpoint is configured.

**E-complete when:** nothing imports the wiki, and the line count is down, not up.

---

## F — Security

| # | part | check | status |
|---|---|---|---|
| F1 | The agent endpoint is authenticated and derives project scope server-side | integration: request with a forged project id in client state is rejected; endpoint is off the middleware skip list | ✅ |
| F2 | Untrusted ingested text is defanged at the boundary | unit: homoglyph control tokens, U+2028/2029 and bidi marks are stripped at ingest | ✅ |
| F3 | Agent tokens are scoped and expire | unit: an expired or wrong-project token is rejected | ✅ |

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
