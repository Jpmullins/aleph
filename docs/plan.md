# The Aleph plan

Written 2026-08-21. Built by twenty-four agents: six clusters each ground-truthed
against the code, planned, and then adversarially reviewed; five independent
horizon scans; one completeness critic. Every number below was verified against
the tree or the running stack. Where a claim could not be verified, it says so.

**All six reviewers returned `needs-work`. None approved.** That is the intended
outcome — their corrections are folded into the criteria, and the ones worth
reading are in Part 4.

---

## Part 0 — Stop. The instrument is lying.

Before any of the work below begins, one job: repair the gate. This is the completeness
critic's finding and it is the single biggest risk in the plan.

`scripts/acceptance.sh` is the artifact that answers "is Aleph finished". Today:

```
$ ./scripts/acceptance.sh --quick
pass=23  fail=0  red=0  skip=18 (not built)
INCOMPLETE — 18 part(s) not built. Nothing regressed.
$ echo $?
0
```

It runs 23 of 41 rows, **exits 0 on eighteen skips**, and is **never invoked by
CI** (`grep -c acceptance.sh .github/workflows/ci.yml` → `0`).

Worse, it certifies things that are false. **Acceptance Part D — the entire
product thesis — is six ✅ rows resolved against `packages/aleph-kernel/tests/`**,
a module with no callers outside its own tests. D6 asserts a ported skill "loads
and works"; that skill lives at repo-root `skills/literature-review/`, while the
agent's skills root is `apps/api/src/aleph_api/skills/` — four directories, none
of them that one. The running agent cannot see the skill its green check
certifies.

So the failure mode is not "a cluster runs late". It is: five clusters land
plausible work over three months, each green against its own criteria, and the
first honest end-to-end measurement happens at the end — against an instrument
that was already lying before anyone started.

**Status as of 2026-08-22 — five of the six are done.** Measured, not asserted:

| # | item | state |
|---|---|---|
| 1 | `docs/decisions.md` | **done** — 12 decisions; D5 (kernel is Python) closed; `CLAUDE.md`'s "open decision, do not assume Python" line is gone |
| 2 | acceptance status re-derived; dangling `tests/e2e/` refs | **done** — all 4 distinct `tests/e2e/` paths in `acceptance.sh` resolve; `check-dead-refs.sh` is green over 521 files |
| 3 | a self-check probe for every subject | **PARTIAL** — 33 probes over 26 sweeps, and **5 sweeps still have none**: `check-agent-catalog-covers-renderer`, `check-compose-hardening`, `check-confidence-vocabulary`, `check-lint-count`, `check-project-scope` — the last is WS-P6's own sweep. `check-web-dead-css` and `check-web-drift` gained probes on 2026-08-22, and writing the first of them found a HOLE in its subject: a class declared above the first `{` in a stylesheet was invisible to the dead-CSS sweep, because the selector text had the `@import` lines glued to it and an `if "@" in selector: continue` guard skipped the whole rule |
| 4 | `acceptance.sh` in CI with a ratcheting `--max-skip` | **done** |
| 5 | `scripts/status.sh` | **done** — the eight numbers; 1 failing, 2 not measurable as of 2026-08-22 |
| 6 | integration cadence declared | **done** — Part 6, line 2495 |

The gate itself was the thing it was built to prevent, and that is fixed: `run_shell`
read `tail`'s exit status through 24 of 33 pipes, so a failing check behind a pipe
reported PASS. The remaining Part 0 work is item 3 alone.

**First, in order:**

1. Create `docs/decisions.md`. It does not exist and is cited nine times, and
   three clusters write criteria of the form `grep -n '…' docs/decisions.md`.
   More seriously, `CLAUDE.md:59` says *"The kernel language and structure are an
   open decision — D5. Do not assume Python for the kernel."* Every one of the 57
   workstreams assumes Python. That decision is being closed by momentum. Write
   it down or reopen it honestly.
2. Re-derive `docs/acceptance.md`'s status column from checks that can fail.
   Repair the twenty dangling `tests/e2e/` references inside `acceptance.sh`
   (that directory does not exist — `git ls-files tests/` returns seven files).
3. Add a self-check probe for every subject, so `--self-check` proves each
   non-skipped check can fail.
4. Wire `./scripts/acceptance.sh` into CI against the postgres+redis services
   `python-integration` already declares, with a `--max-skip N` budget that can
   only ratchet down.
5. Write `scripts/status.sh` — the eight numbers in Part 1, printed by one
   command.
6. Declare an integration cadence: shared trunk, weekly merge, and a rule that no
   cluster carries more than one week of divergence on the eleven contested files
   (Part 6).

---

## Part 1 — The eight numbers that mean done

Success is abstract by nature, so it needs proxies that are not. Aleph is
finished when all eight hold simultaneously on the deployed instance, and each is
a single command. Six of the eight can be printed by one script; that script is
the notion of done.

| # | The number | Today |
|---|---|---|
| 1 | `./scripts/acceptance.sh` exits 0 with `fail=0` and `skip ≤ 2`, and `--self-check` proves every non-skipped check can fail | 23 pass, **18 skip**, exit 0, no CI |
| 2 | Retrieval nDCG@10 ≥ 0.80 and recall@1 ≥ 0.91 on a ≥150-question set, through the production `search_corpus` path, with `document_chunks > 0` in the same database | 45 pairs, seeded rows, **0 production chunks** |
| 3 | Citation precision ≥ 0.90 by entailment over ≥30 reports, and `select count(*) from citations where quote is null or char_start is null` = 0 | **786 of 786 fail** |
| 4 | The agent authors a skill in thread A; thread B sees it after an API restart | impossible — the skills backend is read-only |
| 5 | `select count(*) from model_calls where pricing_source='unknown' or agent_run_id is null` = 0 | **80** (67 unpriced, 13 more with null run id) |
| 6 | `select count(*) from agent_runs where status='running' and started_at < now() - interval '1 hour'` = 0 | **45** |
| 7 | p95 first-token latency on `POST /copilotkit` under a stated ceiling, measured with the full middleware stack | **no number exists** |
| 8 | Zero dead code by construction — the dead-code sweeps and the drift ratchet all exit 0, and `git ls-files apps/web/src \| wc -l` has gone **down** | 60 files, several unreachable |

Number 7 deserves emphasis. This plan puts a per-request graph factory, an LLM
reranker, a QuickJS interpreter, a rubric grader loop and two new middlewares
onto a request path that runs **in-process inside FastAPI** — and not one
workstream currently states a millisecond number. Pick the ceiling before adding
the load, or five of those decisions are assumed rather than argued.

---

## Part 1b — The knowledge layer is two plugins

Recorded here because it reframes the whole research cluster, and because the
plan was drafted before it was settled (`docs/decisions.md` D1, 2026-08-21).

The wiki is **not** being deleted. The old decision had the Claim Spine
replacing it as the retrieval surface; that replacement never ran, and the wiki
became the working knowledge layer. There are **two knowledge plugins and both
stay**:

- **The wiki** — what the project *concluded*. Synthesised pages, claims, hubs,
  a governed vocabulary. Curated and cross-linked; a thing a person reads.
- **RAG over the raw collection** — what the project *collected*. Every source
  chunked and indexed, searched directly, so an answer is grounded in the actual
  passage rather than in somebody's summary of it.

*"What do we think about X, and on what evidence?"* is the wiki. *"What did
source 47 actually say?"* is the RAG. Framing them as competitors is what
produced the removal decision, and it was a false choice.

The Claim Spine is the evidence layer **underneath** the wiki — what makes a
page's assertions traceable to an exact sentence — not its successor.

**Acceptance Part E is withdrawn.** It asked when the wiki could be deleted.
There is no deletion to unblock.

---

## Part 2 — What is actually broken right now

Verified live, not remembered. These reorder everything.

### Retrieval is dead on the deployed instance

```
document_chunks: 0        sources: 75        wiki_pages: 843
```

Seventy-five ingested sources, 843 compiled wiki pages, and **zero chunks**. The
hybrid retrieval that CLAUDE.md calls "built and measured" at recall@1 0.91 is
running against an empty table.

The cause: model profiles bind the embedder to `titan-embed-v2`; the configured
gateway serves `titan-embed-text-v2`. The name is wrong by one word. And because
chunks are written only *after* the embed call returns, one bad model name also
kills the lexical leg, which needs no model at all. 45 index runs sit in
`running` with no error recorded.

This gates four other clusters' measurements. Nothing in the original backlog
depended on it because nothing in the original backlog knew.

### The belief layer has never run

```
citations: 786    with a verbatim quote: 0    with chunk_ids: 0
claims:    786    claim_edges: 0
BeliefService callers outside its own module: 0
```

CLAUDE.md states that claim→chunk grounding "fills `Citation.chunk_ids` at commit
time, so the claim → chunk → char-span chain is populated on the real write path
instead of only in fixtures". On live data that is **0 of 786**.

### `access_scope` is a write-only column, schema-wide

70 assignment sites across `apps/*/src` and `packages/*/src`. **Zero** query
filters — `grep -rnE "access_scope ==|access_scope\.in_|filter.*access_scope"`
returns nothing. Every migration adds the column. CLAUDE.md lists it as a
review-held rule; it is in fact the largest single instance of the defect class
CLAUDE.md itself calls dominant. Enforce it or delete it — either way it is currently nobody's.

### The frontend has no test runner, and three clusters wrote criteria against one

`grep -c vitest apps/web/package.json` → `0`. No Playwright anywhere.
`pnpm-workspace.yaml` declares `tests/playwright` as a member of a directory that
does not exist. One workstream builds both harnesses; three clusters depend on it
and none of them said so.

### And the UI has no error containment or keyboard access at all

Across 60 files in `apps/web/src`: 49 `aria-*`/`role=` attributes, **1**
`onKeyDown`, **0** `.focus()` calls, **0** error boundaries. One thrown render
error blanks the workspace. The Board is a spatial canvas with no keyboard access
whatsoever. "Every part needs to function as expected" does not survive that.

---
## Part 3 — The work, by cluster

**59 workstreams · 348 criteria that can fail.** Each carries what it is in plain
language, why Aleph needs it, how, criteria, a review step and an iteration step.

Ids are prefixed `WS-` deliberately: the completeness critic found that bare ids like
`A2`, `D1` and `E1` already mean three different things across `docs/acceptance.md`,
`docs/backlog.md` and this plan — and that one review step already pointed an executor
at the right file and the wrong row.


---

### The research capability: is it actually state of the art?


#### WS-RS1 · Unbreak retrieval on the deployed stack, and make it impossible for search to fail silently

**What it is.** On the machine that is actually running right now, the search that powers every other feature returns nothing at all. 75 documents were fetched, 45 were converted to text, and then zero were cut into searchable pieces. The cause is one wrong word: both starter configurations ask the gateway for an embedding model called `titan-embed-v2`, and the gateway serves one called `titan-embed-text-v2`. Every request comes back 400. Three design choices turn that small mistake into a total blackout.

**Why.** Every number, claim and feature in this cluster is measured through this path. The headline 0.91 recall figure is a laboratory result with no production counterpart today — the same code scores 0.00 on the running deployment. Nothing else in this plan can be observed, tested or trusted until chunks exist. It is also the clearest violation of the prod-ready bar: a total capability outage that no dashboard, log level or user-facing message reports.

**How.** Five changes. (a) Stop shipping a model name. `apps/api/alembic/versions/20260527_1200_inc0_initial.py:85` and `:151` seed both templates with `"model": "titan-embed-v2"`. New migration nulls the `embedding` binding on both templates; project creation enqueues the existing `POST /v1/projects/{id}/model-profile/autoconfigure` (`apps/api/src/aleph_api/routes/model_profile.py:113`) so the binding is chosen from `/model/info` + `/v1/models` and probed before binding. Enqueue, never await inline — the agent runs in-process in FastAPI. Also fix `packages/aleph-rks/src/aleph_rks/embedding.py:36`, whose `KNOWN_EMBEDDING_DIMS` registry lists the wrong name and omits the served one. (b) Reorder `apps/workers/src/aleph_workers/jobs/chunk_embed.py`: insert `DocumentChunk` rows (the trigger fills `text_tsv`) at :192 BEFORE the `embed_texts` call at :180, then update embeddings in a second pass.

**Criteria:**

- No ingested document is left without chunks on the live stack
  <br>`uv run python scripts/_acceptance/status_numbers.py` reports `unindexed_documents 0`. **CORRECTED 2026-08-22:** the original counted every row, including in-flight `[e2e]` ingests from the browser suite — 81 of them on the day this was audited, 0 of them in a real project. A health number a test run can turn red is one people learn to skim, so it carries the same `p.title not ilike '[e2e]%' and not ilike 'smoke test%'` exclusion the health script already applies, and prints `unindexed_documents_fixtures` beside it so the scope cannot hide anything.`
- No ModelProfile binds a model the gateway does not serve
  <br>`uv run python scripts/_acceptance/gateway_serves_bound_models.py exits 0. FAILS TODAY: exits 1, because titan-embed-v2 is absent from /v1/models (confirmed: gateway returns 26 model ids, embedding ones are titan-embed-text-v2, cohere-embed-v4, bedrock-titan-embed-text).`
- A dead embedder degrades to keyword-only search instead of no search
  <br>`New test tests/integration/test_chunk_embed_degrades.py::test_dead_embedder_still_writes_chunks — monkeypatch embed_texts to raise, run chunk_embed_job, assert document_chunks count > 0 and search_corpus with a zero vector returns >=1 lexical hit. Fails today: chunks are inserted after the embed call.`
- A failed index job is visible, not stuck
  <br>`Same test asserts agent_runs.status='failed' and error_text is not null; plus psql -tAc "select count(*) from agent_runs where status='running' and started_at < now() - interval '1 hour'" returns 0. FAILS TODAY: returns 45.`
- The assistant reports degradation rather than emptiness
  <br>`packages/aleph-assistant/tests/test_router_degradation.py asserts RetrievalResult.degraded == 'embedder_unavailable' and that the composed body names it. Fails today: router.py:346-353 returns [] with only a warning log.`
- The repair job is idempotent
  <br>`Run backfill_unindexed_for_project twice; second run returns (0, 0). Any non-zero on the second pass is a failure.`

**Review.** Mutation test the whole path. Rebind a project's embedding to a nonsense model id, re-run ingest, and confirm four things fail as designed: the gateway acceptance check exits 1, chunks are still written (lexical search still answers), the AgentRun ends 'failed' with error text, and the assistant says 'degraded'.
<br>**Iterate.** v2 turns degradation into a first-class state rather than a boolean: RetrievalIndexRecord.state in {lexical_only, embedded, stale}, rendered on the PipelineStrip with a Reindex action, plus a startup reconciliation sweep that fails any AgentRun still 'running' past a deadline. That converts a class of silent worker hangs — not only this one — into visible state.
<br>**Depends on:** —
<br>**Risk.** Making `embedding` nullable weakens an invariant the HNSW index assumes — pgvector will simply not index NULL rows, which is what we want, but ordering by cosine_distance over a NULL column is undefined, so the explicit filter in (c) is load-bearing and must be tested, not assumed. Second risk: autoconfigure on project creation adds a network round-trip to a hot path;


#### WS-D1 · Make wiki commit atomic on the path agents actually use

**What it is.** Saving a page picks its version number by asking the database 'what is the highest version so far?' and adding one. When two things save the same page at the same moment — which happens whenever two ingested sources mention the same topic — both get the same answer, both try to write version N, the database rejects the second, and the caller gets an unhandled 500 with their work discarded. There is a second, unreported version of the same bug in the same function: two saves that both CREATE the same new page title both see 'this page does not exist' and collide on the page table instead.

**Why.** This is on the agent path, not a rare corner: five call sites pass no page id at all, and the wiki ingest workflow's page id is optional, so concurrent ingest of two sources that mint the same topic title races every time. It is also on the claim-write path — every claim in the system today was written through this function — so the belief work in RS8 inherits the race unless it is fixed or moved. And a 500 that loses a commit is the plainest possible failure of the prod-ready bar.

**How.** `packages/aleph-wiki/src/aleph_wiki/wiki_service.py:434-443` performs a plain SELECT on (project_id, slug) with no `with_for_update()` and returns the row unlocked; only the by-id branch at :415-424 takes a lock. Then `commit_revision` computes `new_rev_no = (max_rev or 0) + 1` at :239-246 with no lock held, and inserts against `uq_wiki_rev_page_no` = UNIQUE(page_id, revision_no) (`models.py:126`). Replace the create-or-fetch with an atomic upsert: `INSERT INTO wiki_pages ... ON CONFLICT (project_id, slug) DO NOTHING RETURNING id` against `uq_wiki_pages_project_slug` (`models.py:41`), then re-SELECT `... FOR UPDATE` in the same transaction. That makes create-or-lock one step and closes both races at once. Add a single bounded retry on IntegrityError against `uq_wiki_rev_page_no` as a belt-and-braces measure, using a shared `retry_on_unique_violation` helper rather than an inline try.

**Criteria:**

- Concurrent by-title commits to the same page all succeed with distinct version numbers
  <br>`New test tests/integration/test_commit_revision_concurrency.py::test_concurrent_by_title_commits_do_not_collide — 8 concurrent commit_revision(page_id=None, title='X') across 8 real sessions; assert 8 revisions exist with revision_no 1..8 and no exception raised. FAILS TODAY with IntegrityError on uq_wiki_rev_page_no.`
- Concurrent creation of the same new title produces exactly one page
  <br>`::test_concurrent_creates_of_a_new_title_produce_one_page asserts psql select count(*) from wiki_pages where slug='x' returns 1. Fails today on uq_wiki_pages_project_slug.`
- The duplicate-version invariant holds on live data
  <br>`psql -tAc "select page_id, revision_no, count(*) from wiki_revisions group by 1,2 having count(*) > 1" returns 0 rows.`
- No unlocked page read remains in the create-or-lock path
  <br>`scripts/check-page-lock.sh asserts every select(WikiPage) inside _lock_or_create_page carries with_for_update() or is the ON CONFLICT upsert; exits 1 otherwise. Fails today against the current file.`
- The acceptance gate covers it and the check can fail
  <br>`./scripts/acceptance.sh --self-check exits 0 with rows C9/C9a included, proving the check fails when the lock is removed. Rows C9/C9a, not a 'D1 part': part D is the kernel/skills cluster and the wiki-commit rows are filed under C on purpose (acceptance.sh:398-407 says why).`

**Review.** Mutation: revert the ON CONFLICT upsert to the plain SELECT and confirm the concurrency test fails with IntegrityError on uq_wiki_rev_page_no; remove with_for_update() and confirm the version-number test fails; restore both and confirm green. Then run the ingest workflow against two sources that mint the same topic title, 20 times, and confirm zero 500s in the API log.
<br>**Iterate.** v2 generalises the retry helper across the other max+1 sites in the codebase and adds a sweep that flags any `max(...) + 1` computed outside a row lock. Once RS8 moves claim writes off commit_revision entirely, the retry can be deleted and only the lock remains.
<br>**Depends on:** —
<br>**Risk.** `ON CONFLICT DO NOTHING RETURNING` returns no row on conflict, so the follow-up SELECT FOR UPDATE must be in the same transaction and can still deadlock under heavy fan-out — the retry must be bounded, not a loop. Second risk: holding a page row lock across a long commit serialises ingest if any caller holds it across a model call;


#### WS-E2 · Scholar search stops returning 503, and stops serialising behind a one-request-per-second gate

**What it is.** Literature search fails with 'service unavailable' in the API log. Three unrelated causes all wear that one error code. First, any error response from OpenAlex or Crossref at all — including a 400 caused by a malformed filter Aleph itself sent — is converted to 'the upstream service is unavailable' with no retry, which is both wrong and unactionable. Second, genuine rate limits survive only three retry attempts with an eight-second ceiling.

**Why.** The research loop's search phase runs through this. RS7 (making the loop read its sources) is pointless if the search phase cannot fetch anything, and the fan-out that E5 in the backlog describes as 'weirdly rate limited' is this same token bucket. It is also a correctness issue in error reporting: a 503 tells an operator the internet is down when the actual message is 'your filter syntax is wrong', which is exactly the kind of misdirection that costs days.

**How.** `packages/aleph-scholar/src/aleph_scholar/http.py:95-106` — `ensure_ok` raises `ScholarUpstreamError` on any status >= 400, and `apps/api/src/aleph_api/routes/scholar.py:176-177` maps that to `GatewayUnavailable`, whose http_status is 503 (`packages/aleph-core/src/aleph_core/errors.py:56-60`). (a) Split the mapping by status: 400/404/422 from upstream become a 4xx carrying the upstream reason; 429 and 5xx surviving retries become 503 with a `Retry-After` header derived from the upstream one. (b) `_is_retryable` (:42-48) already covers 429/5xx, but `stop_after_attempt(3)` with `_RETRY_AFTER_CAP_S = 8.0` (:38) is thin. Replace the fixed cap with a per-request deadline so the retry budget is expressed in wall-clock time the caller chose. (c) The throughput fix.

**Criteria:**

- Upstream client errors are not reported as service outages
  <br>`New packages/aleph-scholar/tests/test_http_status_mapping.py: a stub returning 400 produces a 4xx from POST /v1/scholar/search carrying the upstream reason; a stub returning 429 on every attempt produces 503 with a Retry-After header. FAILS TODAY: both produce a bare 503.`
- Concurrent scholar searches do not serialise into timeouts
  <br>`scripts/_acceptance/scholar_search_under_fanout.py fires 8 concurrent POST /v1/scholar/search and asserts >=7 return 200 within 30s. Fails today: 1 req/s x 8 plus three retry attempts each exceeds the budget.`
- Identical concurrent queries hit upstream once
  <br>`Test with a counting fake httpx transport: 8 identical concurrent queries produce exactly 1 upstream request. Fails today: 8.`
- The blanket status mapping is gone
  <br>`grep -n 'except ScholarUpstreamError' apps/api/src/aleph_api/routes/scholar.py shows a status-aware mapper, and grep -c 'GatewayUnavailable' in that file no longer covers the 4xx case; a unit test pins the mapping table.`
- No 503s on the endpoint in a day of real traffic
  <br>``scripts/_acceptance/scholar_search_under_fanout.py` completes with 0 responses carrying status 503. A probe run, not `docker logs … | grep -c 'scholar/search.*503'`: a log grep over a day with no traffic returns 0 by vacuum, so the old form is satisfied by the endpoint never being called.`

**Review.** Mutation: point the OpenAlex base URL at a stub that always returns 400 and confirm the route returns 4xx with the upstream reason rather than 503; point it at a stub that always returns 429 and confirm 503 plus Retry-After; set the token bucket rate to 0.01 and confirm the fan-out probe fails on the deadline rather than hanging. Restore each and confirm the probe passes.
<br>**Iterate.** v2 moves the token bucket into Redis so the limit is per-deployment rather than per-process (it is per-process today, so two API replicas silently double the rate), and adds a per-host circuit breaker that opens after N consecutive failures and reports 'OpenAlex is unreachable' on the pipeline strip instead of failing every individual query.
<br>**Depends on:** —
<br>**Risk.** Raising the rate limit risks getting the deployment's mailto address blocked — OpenAlex's polite pool is 10 req/s with a mailto, so 1 req/s is over-conservative, but the ceiling must stay configurable and default conservative, and the change must be validated against the real service, not a stub.


#### WS-RS4 · Make the retrieval number a gate that can actually fail

**What it is.** Aleph's headline research claim is a measured number: 0.91 out of 1.0 for 'the right document came back first'. The number is real — I reproduced it. But nothing re-checks it. Continuous integration runs linting, typechecking, sweeps and tests; it has never run the search evaluation and has never run the acceptance script. Meanwhile the acceptance script invokes four test files that were deleted in the harness reset, so eleven of its parts fail the instant a database is reachable — and pass as 'skipped' when one is not, which is how the tree still looks green.

**Why.** This is the project's own stated principle failing: 'a check nobody has seen fail is an assumption wearing a green light'. Every improvement in RS5, RS6, RS7 and RS10 is a claim about a number, and there is currently no mechanism that would catch any of them regressing. It is also the specific mechanism CLAUDE.md blames for a broken retrieval path surviving seven work packages. Fixing this before the quality work means every later workstream lands with evidence instead of a story.

**How.** (a) Rewrite the deleted end-to-end tests. `tests/` now holds only `unit/` (2 files) and `integration/` (3 files); `tests/e2e/` does not exist. `scripts/acceptance.sh` invokes `tests/e2e/test_retrieval_finds_body_text.py` (:167,:169,:173), `test_search_corpus.py` (:171), `test_belief_spine.py` (:230-257) and `test_retraction_walk.py` (:246). pytest exits 4 on a missing path and `run_pytest` (acceptance.sh:80-82) records that as FAIL — so B1, B2, B3, B7, C1, C3, C4, C5, C6, C7 and C8 all fail today. Rewrite them against the current code; where the harness reset changed behaviour, redefine the part rather than porting a stale assertion. (b) Delete the dead second runner.

**Criteria:**

- The acceptance gate is green for a real reason
  <br>`./scripts/acceptance.sh with Postgres reachable exits 0 and reports 0 FAIL. FAILS TODAY: 11 parts FAIL because four invoked test files do not exist (pytest exits 4).`
- The eval CLI runs the real path and can fail
  <br>`uv run python -m aleph_evals exits non-zero when recall@1 is below baseline. FAILS TODAY: the same command prints selected_datasets: [] and exits 0.`
- No scorer reads its answer from the fixture it is grading
  <br>`uv run pytest packages/aleph-evals/tests -q` includes a test that a case carrying its own `actual` is counted as **errored**, not passed. **CORRECTED 2026-08-22:** the original grep now matches the FIX rather than the defect — `scorers.score()` takes `actual=` as a separate argument and raises `SelfGradingFixture` when the case supplies one (`scorers/__init__.py`), and `_run_dataset` counts that as errored (`runner.py`). Seven hits, all correct. A grep that forbids the name of the thing that fixed it is a criterion that can only be satisfied by undoing the fix.`
- CI runs the eval
  <br>`grep -c 'aleph_evals' .github/workflows/ci.yml is >= 1. Returns 0 today.`
- Every acceptance check can be shown to fail
  <br>`./scripts/acceptance.sh --self-check exits 0 with the newly added and repaired parts included.`
  <br>`The acceptance run reports E5 as a known, unfixed defect until RS8 lands, rather than 'FIXED'. Today it reports FIXED for a module with no callers.`

**Review.** Mutation across three layers. Flip `or_tsquery` back to `plainto_tsquery` at retrieval.py:98 and confirm the CI eval job fails on the recall floor; restore. Delete one tests/e2e file and confirm acceptance reports FAIL, not SKIP; restore.
<br>**Iterate.** v2 records every eval run into a small results table of its own, so the number has a history and a regression is a visible trend rather than a single boolean, and publishes it on a health pane. That also gives the self-improving-harness thesis its first real feedback signal: the agent can see whether its own changes moved the number.
<br>**Depends on:** WS-RS1
<br>**Risk.** Checked-in embeddings pin one embedding model; if the default embedder changes, the baseline silently measures something else. Mitigate by recording the model id in baseline.json and failing loudly on mismatch. Second risk: the deleted tests are being rewritten from the acceptance script's node ids, which describe the pre-reset design — some parts may no longer be the right ass…


#### WS-RS5 · An evaluation set that can tell good from great

**What it is.** The measuring stick is too short to measure anything interesting. The test corpus is twelve documents, the longest under sixty words, with forty-five questions that each have exactly one right answer. Nine in ten questions are already answered inside the top eight results, so that score is saturated and can only fall. A single question is worth 2.2 points, so noise and real progress look identical.

**Why.** Everything after this workstream is a claim that a number went up. On the current set, a reranker, contextual embeddings or query rewriting could each be worth exactly one question and be indistinguishable from noise. Worse, the eval measures a better-conditioned corpus than production produces, so it will report improvements that do not exist in the running system. Without this, RS6 and RS10 are unfalsifiable, and the wiki-deletion decision in RS10 rides on a number the set cannot resolve.

**How.** (a) Grow the corpus to >=300 documents of realistic length (2-15k chars), drawn from material Aleph actually ingests plus a redistributable public set, with a NOTICE recording provenance and licence. Keep it as JSONL under `packages/aleph-evals/datasets/retrieval/`. (b) Run the corpus through the real chunker. `packages/aleph-evals/src/aleph_evals/retrieval_eval.py:206-230` seeds exactly one DocumentChunk per document with `section_path=None, char_start=0`, and disables the diversity cap at :244 (`per_source_cap=None, # one chunk per source anyway`). Replace with `chunk_markdown(markdown)` so chunking, overlap, section_path and the cap are inside the measurement. (c) Close the eval/production mismatch. `retrieval_eval.py:200` embeds `f"{doc['title']}. {doc['text']}"`; `apps/workers/src/aleph_workers/jobs/chunk_embed.py:186` embeds `c.text`.

**Criteria:**

- The set is large enough to resolve small changes
  <br>`uv run python -m aleph_evals.build_retrieval_set` emits >= 300 documents and >= 150 questions to the directory `ALEPH_RETRIEVAL_DATASET` names, and the eval run against it reports its size. **CORRECTED 2026-08-22:** the original required the COMMITTED set to grow, which contradicts this workstream's own Risk paragraph — the corpus is published papers and redistributing them is not the eval's call. The committed set stays small and CI measures it lexical-only; the large set is generated per instance.`
- The eval reports ranking-quality metrics, not just recall
  <br>`uv run python -m aleph_evals prints ndcg@10, mrr, recall@1/3/8/20 and a per-category breakdown including 'unanswerable'. Today it prints recall only.`
- The measurement has headroom
  <br>`At least one headline metric is below 0.90 on the new set (today recall@8 is 0.98 and effectively saturated). A set on which every metric exceeds 0.95 is rejected and made harder.`
- Chunking is inside the measurement
  <br>`grep -c 'chunk_markdown' packages/aleph-evals/src/aleph_evals/retrieval_eval.py >= 1. Returns 0 today.`
- The eval and production embed identical strings
  <br>`New test test_eval_and_production_embed_the_same_text asserts both retrieval_eval and chunk_embed call the same contextual-text function. FAILS TODAY: one prefixes the title, the other does not.`
- Unanswerable questions produce an honest miss
  <br>`The eval reports an 'abstain rate' on the unanswerable category and it exceeds 0.80. No such category exists today, so the metric cannot be computed at all.`

**Review.** Establish run-to-run variance first by running the eval five times with different chunk orderings and recording the spread. Then mutation-test the set itself: shuffle 10% of the gold labels and confirm every metric moves by more than that variance; reverse the fused ranking and confirm nDCG@10 collapses to near zero.
<br>**Iterate.** v2 splits the set into a public dev half and a held-out half that only CI sees, so tuning in RS6 and RS10 cannot overfit the number the gate reports. Add a per-question failure report so a regression names the specific question that broke rather than moving an aggregate.
<br>**Depends on:** WS-RS4
<br>**Risk.** The largest risk is labelling with a model and then measuring a model: the labels become an agreement score, not a correctness score. Budget a human pass over every multi-hop and unanswerable question specifically. Second risk: corpus licensing — every document must be redistributable, with a NOTICE, or the repository cannot ship the set.


#### WS-RS6 · Retrieval quality: reranking, context-aware chunk embeddings, and vector-scan tuning

**What it is.** Three concrete upgrades to how search finds things. First, reranking: today Aleph takes the top results from two different rankings and interleaves them, and stops there. The state of the art adds a second, slower, more accurate pass that re-reads the top fifty candidates and reorders them. Aleph already has the settings switch for this — the Settings drawer literally shows a row labelled 'Reorders retrieved chunks' — with nothing behind it.

**Why.** This is the gap between 'a working hybrid search' and 'state of the art'. A repo-wide search for reranking, query rewriting, HyDE, MMR, late interaction, GraphRAG or SPLADE returns exactly one hit — the unused capability enum. Reranking is the single highest-leverage addition and its entire configuration surface is already built and shipped to users with no implementation behind it, which is the codebase's signature defect (a producer with no consumer) inverted: a consumer with no producer.

**How.** (a) Reranking. Add `LiteLLMClient.rerank()` posting to `/v1/rerank` — I verified that route is live on the configured gateway (it returns 400 for a non-rerank model, not 404). Bind it through `Capability.RERANK`, which already exists end to end with no implementation: the enum at `packages/aleph-core/src/aleph_core/schemas/model_profile.py:21`, the discovery policy `CapabilityPolicy(mode="rerank", tier="light")` at `packages/aleph-models/src/aleph_models/discovery.py:392`, and the UI row at `apps/web/src/components/Drawers.tsx:189,201`. `packages/aleph-models/src/aleph_models/client.py` has only `chat` (:251) and `embed` (:401).

**Criteria:**

- Reranking measurably improves ranking quality
  <br>`uv run python -m aleph_evals --rerank on beats --rerank off by >= 0.05 nDCG@10 on the RS5 set, printed side by side by one command. Fails today: grep -rn 'def rerank' packages/aleph-models/src returns 0 — there is no rerank path at all.`
- The rerank capability has a real caller
  <br>`grep -rn 'capability=Capability.RERANK' --include='*.py' apps packages | grep -v tests | wc -l` returns >= 1 (6 today: `aleph_rks/rerank.py:585` and five in `aleph_models/client.py`). Returns 0 today. `capability=`, not the bare name: the bare name also matches the docstrings explaining what reranking is, so it would stay green if every call site were deleted and the explanation left.
- Vector scan settings are configured, not left at defaults
  <br>`grep -rn 'ef_search\|iterative_scan' packages/aleph-rks/src returns >= 1. Returns 0 today (repo-wide).`
- The dense leg actually returns the number of rows it asks for
  <br>`test_dense_leg_returns_full_window seeds 10k chunks across 5 projects and asserts the dense query returns exactly `fetch` rows for the target project; a companion test asserts SHOW hnsw.ef_search inside the same transaction returns the configured value. FAILS TODAY: returns fewer than fetch under post-filtering.`
- Grounding spans survive contextual embedding
  <br>`uv run pytest packages/aleph-rks/tests/test_chunk_offsets.py exits 0 — markdown[char_start:char_end] == chunk.text still holds.`
- The agent can search the corpus directly
  <br>`grep -c 'search_corpus\|search_sources' apps/api/src/aleph_api/copilot_agent.py >= 1. Returns 0 today; grep -rn search_corpus apps/ returns 0 hits across the whole app tree.`
- Rerank is never silently skipped
  <br>`Test asserts a retrieval.rerank.skipped span attribute with a stated reason when no rerank capability is bound.`

**Review.** Mutation on all three legs. Bind Capability.RERANK to a stub that returns the reversed order and confirm nDCG@10 collapses — that proves the reranker's output is actually consumed rather than computed and dropped. Set hnsw.ef_search to 1 and confirm the row-count test fails. Strip section_path from the contextual text and confirm the eval drops measurably. Restore each and confirm the numbers return.
<br>**Iterate.** v2 measures whether the LLM listwise reranker earns its latency on the request path — the agent runs in-process inside FastAPI, so a two-second rerank is two seconds of user-visible delay. If it does not, move it behind a 'deep' flag and keep the fast path fusion-only. Then evaluate query decomposition and HyDE against the same set;
<br>**Depends on:** WS-RS1, WS-RS5
<br>**Risk.** The deployed gateway serves no reranker, so the primary backend cannot be validated against the real deployment and the LLM fallback is the one that will actually run — it is slower and non-deterministic, which is a latency and reproducibility risk on the in-process agent path.


#### WS-RS7 · The deep-research loop must read the sources it fetched

**What it is.** This is the most serious defect in the cluster. Aleph's flagship research feature searches for papers, downloads them, ingests them — and then writes the report without ever reading them. The composing step is handed a list of titles and web links and asked to write the body with citation markers in it. So the report is written from the model's own recollection, and the citation numbers are assigned by position in a list rather than by what any source says.

**Why.** If a research harness composes prose from titles and then blesses its own citations by counting them, it is not a research harness; it is an essay generator with footnote decoration. That single fact answers the cluster's question 'is the research capability state of the art' more directly than anything else here. It is also the exact failure mode the belief layer exists to prevent, and the reason the live database holds 786 citations with no quote and no chunk anchor.

**How.** (a) `packages/aleph-research/src/aleph_research/research_workflow.py:849-877` — `_node_compose` builds `listing = "\n".join(f"c{i}: {s.title}" + (f" — {s.url}" ...))` at :859-861 and sends exactly that as the user message at :866-871. Replace it with an evidence pack: for each sub-question in the plan, call `aleph_rks.retrieval.search_corpus` scoped to the ingested source ids, plus `descend_into_source` for the top sources, and send the retrieved chunk TEXT with a stable marker per chunk rather than per source. `grep -c search_corpus packages/aleph-research` returns 0 today. (b) `ResearchSourceRef` (`packages/aleph-wiki/src/aleph_wiki/synthesis_workflow.py:49-52`) carries only short_id, title and url. Extend it with chunk_id, char_start and char_end so a marker resolves to an exact span.

**Criteria:**

- The composer sees source text
  <br>`New tests/e2e/test_research_reads_sources.py::test_compose_prompt_contains_source_text — run the loop over a seeded corpus containing a distinctive sentence, capture the research.compose prompt, assert the sentence appears in it. FAILS TODAY: the prompt contains only 'cN: title — url' lines.`
- Every citation resolves to a verbatim span in the cited chunk
  <br>`::test_every_citation_resolves_to_a_verbatim_span asserts ground(citation.verbatim, chunk.text) is not None for every Citation a research run writes. Fails today: verbatim is never written (live DB: 786 citations, 0 verbatim).`
- A fabricated quote blocks the commit
  <br>`::test_a_fabricated_quote_fails_the_commit injects a marker whose quote appears in no chunk; assert the commit raises and no revision row is written. Fails today: the range check passes it.`
- Research-path citations carry a source anchor
  <br>`psql -tAc "select count(*) from citations c join wiki_revisions r on ... where c.source_id is null" for a project after a research run returns 0. Today the research path writes NULL for all of them.`
- The report builder no longer discards claims
  <br>`grep -n 'claims=\[\]' packages/aleph-research/src/aleph_research/research_workflow.py returns 0 hits. Returns 1 today (line 337).`
- The research package retrieves at all
  <br>`grep -c 'search_corpus' packages/aleph-research/src/aleph_research/research_workflow.py >= 1. Returns 0 today.`

**Review.** Mutation: restore the title-only listing and confirm the prompt-content test fails; make ground() always return a span and confirm the fabricated-quote test fails; remove the source_id kwarg and confirm the NULL-anchor query goes non-zero. Restore each.
<br>**Iterate.** v2 adds a claim-level verification pass — does the sentence the citation is attached to actually follow from the quote? — as a reviewer in aleph-reviewer, and wires the DeepResearch Bench adapter that already exists and is imported by nothing (packages/aleph-evals/src/aleph_evals/adapters/deepresearch_bench.py:40 currently scores passed = status == 'completed', which measures liveness, not quality).
<br>**Depends on:** WS-RS1, WS-E2
<br>**Risk.** Sending retrieved chunk text instead of a title list multiplies the compose prompt's token count by one to two orders of magnitude — cost and context-window pressure become real, and the evidence pack needs a hard budget with a documented selection policy rather than 'send everything'.


#### WS-RS8 · Evidence-anchored claim extraction: give the belief layer its first caller

**What it is.** Aleph's durable knowledge is meant to be claims — individual statements, each pinned to the exact sentence in a source that supports it, with a confidence computed from that evidence rather than guessed by a model. All of that machinery is written and tested. None of it has ever run. The module that does the writing has zero callers anywhere in the codebase: searching for its name returns three hits, all inside the file itself. The live database proves it — 786 claims with no stable identity key and no vector, 786 citations with no quote and no link to any passage, and zero links between claims.

**Why.** CLAUDE.md and the backlog both list the Claim Spine as done. It is a file, not a capability. This is the largest single gap between what the project says it is and what it does, and it is the load-bearing piece of the whole product thesis: prose is supposed to be rendered from a belief layer, not be the layer.

**How.** (a) Replace the producer. `packages/aleph-wiki/src/aleph_wiki/agent/workflow.py:405-445` asks the model for `claims: [{text, citation_marker}]` and builds `ClaimDraft(text=..., confidence="cited", citations=[CitationDraft(chunk_ids=[], ...)])` — no quote, no chunk, no span. Write an extractor that runs over the source's CHUNKS (which RS1 makes exist) and emits, per claim, a proposition plus one or more verbatim quotes plus the chunk each came from. (b) Route the writes through `BeliefService` (`packages/aleph-wiki/src/aleph_wiki/belief_service.py:151`) instead of commit_revision's claim block. That one change lights up `aleph_core.grounding.ground` (called at belief_service.py:264 and nowhere else outside its own tests), claim_key identity, and the derived-confidence recompute. (c) Move it out of the package being deleted.

**Criteria:**

- The belief write path has a real caller
  <br>`grep -rn 'BeliefService' --include='*.py' . | grep -v 'packages/aleph-belief/' | wc -l >= 2. Returns 0 today (all 3 hits are self-references inside belief_service.py plus one docstring mention in models.py:236).`
- Citations written by ingest carry a quote and a chunk
  <br>`After ingesting one document on the live stack: psql -tAc "select count(*) from citations where source_id = '<new>' and (quote is null or char_start is null or jsonb_array_length(coalesce(chunk_ids, '[]'::jsonb)) = 0)" returns 0.` **CORRECTED 2026-08-22:** the original was **unsatisfiable by schema and therefore always green** — `verbatim` is a NOT NULL boolean, so `verbatim is null` matches nothing, and `chunk_ids` is `jsonb`, so `= '{}'` compares an array column to an empty OBJECT and matches nothing either. The query returned 0 for every possible state of the database, including the state it was written to catch. Ungroundedness lives in `quote`, `char_start` and an empty `chunk_ids` ARRAY.`
- Claims have stable identity
  <br>`psql -tAc "select count(*) from wiki_claims where claim_key is null" for the new source returns 0, and re-ingesting the same source does not double the claim count. All 786 are NULL today.`
- The belief acceptance suite passes
  <br>`tests/e2e/test_belief_spine.py passes — the file acceptance.sh:230-257 invokes across parts C1, C3, C5, C6, C7, C8. FAILS TODAY: the file does not exist and pytest exits 4.`
- A fabricated quote is refused at the write path
  <br>`::test_a_fabricated_quote_is_refused — the extractor emits a quote not present in its chunk, ground() returns None, and no Citation row is written.`
- Merge proposal is bounded
  <br>`test_propose_is_bounded seeds 5,000 live claims and asserts propose() completes in under 5 seconds and issues at most N candidate comparisons. Fails today: 12.5 million comparisons, unbounded.`
- The workspace does not grow
  <br>`The existing acceptance E4 check (<= 21 workspace packages) still exits 0.`

**Review.** Mutation on four axes. Make ground() return a span for everything and confirm the fabricated-quote test fails. Replace claim_key with a random uuid and confirm the identity test fails and that re-ingesting doubles the claim count. Remove the blocking from propose() and confirm the bounded test times out. Delete the trust-tier weighting and confirm WELL_SUPPORTED becomes unreachable again. Restore each.
<br>**Iterate.** v2 has the extractor read the RS6-reranked chunks rather than the document in order, so it sees the most relevant passages first. Then add contradiction detection: nothing in the tree has ever written a 'contradicts' stance (all 786 citations are 'supports'), and without it CONTESTED is unreachable and the belief layer is only a citation index. Contradiction is what makes it a web of belief.
<br>**Depends on:** WS-RS1, WS-D1
<br>**Risk.** The extractor is an LLM step whose output quality determines everything downstream, and there is no eval for extraction quality — build one (precision and recall of extracted claims against a hand-labelled set of 20 documents) or this ships unmeasured, which is exactly the failure mode the rest of this plan exists to correct.


#### WS-RS9 · One confidence vocabulary, and a retraction that actually propagates

**What it is.** How confident the system is in a claim is written down in three different vocabularies that do not agree with each other. The engine that computes confidence emits six words. The user-interface catalog permits six different words, two of them spelled with hyphens where the engine uses underscores. The HTML renderer handles some of each and has no styling for three of the engine's values. And every one of the 786 claims in the live database carries the word 'cited', which is not a member of the engine's vocabulary at all.

**Why.** A knowledge layer whose confidence field means three things depending on which component reads it is not a knowledge layer. And retraction propagation is the single feature that distinguishes a belief web from a bibliography — it is why claims are first-class. Both defects are invisible today only because the belief layer has never run; the moment RS8 lands, three renderers start disagreeing about live data. This is the cleanup that makes RS8's output legible.

**How.** (a) Pick one enum and enforce it. The derived engine at `packages/aleph-hypotheses/src/aleph_hypotheses/confidence.py:28-34` emits under_investigation | weakly_supported | well_supported | contested | refuted | abandoned. The A2UI catalog at `packages/aleph-a2ui/src/aleph_a2ui/catalog.json:436-443` permits cited | well-supported | weakly-supported | contested | uncited | retracted. `packages/aleph-wiki/src/aleph_wiki/html_compiler.py:65-73` maps both spellings of two values and has no entry for refuted, abandoned or under_investigation. `apps/web/src/a2ui/components/ClaimCard.tsx:12-18` branches on four strings only. Make the derived enum canonical, regenerate the catalog with scripts/gen_catalog.py, and add scripts/check-confidence-vocabulary.sh diffing the Python enum against the catalog enum against the renderer's branch labels.

**Criteria:**

- The three vocabularies agree
  <br>`./scripts/check-confidence-vocabulary.sh exits 0. It does not exist today; written against the current tree it exits 1 on at least three mismatches.`
- No live claim carries a value outside the canonical enum
  <br>`psql -tAc "select distinct confidence from wiki_claims" returns only Confidence members. Returns 'cited' today, which is in neither the engine enum nor a valid derived state.`
- Retraction propagates two hops
  <br>`tests/e2e/test_retraction_walk.py passes with a two-hop case: retract a source, assert both the directly-cited claim and a claim derived from it are flagged. Fails today — the file does not exist, and the second hop is structurally dead regardless.`
- derived_from edges are written
  <br>`psql -tAc "select count(*) from claim_edges where kind='derived_from'" > 0 after a research run. Returns 0 today for every kind.`
- The top confidence state is reachable
  <br>`Test seeds three tier-A supporting citations and asserts next_confidence_from_evidence returns WELL_SUPPORTED. FAILS TODAY: weight is 1.0 everywhere, so max_pos never reaches the 1.5 threshold at confidence.py:62.`
- The renderer cannot silently drop a state
  <br>`pnpm -C apps/web build passes with an exhaustive switch and a TypeScript never-check on the confidence union; adding a member without a branch fails the build.`

**Review.** Mutation: add a seventh member to the Python enum without regenerating the catalog and confirm the sweep exits 1; delete the derived_from write and confirm the two-hop retraction test fails; drop the trust-tier weight back to 1.0 and confirm the WELL_SUPPORTED test fails; add an unhandled member and confirm pnpm build fails. Restore each.
<br>**Iterate.** v2 adds declined branches to the retraction walk — a human saying 'this retraction does not affect this claim' — and surfaces the blast radius in the GroundingSurface pane, so it is something a person looks at rather than something an endpoint returns. That is what makes retraction a workflow instead of a query.
<br>**Depends on:** WS-RS7, WS-RS8
<br>**Risk.** Recomputing confidence for 786 existing claims changes what the interface shows for every one of them, and some may reflect human judgement — the migration must skip origin='user' rows or it silently overwrites the one thing the belief design promises is immutable.


#### WS-RS10 · Claim-level retrieval, and making the wiki-deletion gate a runnable decision

**What it is.** The plan of record says the legacy wiki gets deleted once the claim layer retrieves better than the current search does. But the command named as the test cannot test that: the evaluation harness only knows how to search document passages, has no mode for searching claims, and the claims table has a vector index that nothing has ever filled. So the gate that governs the largest structural decision in the codebase is not runnable as written. This workstream builds claim search, makes the comparison a single command, and turns 'should we delete the wiki' into a number a script returns.

**Why.** CLAUDE.md says the wiki stays until the belief path beats the measured retrieval number, and docs/acceptance.md:158 states that as the unblock condition. Until this exists, the wiki cannot be deleted, the package count cannot come down, the legacy code the whole team is told not to touch stays in the tree, and the product thesis stalls one step short. It is also the honest test of whether a claim graph actually retrieves better than passages — an open question this plan should be willing to answer 'no' to.

**How.** (a) Add a surface flag to the eval. `packages/aleph-evals/src/aleph_evals/retrieval_eval.py:206-254` seeds DocumentChunk rows and calls search_corpus only; there is no belief mode. Add `--surface {chunks,claims,both}`. (b) Populate `wiki_claims.embedding` at claim-write time in RS8's path. The HNSW index exists at `packages/aleph-wiki/src/aleph_wiki/models.py:204-208`, the column is nullable at :243, and 786 of 786 rows are NULL. (c) Implement `search_claims` in aleph-belief mirroring `_hybrid_search` (`packages/aleph-rks/src/aleph_rks/retrieval.py:62-142`): dense over claim embeddings plus lexical over claim text, fused by RRF, plus a hop over claim_edges — the graph hop is the thing a claim layer can do that passage search cannot, and if it does not help, that is a finding worth reporting rather than hiding.

**Criteria:**

- Claim retrieval is measurable
  <br>`uv run python -m aleph_evals.retrieval_eval --surface claims runs and prints recall@k and nDCG@10. The module matters: `python -m aleph_evals` is the dataset runner and computes no recall at all, so the criterion named a program that could not satisfy it.`
- Claims are embedded at write time
  <br>`psql -tAc "select count(*) from wiki_claims where embedding is null and created_at > '<RS8 landing date>'" returns 0. All 786 existing rows are NULL today.`
- The wiki-deletion gate is a single decidable command
  <br>`uv run python -m aleph_evals.retrieval_eval --surface both` prints chunks vs claims side by side and the delta is recorded. **CORRECTED 2026-08-22:** it is a MEASUREMENT, not a gate. The original made it a boolean gating the wiki deletion, and `docs/decisions.md` D1 reversed that deletion — both knowledge plugins stay, so there is nothing for claims-beating-chunks to unblock. Exiting non-zero because one retrieval surface scores lower than another would fail the build over a fact about the corpus.`
- The acceptance document names a real command
  <br>`The command quoted at docs/acceptance.md:64 — row B5's `python -m aleph_evals.retrieval_eval` — runs and its flags appear in `--help`. Repointed from :158, which is Part D PROSE ('of those three is true, which is why four of these rows are 🟨'): there is no command there, so the criterion had no subject and nothing could turn it red. Third audit to flag it.`
- Claim retrieval has a caller
  <br>`grep -rn 'search_claims' apps/api/src/aleph_api/copilot_agent.py >= 1.`
- The graph hop contributes measurably or is removed
  <br>`The default claim run reports the hop as two counts — results reached ONLY via `claim_edges`, and questions answered only by one — and `--no-graph-hop` reproduces the run without it so the nDCG@10 delta can be stated. Measured: 0 and 0; 0.232 vs 0.228. Not 'the multi-hop question category from RS5': the set has `factual` and `unanswerable` and no multi-hop category, so the difference was to be reported over a slice that does not exist.`

**Review.** Mutation: zero out the claim embeddings and confirm --surface claims recall collapses; remove the graph hop and confirm the multi-hop category drops (and if it does not, report that honestly and delete the hop). Restore. Crucially, run the final comparison against the held-out split from RS5's iteration step, never the dev split, because this number decides a structural change and the incentive to tune it is real.
<br>**Iterate.** v2: if claims lose to passages, the correct next move is not to keep tuning claims — it is a hybrid that retrieves claims and then descends to their evidence chunks, which is what a knowledge graph is actually for. Measure that as a third surface before anyone decides to delete the wiki.
<br>**Depends on:** WS-RS5, WS-RS8
<br>**Risk.** The entire wiki-deletion decision rides on this one number, so the structural temptation is to tune the eval until claims win. Mitigate by freezing the RS5 set and its baseline before search_claims is written, and by having the held-out split cast the deciding vote.


#### WS-RS11 · PDF ingest that preserves structure

**What it is.** Most of what a research tool ingests is PDFs, and Aleph reads them as though they were plain text files. It gets a flat wall of words with no headings, no tables, no figure captions, no column ordering and no page numbers. The code even records 'headings found: 0' as a fixed value, with a comment admitting the library cannot tell. Because the passage splitter decides where a section begins by looking for markdown headings, every passage from every PDF ends up with no section label at all.

**Why.** This caps grounding quality below what the rest of the machinery could deliver. The chunker computes exact character offsets and a test asserts the span slices back to the original text — that precision is wasted on a document with no structure to be precise about. Section labels are also an input to RS6's contextual embeddings, so a flat PDF cannot benefit from that improvement. And a research harness that cannot read a table in a paper is not a research harness in any field where the result is in the table.

**How.** (a) `packages/aleph-rks/src/aleph_rks/normalization.py:85-145` holds two normalizers, pypdf and pdfminer, both emitting flat text and both hardcoding `structure = {"heading_count": 0, "table_count": 0, "figure_count": 0}` (:97-101 and :135-139). Add a layout-aware normalizer emitting real markdown with # headings, tables and figure captions. Evaluate Docling (MIT, in-process), Marker and GROBID (a service, which fits the compose deployment model) on numbers, not reputation. (b) Make the parser a per-project choice through the normalizer registry rather than a hardcoded default — this is the plugin thesis applied to ingest, and it is the shape the rest of the system is moving toward. (c) Emit page markers into the markdown so char_start/char_end map back to a page for the reader's grounding highlight. (d) Give 'ocr-required' a consumer.

**Criteria:**

- PDF passages carry section labels
  <br>`Both halves, and the first is not optional: `select count(*) from normalized_documents where parser like 'docling%'` is > 0, AND `select count(*) filter (where c.section_path is null)::float / count(*) from document_chunks c join normalized_documents n on n.id = c.normalized_document_id where n.parser like 'docling%'` is below 0.10. The ratio alone CANNOT FAIL: scoped to docling rows, of which production has zero, `nullif` makes it return NULL — neither a pass nor a fail, and a gate reading it as "not above 0.10" calls that green. The absence of docling rows is the defect this criterion exists to catch, so it has to be an explicit failure.` **CORRECTED 2026-08-22:** the original joined `sources s ... where s.kind='pdf'` and `sources` **has no `kind` column** — the query errors rather than returning a number, so the criterion could not be evaluated at all. Which parser ran is recorded on `normalized_documents.parser`, which is also the honest discriminator: a pypdf row legitimately has no headings, so measuring "all PDFs" would grade the chooser rather than the parser. Measured today over pypdf rows: **0.903**.`
- Structure metadata is measured, not hardcoded
  <br>`psql -tAc "select count(*) from normalized_documents where (structure_jsonb->>'heading_count')::int > 0 and parser like 'docling%'" > 0`. **CORRECTED 2026-08-22:** the column is `structure_jsonb`; as written the query errors rather than returning a number, so the criterion could never be evaluated at all. Scoped to the docling parser, since a pypdf row legitimately has no headings.`
- Tables are detected
  <br>`Same query on table_count > 0 returns non-zero for a fixture PDF known to contain a table.`
- The parser choice is made on numbers
  <br>`scripts/compare_pdf_parsers.py prints heading count, table count and extracted character count per PDF per normalizer across the fixture set, and its output is committed alongside the decision.`
- Structure improves retrieval, or the result is reported
  <br>`uv run python -m aleph_evals over a PDF-derived corpus, layout normalizer vs flat text, prints the nDCG@10 delta. The gain may be small; the criterion is that the number is produced and recorded either way.`
- The OCR flag has a reader
  <br>`grep -rn 'OCR_REQUIRED in' --include='*.py' packages | grep -v tests | wc -l` returns >= 1 — a BRANCH on the flag, at `aleph_rks/indexing.py:345`. Returns 0 today. Not the literal string 'ocr-required': all four of its hits outside `normalization.py` are prose — two in a test docstring quoting the defect, one in the operator-facing message, one in the comment above the fix — so the criterion would be satisfied by writing about the flag and never reading it.

**Review.** Mutation: swap the layout normalizer back to pypdf across the fixture set and confirm the first three criteria fail. Feed a scanned image-only PDF and confirm it is flagged and routed rather than silently ingested as a near-empty document — today a scan produces a flag nobody reads and an empty body nobody notices.
<br>**Iterate.** v2 extracts tables into structured rows so a table in a paper becomes queryable data rather than markdown, and treats figure captions as separately retrievable units. That is where a structure-aware ingest starts paying compound interest.
<br>**Depends on:** WS-RS1, WS-RS5
<br>**Risk.** Docling and Marker pull heavy ML dependencies including torch into the worker image, which means a large container, a slow cold start, and partial tension with the 'Aleph serves no models' principle. GROBID as a compose service keeps the model out of Aleph's image but adds an operational component to run and monitor.


#### WS-H8 · Make the wiki exportable, then align it with the Open Knowledge Format

**What it is.** Three places in the code state that an Aleph project opens as an Obsidian vault. There is no export path of any kind — nothing anywhere writes the wiki out as markdown files on disk. So the question the backlog raises ('should we match the emerging open format for agent-maintained wikis before it gets expensive?') has nothing to align, because nothing is emitted.

**Why.** The interoperability claim is currently false in three source comments, which is exactly the documentation style CLAUDE.md was rewritten to forbid. More substantively: an export path is what makes the wiki deletion in RS10 safe — a user can take their knowledge with them — and it is the concrete form of 'durable agent context', the thing OpenWiki is actually for. The backlog is right that alignment is cheap now and expensive later; it is wrong about where the cost sits.

**How.** (a) Build the exporter first — everything else is downstream of it. `packages/aleph-artifacts/src/aleph_artifacts/exporters/` contains markdown_bundle.py (report.md plus assets), pdf.py and source_pack.py; none writes a vault. Add vault.py producing one .md per page using frontmatter.render, whose only production caller today is hub-page generation at `packages/aleph-wiki/src/aleph_wiki/navigation.py:30`. Add a route in apps/api/src/aleph_api/routes/wiki.py. (b) Two link dialects behind one flag: 'obsidian' emitting [[wikilinks]] and 'okf' emitting [text](./slug.md). The parser already exists — `packages/aleph-wiki/src/aleph_wiki/frontmatter.py:38 _WIKILINK` — the renderer is new.

**Criteria:**

- A vault can be exported
  <br>`POST /v1/projects/{id}/export/vault returns a zip; unzip -l shows one .md per non-stub page plus index.md. Fails today: grep -rn 'vault' packages/aleph-artifacts/src returns 0 and no such route exists.`
- OKF output contains no Obsidian-only syntax
  <br>`With --dialect okf: grep -c '\[\[' over the exported .md files returns 0.`
- Every exported file validates against OKF v0.1
  <br>`scripts/check-okf.py over the export exits 0, including a non-empty `type` on 100% of files. Fails today: 599 of 843 live pages have no type at all.`
- The format round-trips
  <br>``render_vault(parse_vault(render_vault(x)))` is byte-identical, over BOTH dialects and including the `evidence.json` sidecar via `parse_evidence_json` — `packages/aleph-artifacts/tests/test_vault_export.py::test_export_import_export_is_byte_identical` and `tests/unit/test_vault_evidence_bundle.py::test_the_whole_bundle_including_the_sidecar_round_trips`. This is a FORMAT round trip, and saying so is the point: `parse_vault` writes no revision, no ledger row and no page, so a DATABASE importer is deliberately out of scope. `test_the_round_trip_preserves_the_governance_fields` exists because byte-identity can otherwise hold by dropping the same field on both passes.`
- The false governance claim is gone from the tree
  <br>`grep -rn 'runs on the write path' packages/ apps/ docs/wiki-schema.md CLAUDE.md returns 0 — docs/plan.md is OUT of scope, because the only hit under the old wording is line 534, this criterion quoting itself, which no amount of fixing can remove. Was 2 (docs/wiki-schema.md:16-18 and schema.py:14-16), both describing behaviour that does not exist.`
- The documented lint count matches the code
  <br>`scripts/check-lint-count.sh compares the number in docs/wiki-schema.md:34 against grep -c 'check="' lint.py. FAILS TODAY: 13 documented vs 16 actual.`

**Review.** Mutation: remove `type` from one page's frontmatter and confirm the OKF validator exits 1; point a wikilink at a title that does not exist and confirm the exporter reports a dangling link rather than emitting a broken one; add a 17th lint check and confirm the count sweep fails. Restore each. Then the manual step that is the actual claim being made: open the exported vault in Obsidian and follow three links.
<br>**Iterate.** v2 makes the export continuous — a worker that keeps a git repository of the vault in sync, so 'durable agent context' becomes a directory a human can edit and Aleph re-ingests. That is the OpenWiki shape and it is the version worth having; a one-shot zip is the stepping stone, not the destination.
<br>**Depends on:** —
<br>**Risk.** ~~The wiki is under removal per docs/decisions.md D1~~ — **CORRECTED 2026-08-22: D1 says the OPPOSITE.** The wiki and the RAG over the raw collection are two knowledge plugins and both stay; the removal decision was reversed. This paragraph cited the decision that reversed it as the reason to expect a rewrite. The mitigation it proposes is still right for its own reasons — export from a small view model rather than the ORM, so the source can be swapped without touching the format code — but the deadline it invented does not exist. Mitigate by exporting from a small view model rather than the ORM directly, so the source can be swapped without touching the format code. Second risk: OKF v0.1 is a young specification and will change;


---

### The kernel, plugins, and agent self-improvement


**Everything is a plugin, and the plugin machinery is the Aleph kernel.** That
is not aspirational — the kernel already has `register_dynamic`, `activate`,
`replace`, `deactivate` and `reprobe`, and the guardrail (blast radius: *what
else stops if I turn this off*) is fully implemented in
`AgentPluginAPI.inspect()`.

**The backlog was wrong about the important half.** It said the guardrail was
"missing: entirely". It is built and *completely unreachable* — no HTTP route,
no agent tool, no graph node calls it. Its only non-test importer in the whole
tree is `scripts/_acceptance/kernel_boot.py:77`.

So this cluster is not "build a plugin system". It is: fix two kernel bugs that
break the first agent-authored plugin (`WS-A1a`), give plugins a durable record
so they survive a restart and reach the worker (`WS-A1b`), and connect the
agent to the machinery that already exists (`WS-A2`).

**Two constraints hold throughout.** The kernel must not gain a database
dependency — `aleph-kernel` depends on exactly `aleph-core` and
`aleph-observability`, and the plugin service lives in `aleph-runtime`, the
composition root. And agent-authored code runs in `code-runner` —
cap-dropped, read-only rootfs, network-partitioned — never through `exec()` in
the FastAPI process, which is what the kernel's current skill loader does.

**One open question, and it should be decided before `WS-A1b` starts.** The
`thesis-risk` scan argues the dynamic half of the kernel — 2,120 lines, 12 test
files — has **zero production callers**, and that `aleph.toml`'s claim that the
research suite is "a plugin suite an operator may legitimately run without" is
false in code. Its recommendation is to prove that with a failing test
(`test_api_boots_without_the_research_suite`) before building more on top. That
is the cheapest possible way to find out whether the abstraction is real, and it
belongs in `docs/decisions.md` either way.

#### WS-K0 · Make the acceptance gate tell the truth, and run it in CI

**What it is.** Aleph has a script, `scripts/acceptance.sh`, that CLAUDE.md calls "the gate to trust" — it runs one named check per feature and prints pass/fail/skip. Two things are wrong with it. First, some of its checks describe something they do not actually do: the doc says check D2 proves "a skill provides a tool and shows up in the agent's tool set", but the test behind it (`packages/aleph-kernel/tests/test_skills.py:117`) only asserts a key is registered in the kernel and never touches a tool set at all;

**Why.** CLAUDE.md's opening paragraph exists because the previous version of that file asserted invariants that were false in code, and that is stated as the single reason a broken retrieval path survived seven work packages. The same failure is now present in the document CLAUDE.md points at as the source of truth. Every other workstream in this plan adds criteria to `acceptance.sh`; if the gate can lie and does not run, none of those criteria are worth anything.

**How.** Three parts, all small. (1) Correct `docs/acceptance.md` rows that overstate: A1 says "49 tests" and the suite is 142 passed / 1 skipped (I ran it: `./.venv/bin/python -m pytest packages/aleph-kernel/tests -q` → `142 passed, 1 skipped in 0.33s`); D2 and D3 get check text that matches what `test_skills.py` actually asserts, or get demoted from ✅ to the real state; D4 claims an integration test asserting "parent/child/budget rows" and `test_spawn_ledger.py` is thirteen pure in-memory unit tests with no database. (2) Add an `acceptance` job to `.github/workflows/ci.yml` running `./scripts/acceptance.sh` against the same Postgres+Redis services the existing `python-integration` job already declares (ci.yml:99-120), plus `--self-check` (which runs `scripts/_acceptance/self_check.sh` and proves the checks can go red).

**Criteria:**

- The acceptance gate runs on every push
  <br>``grep -c 'acceptance.sh' .github/workflows/ci.yml` returns ≥ 1. Today it returns 0.`
- The gate can prove it is able to fail
  <br>``./scripts/acceptance.sh --self-check` exits 0 and its output contains the self-check block; CI runs it with the same flag.`
- No acceptance row claims a test does something the test does not do
  <br>`A new `scripts/check-acceptance-claims.sh` parses each ✅ row's cited path and asserts the file exists and that every named test id in the row resolves via `pytest --collect-only -q`.`
- Every doc CLAUDE.md cites as authoritative exists
  <br>``for f in docs/architecture.md docs/acceptance.md docs/belief-engine.md docs/decisions.md docs/operations.md; do test -f $f || exit 1; done` exits 0. Fails today on `docs/decisions.md`.`
- The kernel test count in the docs matches reality
  <br>``uv run pytest packages/aleph-kernel/tests -q` tail line count matches the number quoted in `docs/acceptance.md` row A1. Today: doc says 49, actual is 142 passed / 1 skipped.`

**Review.** Mutation on the gate itself, which is the only honest way to check a gate. Break one real thing per check and confirm the corresponding row goes red: delete `blast_radius`'s call in `scripts/_acceptance/kernel_boot.py:75` and confirm A2 fails; rename a component in `catalog.json` without regenerating and confirm `check-catalog-generated.sh` fails;
<br>**Iterate.** v2 makes a SKIP expensive rather than free: the gate records how many parts were skipped on `main` and fails the build if that number grows, so "unrunnable here" stops being a place features go to hide. v3 adds a per-check timestamp of the last time each check was observed to FAIL — a check nobody has ever seen fail is the thing CLAUDE.md warns about, and the gate should be able to name its own untested checks.
<br>**Depends on:** —
<br>**Risk.** Low technically, uncomfortable politically: turning the gate on will make several currently-green parts go red or skip, and the honest response is to let them, not to soften the check. Also, adding acceptance.sh to CI lengthens the build; if it becomes slow enough that people start skipping it, the gate is worse than before.


#### WS-K1 · Close the unsupervised skill-write path before opening a governed one

**What it is.** The assistant reads four short instruction documents that tell it how to do its job — how to run research, how to write a report, how to use the ACH analysis method — stored at `apps/api/src/aleph_api/skills/*/SKILL.md`. Right now it can also *rewrite* them, silently, using its ordinary file-writing tools, on the live API container. Nothing checks, nothing logs, no one is asked. So text the agent reads from an ingested web page can in principle instruct it to edit its own standing orders, and that edit persists for the life of the container and affects everyone using it.

**Why.** Backlog H1 states the opposite — "Aleph's skills backend is a read-only host filesystem. The agent can read skills and can never author one." That is false. `deepagents.FilesystemBackend` implements `write` (filesystem.py:430) and `edit` (:472); `create_deep_agent` is called at `apps/api/src/aleph_api/copilot_agent.py:1627-1662` with no `permissions=` argument; and deepagents allows any operation no rule matches (`_check_fs_permission` returns "allow" as its default, filesystem.py:116).

**How.** Pass `permissions=` to `create_deep_agent` at `copilot_agent.py:1627`. The rule type is `FilesystemPermission(operations=[...], paths=[...], mode="deny")` (filesystem.py:84-104). Matching is first-match-wins (`_check_fs_permission`, :111-116) and both `write_file` and `edit_file` map to the single `"write"` operation (`_DEFAULT_FS_TOOL_OPS`, :74-80), so one rule — `operations=["write"], paths=["/skills/**"], mode="deny"` — closes it. Subagents inherit the parent's rules unless their spec overrides (`graph.py:584-585`), so this covers all six of Aleph's subagents in one line. Then two consumers, because a fix with no check is how this came back: `tests/unit/test_agent_skill_write_gate.py` drives the real `FilesystemMiddleware` write tool against the real `_memory_backend` from `copilot_agent.py:1600-1617` and asserts refusal;

**Criteria:**

- Writing over a bundled skill is refused, and the file on disk is unchanged
  <br>``uv run pytest tests/unit/test_agent_skill_write_gate.py -q` exits 0. The test calls the agent's own `write_file` tool with path `/skills/research/SKILL.md` and asserts (a) the tool result is a refusal and (b) the sha256 of `apps/api/src/aleph_api/skills/research/SKILL.md` is unchanged.`
- Creating a brand-new skill directory on the host filesystem is refused
  <br>`Same file, `::test_a_new_host_skill_cannot_be_created`; asserts `apps/api/src/aleph_api/skills/agent-authored/` does not exist after the call. FAILS TODAY.`
- The permission argument cannot be dropped silently
  <br>``./scripts/check-agent-fs-permissions.sh` exits 0 with the rule present and exits 1 when the `permissions=` kwarg is removed from `copilot_agent.py`.`
- Nothing under the skills directory changes across a full test run
  <br>``uv run pytest -m "not integration" -q && git status --porcelain apps/api/src/aleph_api/skills` produces no output.`
- The sweep runs in CI
  <br>``grep -c check-agent-fs-permissions .github/workflows/ci.yml` returns 1.`

**Review.** Mutation, three passes. (a) Delete the deny rule and re-run criteria 1 and 2 — both must go red. Restore. (b) Insert an `mode="allow"` rule for `/skills/**` *ahead* of the deny and confirm criterion 1 goes red — this proves the check is reading the effective ordering rather than merely the presence of a `permissions=` kwarg. Restore.
<br>**Iterate.** v2 turns the blanket deny into the governed path: writes are permitted into `/skills/authored/**` only, and every such write appends an `ActionLedgerEvent` — that is workstream H1. v3 widens the sweep from one call site to a rule: no new `FilesystemBackend(` rooted at any directory under `apps/` may appear anywhere, because the underlying mistake was pointing a read-write backend at the application's own source tree,…
<br>**Depends on:** —
<br>**Risk.** `FilesystemMiddleware.__init__` raises `NotImplementedError` when permissions are combined with an execution-capable backend whose rule paths fall outside a CompositeBackend route (filesystem.py:690-702). `"/skills/**"` starts with the existing `/skills/` route prefix so `_all_paths_scoped_to_routes` returns True today, but if a future backend gains execution support this becom…


#### WS-A1a · Fix the two kernel bugs that break the first agent-authored plugin

**What it is.** The kernel — Aleph's own machinery for adding and removing abilities while the system runs — already works, and it already refuses to remove something other parts are standing on. But two bugs make the runtime path unusable the first time an agent actually drives it. First: if a plugin fails to start, its declaration is left behind with no way to remove it, and because every later start-up re-validates the whole set, that one bad leftover makes every subsequent install fail too, for the life of the process.

**Why.** These are not edge cases. They are literally the first two things that happen when an agent writes a plugin: the first attempt is wrong, and the second attempt is version two of the same name. Until they are fixed, "an agent that authors plugins for itself" fails on attempt one and stays failed until someone restarts the API. Neither is covered by any of the 142 kernel tests.

**How.** Four changes in `packages/aleph-kernel/`. (1) Add `Kernel.unregister(name)` beside `_register` (kernel.py:100-113): refuses when the capability is ACTIVE, refuses when `spec.protected`, otherwise drops the `_mounted` entry — the inverse living next to the thing it undoes, which is the kernel's own stated rule. (2) `AgentPluginAPI.install` (agent_api.py:110-126) calls `unregister` in both failure branches, so a refused install leaves the graph exactly as it found it. (3) `AgentPluginAPI.disable` (agent_api.py:167-196) returns the `plugin_id` it was handed rather than `None`, and gains an `unregister: bool = False` flag so the agent can pick "stopped but still installed" versus "gone". I verified `_teardown` (kernel.py:348-354) leaves `plugin_id` intact, so `replace` still works on a disabled plugin — the handle exists, the agent is just never given it back.

**Criteria:**

- A failed install leaves nothing behind
  <br>``uv run pytest packages/aleph-kernel/tests/test_agent_api.py::test_a_failed_install_leaves_no_ghost -q` exits 0. The test installs a spec requiring an unprovided key, asserts refusal, installs an unrelated valid spec and asserts `installed is True`, then asserts `await kernel.boot()` does not raise.`
- An agent can ship version two of its own plugin
  <br>``::test_an_agent_can_ship_a_second_version` — install "mine", disable, install "mine" again, assert `installed is True`. FAILS TODAY — returns `"'mine' is already registered"`.`
- Disable hands back a handle the agent can use
  <br>``::test_disable_returns_the_id_it_was_given` asserts `outcome.plugin_id == str(pid)` and that a subsequent `await api.replace(pid, better_spec)` succeeds. FAILS TODAY — `disable` returns `plugin_id=None` at agent_api.py:189-196.`
- `unregister` cannot reach core capability
  <br>``::test_unregister_refuses_a_protected_capability` and `::test_unregister_refuses_an_active_capability` both pass; the protected check must be the first statement in the method.`
- The agent surface is importable from the package root
  <br>``uv run python -c "from aleph_kernel import AgentPluginAPI, CapabilityView, InstallOutcome"` exits 0. FAILS TODAY.`
- The suite grows and stays green under strict typing
  <br>``uv run pytest packages/aleph-kernel/tests -q` reports ≥ 148 passed (today: 142 passed, 1 skipped) and `uv run pyright` reports 0 errors.`

**Review.** Mutation, four passes, each restoring after. (a) Remove the `unregister` call from `install`'s except branch → criterion 1 red. (b) Return `plugin_id=None` from `disable` again → criterion 3 red. (c) Let `unregister` accept an ACTIVE capability → criterion 4's second test red. (d) Move the `protected` check below the `_mounted.pop` → criterion 4's first test red.
<br>**Iterate.** v2 adds `Kernel.quarantine(plugin_id)`: a plugin that fails `check_health` twice moves to a FAILED-and-unregistered state with the reason retained, and `inspect()` reports it — so the agent can see its own graveyard instead of reinstalling the same broken thing in a loop.
<br>**Depends on:** —
<br>**Risk.** `unregister` takes a `name`, not a `PluginId`. The kernel's primary guardrail is that core capability has no addressable id — "deactivating core capability is not refused, it is unexpressible" (kernel.py:41-49). A name-addressed method sidesteps that, so the `protected` check inside `unregister` is the only defence and it must ship with its own test.


#### WS-A1b · A plugin becomes a durable record with a real loader — and `aleph_kernel.skills` stops being dead code

**What it is.** Today a plugin exists only as a live Python object inside one running process. Restart the API and it is gone; the background worker never had it. There is no plugin table anywhere in the schema — I counted 61 `__tablename__` declarations and none of them is plugin-, skill- or capability-related. There is no version, no configuration schema, no record of who installed it.

**Why.** This is the missing half of "everything is a plugin". Without it, an agent that improves itself forgets the improvement at the next deploy and the worker process never learns it at all. It also settles backlog H2 honestly. H2 frames `aleph_kernel/skills.py` as dead code to "wire or delete" — it is neither.

**How.** A new `plugins` table in `aleph-db`: `id`, `project_id`, `name`, `major_version`, `source_kind` (`skill` | `capability`), `instructions`, `code`, `provides`/`requires` (jsonb), `config_schema` (jsonb), `state`, `installed_by`, plus `created_at`/`updated_at`/`created_by` per the project-scope rule (NOT `access_scope` — deleted, `decisions.md` D7). Alembic revision under `apps/api/alembic/versions/` following the existing `YYYYMMDD_HHMM_<slug>_<message>.py` naming. The service goes in `packages/aleph-runtime` (the composition root, which already depends on aleph-db and aleph-kernel); the kernel itself must NOT gain a database dependency — `packages/aleph-kernel/pyproject.toml` lists exactly `aleph-core` and `aleph-observability` and that stays true.

**Criteria:**

- A plugin survives a process restart
  <br>``uv run pytest -m integration tests/integration/test_plugin_durability.py::test_an_installed_plugin_comes_back_after_a_restart -q`: install through `PluginService`, discard the kernel, build a fresh `Kernel`, run boot + reconstitution, assert `kernel.is_provided("skill.literature-review")`.`
- Every install writes a ledger row in the same transaction
  <br>``::test_install_writes_an_action_ledger_event` asserts exactly one `action_ledger_events` row with `action_kind == "plugin.install"` and `target_id == plugin.id`, and that forcing a rollback after the kernel call leaves zero rows and zero active capabilities.`
- Source with an import-time side effect is refused and leaves no row
  <br>``::test_a_gated_plugin_leaves_no_row` feeds a `kernel.py` whose top level calls `open(...)`; asserts `SkillRejected` and that the `plugins` row count is unchanged. The AST gate forbids top-level calls outside a 16-name allowlist (ast_gate.py:40-57).`
- The kernel still has no database dependency
  <br>``uv run python -c "import tomllib,pathlib; d=tomllib.loads(pathlib.Path('packages/aleph-kernel/pyproject.toml').read_text()); assert sorted(d['project']['dependencies'])==['aleph-core','aleph-observability']"` exits 0.`
- There is no second declaration of the core capability set
  <br>``grep -rn "def core_capabilities" apps packages scripts | wc -l` returns exactly 1, in `packages/aleph-runtime/src/aleph_runtime/capabilities.py`. Not 0 over the bare name — the composition root has to declare the set somewhere, so the old form was red forever and said nothing about whether a SECOND declaration had appeared, which is the property.`
- The schema and the models agree
  <br>``cd apps/api && uv run alembic check` produces no diff; `uv run pyright` reports 0 errors.`
- The workspace package count does not grow
  <br>``./scripts/acceptance.sh --quick` E4 still passes — this work adds a table and a module, not a 22nd package.`

**Review.** Mutation. (a) Comment out the install call inside the reconstitution loop → criterion 1 red. (b) Move the ledger append outside the transaction and force a rollback → criterion 2 red. (c) Replace `skill_from_source` with a bare `exec` → criterion 3 red, which is what proves the gate is on the real path and not merely present in the tree.
<br>**Iterate.** v2 removes the restart requirement for the worker: a Redis pub/sub channel the worker subscribes to, calling the same `PluginService.install`, so a plugin installed in the API is live in the worker within seconds rather than at next deploy.
<br>**Depends on:** WS-A1a
<br>**Risk.** The highest risk in this plan, and it must be decided before the work starts rather than during. `skill_from_source` calls `exec` on agent-authored code inside the API process (skills.py:117), and the module's own docstring is honest that the gate secures *loading*, not *execution* (skills.py:23-28) — once a helper is called it runs with the API process's full authority, includ…


#### WS-A2 · The agent can actually reach the kernel — the guardrail gets its first caller

**What it is.** The guardrail everyone describes as the product — the agent may add abilities but cannot remove the ones the system is standing on — is fully built and completely unreachable. There is no HTTP route, no agent tool and no graph node that constructs or calls it. This workstream gives the assistant five tools and an operator five HTTP routes that reach the kernel through the plugin service, so "write a plugin, see what turning it off would break, turn it off" becomes something a person can actually do in the app.

**Why.** CLAUDE.md's first substantive line is: "an agent that authors plugins for itself and activates or deactivates them as needed… The kernel is the product." The kernel exists. The product does not, because the agent-facing surface has one non-test importer and it is `scripts/_acceptance/kernel_boot.py:77`. Backlog A2 says the guardrail is "missing: entirely" — that half is wrong, and it is the important half.

**How.** Five `@tool` functions in `apps/api/src/aleph_api/copilot_agent.py` alongside the existing twelve (`search_wiki` at :378 through `spotlight` at :990), each resolving project scope through the existing `_authorized` helper (:301) so they inherit the agent-scope defence already in place: `list_capabilities`, `preview_removal`, `author_plugin`, `disable_plugin`, `replace_plugin`. `preview_removal` returns `AgentPluginAPI.inspect()`'s `would_also_stop` verbatim — the blast radius is a pure function over the declaration graph (`support.py:93-112`), so showing it costs nothing and changes nothing, and the kernel's own docstring is right that a refusal the agent cannot predict is indistinguishable from a broken tool. A new `apps/api/src/aleph_api/routes/plugins.py` mirrors the tools for an operator, using `ProjectScopeDep` like every other route.

**Criteria:**

- The agent surface has a caller in product code
  <br>``grep -rn "AgentPluginAPI(" apps/api/src | wc -l` returns ≥ 1. Today it returns 0. The open paren is load-bearing: without it the grep counts the docstrings that describe the fix, so it passes whether or not anything constructs the class.`
- The kernel on app state is read, not just written
  <br>``grep -rn 'getattr(request.app.state, "kernel"' apps/api/src | wc -l` returns ≥ 1 — a READ, outside `lifespan.py`. Counting `state.kernel` counts the write too, so it was ≥ 2 the moment one route mentioned it. `getattr` with a default is deliberate: a route must answer 503 when the kernel is not mounted, not `AttributeError`. Today: 3, in `routes/plugins.py`.`
- A refusal is predictable from the preview
  <br>``uv run pytest -m integration tests/integration/test_plugin_routes.py::test_preview_matches_the_refusal -q`: install A, install B requiring A, assert `preview_removal(A)` names exactly {B}, then assert `disable(A)` is refused naming exactly {B}. FAILS TODAY — there is no route and no tool.`
- Core capability is not addressable over HTTP
  <br>``::test_no_core_capability_is_addressable_over_http` asserts `GET /v1/projects/{id}/plugins` returns `plugin_id: null`, `removable: false` and `trust: "core"` for every one of the ten capabilities in `apps/api/aleph.toml`; `::test_a_fabricated_plugin_id_is_unaddressable_not_merely_refused` asserts `DELETE` and the removal preview both return 404 carrying the "mounted from the boot manifest" message from agent_api.py:198-206.`
- Authoring a plugin requires more than project scope
  <br>``::test_authoring_over_http_is_refused_for_a_member_who_is_not_owner` holds a real `ProjectRole.EDITOR` principal — one that passes `project_scope_dep` — and asserts 403 on `POST /plugins` while a VIEWER still gets 200 on `GET`, so the refusal is about the role and not about project access. `::test_authoring_requires_owner_not_merely_project_access` is the second witness below HTTP.`
- The plugins pane is reachable and the client did not hardcode it
  <br>``PANE_REGISTRY` includes `settings`, whose surface carries a `plugins` section listing every `UIContribution` with its trust (`routes/surfaces.py:541`), and `./scripts/check-pane-registry.sh` exits 0. There is no top-level `plugins` pane and there is not meant to be: WS-B1 made settings a pane and plugin settings a section inside it, so the original wording asks for a screen the design deliberately does not have.`
- Every mutating route writes a ledger row
  <br>``::test_disable_over_http_writes_a_ledger_row` asserts one `action_ledger_events` row with `action_kind == "plugin.disable"`.`

**Review.** Mutation. (a) Make `preview_removal` return an empty list unconditionally → criterion 3 red. (b) Make `inspect()` hand out a `plugin_id` for protected specs → criterion 4 red *and* `scripts/_acceptance/kernel_boot.py` goes red independently, since it already asserts zero manifest capabilities are agent-addressable (:77-82) — two witnesses for the guardrail is the point.
<br>**Iterate.** v2 makes the refusal actionable rather than merely correct: instead of "refused, would also stop X and Y", return the ordered plan the agent could execute, since `deactivate` already computes the teardown order (kernel.py:342-344). v3 renders the blast radius as an A2UI surface in the plugins pane — the kernel already computes the graph, so this is a projection, not new logic.
<br>**Depends on:** WS-A1b
<br>**Risk.** An agent tool that installs code is a privilege the assistant did not previously have, reached over a chat endpoint. The existing `middleware/agent_scope.py` and `_authorized` bound *which project* a request may touch, not *what authority* it carries.


#### WS-H1 · Agent-authored skills that survive the session

**What it is.** A "skill" here is a short instruction document, optionally with helper code, that teaches the assistant how to do one kind of job. Four ship with Aleph. When the assistant works out a better way to do something, there is nowhere durable to put it: anything it writes lands in the container's own source tree, disappears at the next deploy, is invisible to the background workers, and — because of how the library caches the skill list per conversation — is not even visible in the conversation that wrote it.

**Why.** This is the smallest complete instance of the self-improvement loop: the agent learns something, writes it down, and finds it there tomorrow. It is also the cheapest. I verified against the installed library that `StoreBackend` and `SkillsMiddleware` already work together with no library modification, on both the sync and the async path — `_list_skills_with_errors` and `_alist_skills_with_errors` both returned the authored skill from a store-backed route.

**How.** Add a second route to the `CompositeBackend` at `copilot_agent.py:1610-1617`: `"/skills/authored/": StoreBackend(namespace=_authored_namespace)`, reusing the per-project namespace pattern of `_memory_namespace` (:1567-1590) — note the namespace must be a *callable*, not a tuple, or `_get_namespace` raises. `CompositeBackend` sorts routes longest-prefix-first (`backends/composite.py:162-163`), so `/skills/authored/` nests correctly inside `/skills/`. Pass **two** sources: `skills=["/skills", "/skills/authored"]` at :1657. I verified this is required, not optional — measured through the composite, `/skills/` returns `['ach','report-authoring','research','wiki-style']` and never the store's, while `/skills/authored/` returns the authored one.

**Criteria:**

- A skill written in one conversation is visible in another
  <br>``uv run pytest -m integration tests/integration/test_authored_skills.py::test_an_authored_skill_survives_the_thread -q`: write through the composite in thread A, build fresh state for thread B, assert `await _alist_skills_with_errors(backend, "/skills/authored")` contains it. FAILS TODAY.`
- The authoring conversation sees its own skill without starting a new one
  <br>``::test_the_authoring_session_sees_its_own_skill` asserts `skills_metadata` after the write contains the new name. FAILS TODAY — the early return at skills.py:959-961 means the list is loaded once per thread and never rescanned.`
- Only the authored path is writable; the bundled four remain untouchable
  <br>`K1's criterion 1 still passes with the new allow rule in place, and `::test_the_authored_route_is_writable` passes — both in the same run, which is what proves the ordering is right rather than the deny being dropped.`
- Authored skills are project-scoped
  <br>``::test_project_a_cannot_read_project_bs_authored_skill` asserts the skill list for project B excludes project A's authored skill.`
- Nothing blocks the FastAPI event loop
  <br>``::test_the_async_path_is_used_so_nothing_blocks_the_event_loop` records `threading.get_ident()` inside the routed backend's sync `ls`/`download_files`, asserts both really ran, and asserts neither ran on the loop thread. NOT `abefore_agent`: that hook belongs to `rubric.py`, and `authored_skills.py` has none — the refresh happens in `awrap_tool_call`, the only moment the authoring turn can use what it just wrote.`
- Every authored skill has a ledger row
  <br>``::test_every_authored_write_is_ledgered` asserts the count of `action_ledger_events` rows with `action_kind='skill.author'` equals the number of authored `SKILL.md` keys in the store for that project.`

**Review.** Mutation. (a) Drop `/skills/authored` from the `skills=` list, leaving one source → criterion 1 red. This is the specific failure the "two sources, not one" correction exists to catch, and it is invisible without a test. (b) Point `_authored_namespace` at a constant tuple instead of the per-project callable → criterion 4 red. (c) Remove the metadata-clearing middleware → criterion 2 red.
<br>**Iterate.** v2 adds the review gate that arguably belongs in v1: an authored skill is written in `state="proposed"` and enters the agent's own metadata only after an operator approves it in the plugins pane, reusing the ApprovalCard flow that already exists.
<br>**Depends on:** WS-K1, WS-A1b
<br>**Risk.** An authored skill is an instruction the model will follow, and its content can originate in a document the agent ingested. The AST gate covers `kernel.py` and cannot inspect prose — skills.py:23-28 says so plainly.


#### WS-A3a · Tell the agent what it can actually draw — and give the settings-card generator a caller

**What it is.** The list of screen pieces the assistant is allowed to ask for, and the list the browser can actually draw, are two different lists and nobody noticed. The browser can draw 39 things. The assistant is told about 19 — I counted both. Every single input control is in the first list and missing from the second: text boxes, checkboxes, dropdowns, sliders, date pickers. So the assistant has never been able to ask for a form, and no plugin can declare a settings screen.

**Why.** A plugin that cannot declare a settings screen is a library, not a plugin. And Aleph already contains a finished settings-screen generator — `packages/aleph-a2ui/src/aleph_a2ui/settings_card.py`, whose docstring says it exists so "a plugin an agent wrote a minute ago gets a settings screen on the same terms as a core one". It has no callers outside its own tests and it cannot work as written.

**How.** Derive the agent-facing catalog from the *rendering* catalog rather than hand-listing it. `extractCatalogComponentSchemas` is exported from the installed `@copilotkit/a2ui-renderer@1.58.0` — I verified it in `dist/index.d.mts` and `dist/index.mjs` — and emits the correct A2UI v0.9 inline shape from the zod schemas that actually render. Replace `render_runtime_catalog` in `scripts/gen_catalog.py:88-112`, which today filters to the ten components carrying an `agent` block (:91-102) plus nine primitives, with a small Node step that imports `buildAlephCatalog()` from `apps/web/src/a2ui/aleph-catalog-v09.tsx:589-595` and extracts. `scripts/check-catalog-generated.sh` keeps working unchanged because it just regenerates and diffs.

**Criteria:**

- The agent knows every input control exists
  <br>`A Node assertion over `apps/copilot-runtime/src/catalog.generated.ts` that its component set is a superset of `{TextField, CheckBox, ChoicePicker, Slider, DateTimeInput}`. Today the file parses to 19 components and the intersection with that set is empty — I measured it. FAILS TODAY.`
- The agent-facing list and the renderable list do not disagree
  <br>`A new `scripts/check-agent-catalog-covers-renderer.sh` asserting the renderable set minus the agent-facing set is empty. Today: 39 renderable versus 19 agent-facing, a gap of 20.`
- A generated settings surface validates
  <br>``uv run pytest packages/aleph-a2ui/tests/test_settings_card.py::test_every_emitted_component_is_in_the_catalog -q` — runs `settings_components` over a schema exercising every supported field type and calls `validate_component` on each result. FAILS TODAY: five component types are rejected.`
- The save action has a registered handler
  <br>``uv run python -c "from aleph_api.a2ui_handlers import build_action_router; assert 'plugin.settings.save' in build_action_router().registered_actions()"` exits 0. FAILS TODAY — `register` raises because the action is not in the catalog.`
- `settings_card.py` has a caller in product code
  <br>``grep -rn "from aleph_a2ui.settings_card import" apps/api/src | wc -l` returns ≥ 1. Today it returns 0. Not a grep for `settings_surface`, which also matches the unrelated `settings_surface_v09` already imported by `routes/surfaces.py` — that check is green today and would stay green if this workstream were never done.`
- The generated catalogs and the web build stay consistent
  <br>``./scripts/check-catalog-generated.sh`, `./scripts/check-surface-bindings.sh` and `pnpm -C apps/web build` all exit 0.`

**Review.** Mutation. (a) Remove `TextField` from `ALEPH_CARD_IMPLS`'s effective set in the render catalog → criteria 1 and 2 go red from opposite directions, which is what proves they are independent checks rather than the same check twice. (b) Unregister `plugin.settings.save` → criterion 4 red. (c) Hand-edit `catalog.generated.ts` → `check-catalog-generated.sh` red.
<br>**Iterate.** v2 moves delivery from build-time to run-time: the browser provider already pushes its catalog schema as run context under `A2UI_SCHEMA_CONTEXT_DESCRIPTION`, which is the "bring your own catalog" direction the stack intends, and taking it deletes `apps/copilot-runtime/src/catalog.generated.ts` and half of `check-catalog-generated.sh`.
<br>**Depends on:** —
<br>**Risk.** `extractCatalogComponentSchemas` needs the live catalog object, which means a build-time script has to import a `.tsx` module that pulls in React components. If that import chain proves unrunnable outside Vite, the fallback is a Vitest-driven extraction step, or skipping straight to the run-time delivery described in the iteration step.


#### WS-A3b · One catalog per plugin, merged without collisions

**What it is.** If two plugins both define something called "Chart", one silently replaces the other and nothing anywhere reports it — the underlying code is a plain map assignment. The drawing protocol Aleph already uses has the answer built in: a client can hold several *named* catalogs at once, and every screen says which one it belongs to. Aleph is already on that code path and passes a list of exactly one, in three places.

**Why.** This is A3's stated goal and it is the isolation boundary that makes agent-authored UI safe to offer at all. It also closes the loop back to the kernel, which is where Aleph is genuinely ahead of the stack it sits on: a catalog becomes a capability declaring `provides = {"ui:catalog:aleph://plugin/<name>@1"}`, and a pinned pane declares `requires` on it.

**How.** Naming convention: `aleph://core@1` for the human-owned set, `aleph://plugin/<name>@<major>` per plugin with contents = core ∪ its own. Putting the major in the id means `@1` and `@2` are different strings, therefore different catalogs, therefore they coexist in the same processor array with no migration and surfaces created before an upgrade keep painting — free version tolerance for the cost of one naming rule. Add a collision check at assembly: refuse a plugin whose component or function names intersect core's, naming both sides. It is about fifteen lines and it converts a silent map overwrite into a rejected install. Serve the array from a new `GET /v1/projects/{id}/catalogs` driven by A1b's enabled plugin rows.

**Criteria:**

- Two plugins may both define a component called Chart
  <br>``uv run pytest packages/aleph-a2ui/tests/test_plugin_catalogs.py::test_two_plugins_may_share_a_component_name -q`. FAILS TODAY — one catalog, one namespace.`
- A plugin that would shadow a core component is refused, naming both sides
  <br>``::test_a_plugin_cannot_shadow_a_core_component` asserts the refusal message contains both the plugin name and the core component name.`
- The renderer is fed the real list
  <br>``grep -rn "new MessageProcessor(\[catalog\])" apps/web/src | wc -l` returns 0. Today it returns 3.`
- The rewritten sweep still catches the original defect and the new one
  <br>``./scripts/check-single-catalog.sh` exits 0 on the tree; exits 1 when `basicCatalog.functions.values()` is removed from the builder (the original chat-only failure); and exits 1 when two catalogs are declared with the same id (the new failure mode). Three runs, three outcomes.`
- Disabling a plugin a pinned pane depends on is refused
  <br>``uv run pytest -m integration tests/integration/test_catalog_capability.py::test_a_pinned_pane_protects_its_catalog -q`: pin a pane whose surfaces name `aleph://plugin/x@1`, then `disable(x)` raises `DependentsWouldBreak` naming the pane. FAILS TODAY.`
- The web app still builds and lints clean
  <br>``pnpm -C apps/web build` and `pnpm -C apps/web lint` both exit 0.`

**Review.** Mutation. (a) Remove the collision check → criterion 2 red. (b) Give two plugin catalogs the same id → criterion 4's second clause red. (c) Drop the pane's `requires` declaration → criterion 5 red, and this is the important one, because it proves the kernel is genuinely reading the declaration graph rather than the test asserting a constant. Restore each.
<br>**Iterate.** v2 adds a snapshot test pinning each published catalog's component names and prop types within a major version, so removing or re-typing a component forces a `@2` rather than silently breaking every surface created before the change — the stability rule that nothing currently enforces.
<br>**Depends on:** WS-A1b, WS-A3a
<br>**Risk.** The chat renderer accepts one catalog, so per-plugin isolation in chat is a merge — and a merge is precisely where the silent-overwrite hazard lives. The collision check must run on the merged chat catalog too, not only on the per-pane catalogs, or the isolation guarantee holds in panes and quietly fails in chat.


#### WS-H3 · The agent grades its own work before handing it over

**What it is.** Today the assistant decides for itself when an answer is finished. `RubricMiddleware` lets you write down what "finished" means as a list of named criteria; a second model call grades the answer against them, and if a criterion fails the agent revises and tries again, up to a cap. The library ships it, Aleph has the version that has it (0.6.6 installed, `apps/api/pyproject.toml:22` pins `>=0.6,<0.7`), and it does nothing at all unless something puts a rubric onto the run.

**Why.** The self-improving harness thesis is empty if the agent cannot tell whether an improvement improved anything. This is the smallest closed loop available: declare the standard, measure against it, revise. It also completes H1 — a skill that declares its own rubric is a skill whose value is measurable rather than asserted, which is the difference between the agent accumulating instructions and the agent getting better.

**How.** The middleware is the easy part; the plumbing is the work. The graph is compiled once at start-up (`copilotkit_endpoint.py:50`, called from lifespan) and the Node bridge constructs `new HttpAgent({ url: AGENT_URL })` with no state channel (`apps/copilot-runtime/src/server.ts:44`), so nothing carries a rubric from the browser to the graph. Solve it server-side, which needs no AG-UI change and is directly testable: add an Aleph middleware ahead of `RubricMiddleware` in the `middleware=[...]` list at `copilot_agent.py:1658`, whose `abefore_agent` resolves the rubric for this turn — from the project's configured rubric, or from the skill the agent just activated — and returns `{"rubric": ...}`. deepagents appends the caller's `middleware` list in order (`graph.py:750-751`), so ordering within that list is ours;

**Criteria:**

- A rubric reaches the graph with no browser change
  <br>``uv run pytest apps/api/tests/unit/test_rubric_grading.py::test_a_configured_rubric_lands_on_state -q` (**CORRECTED 2026-08-22:** the plan pointed at `tests/integration/test_rubric.py`, where that node id does not exist — only criterion 4's cost assertion lives in the integration file, and pytest cannot collect the id as written)`: invoke the compiled graph with no rubric in the input and assert `agent.get_state(config).values["rubric"]` equals the project's configured rubric. FAILS TODAY.`
- A failing criterion causes exactly one revision
  <br>``::test_a_failing_criterion_triggers_one_revision` with a stubbed grader returning `needs_revision` once then `satisfied`; asserts two agent turns and `_rubric_status == "satisfied"`.`
- Hitting the cap is reported, not mistaken for success
  <br>``::test_max_iterations_terminates_and_reports` asserts `_rubric_status == "max_iterations_reached"` and that the library's `logger.warning` fired — the middleware deliberately does not mutate the response on a non-satisfied termination (rubric.py:300-318), so silence here would read as success.`
- The grader's spend is recorded
  <br>``::test_grader_calls_write_a_model_call` asserts the count of `model_calls` rows with `purpose == "assistant.rubric.grader"` equals the number of grader invocations. FAILS TODAY — the grader model bypasses `_gateway_chat_model` entirely, so its calls are uncosted.`
- With no rubric the middleware is inert
  <br>``::test_no_rubric_means_no_grader_call` asserts zero grader invocations and byte-identical output against the pre-change graph on the same input.`
- Gateway call volume is bounded and countable
  <br>``::test_a_turn_issues_at_most_three_grader_calls` with `max_iterations=2`, counted on a stub.`

**Review.** Mutation. (a) Move Aleph's rubric-source middleware *after* `RubricMiddleware` in the list → criterion 1 must go red. This is the one that matters, because it converts the hook-ordering assumption into something proven rather than believed. (b) Construct `RubricMiddleware(max_iterations=21)` → must raise `ValueError` from the library's own cap check (rubric.py:365-370).
<br>**Iterate.** v2 lets a plugin or a skill declare its own rubric, so the standard travels with the ability instead of being a single global setting — that is what makes H1's authored skills evaluable at all. v3 records every `RubricEvaluation` as rows via the library's `on_evaluation` callback (rubric.py:337-345), turning "is the agent getting better at this task" from an impression into a query.
<br>**Depends on:** WS-A1b
<br>**Risk.** `RubricMiddleware` is marked `@beta` (rubric.py:296), so its API can move under us. The concrete operational risk is larger: each iteration is a full agent turn, so a rubric the grader can never satisfy turns one user message into `max_iterations + 1` complete turns plus grader calls. Backlog E5 already reports unexplained gateway rate limiting attributed to subagent fan-out.


---

### The agent path: inspector, streaming, interpreters, async subagents (C3, H7, H5, H6, E1, D2)


#### WS-E1a · Aleph owns the agent's stream endpoint, so a failed run says so

**What it is.** Today, when the assistant breaks mid-answer, the connection just goes quiet. No error is sent to the browser. The chat shows a half-written message and stops, and then the browser invents its own error — that is the confusing 'The run has already errored with RUN_ERROR' message in the console. The cause is 40 lines of third-party code that turns the agent into a web stream and has literally no error handling.

**Why.** Nothing else in this cluster can be built while the primary failure is invisible. An Inspector that shows tool calls but cannot show why the run died would be a prettier version of the current problem. It is also the single cheapest prod-readiness fix on the agent path: the owner's live-stack defect E1 has been untraceable for a reason — the diagnostic content of every agent failure currently exists only in the aleph-api container's stderr, and the browser is shown a fabrication.

**How.** Replace the call to `ag_ui_langgraph.add_langgraph_fastapi_endpoint` in /Users/jpmullins/Documents/code/aleph/apps/api/src/aleph_api/copilotkit_endpoint.py:45-62 with an Aleph-owned FastAPI route. The thing being replaced is small and fully readable: .venv/lib/python3.13/site-packages/ag_ui_langgraph/endpoint.py:9-32 is the entire implementation — `agent.clone()`, then `async for event in request_agent.run(input): yield encoder.encode(event)`. Aleph's version keeps `clone()` and `EventEncoder` and adds three things the upstream lacks. (1) An `except Exception` around the `async for`, emitting `RunErrorEvent(type=EventType.RUN_ERROR, message=...)` as the final frame — this is what `LangGraphAgent.run()` cannot do, because it has exactly one `try:` (agent.py:193) and one `finally:` (agent.py:451) and no `except` between them.

**Criteria:**

- A graph that raises produces a RUN_ERROR as the last SSE frame instead of a truncated body. FAILS TODAY — there is no `except` anywhere in `LangGraphAgent.run()`.
  <br>``uv run pytest apps/api/tests/unit/test_agent_endpoint_errors.py::test_graph_exception_yields_run_error -q` — POSTs an AG-UI RunAgentInput at a route mounted over a stub graph whose node raises RuntimeError; parses the SSE body and asserts the final `data:` frame has `"type": "RUN_ERROR"`.`
- No event is ever emitted after a terminal event, in either direction. FAILS TODAY — upstream falls through from RUN_ERROR to RUN_FINISHED.
  <br>``uv run pytest apps/api/tests/unit/test_agent_endpoint_errors.py::test_no_events_after_terminal -q` — drives the encoder with a synthetic generator that yields RUN_ERROR then TOOL_CALL_START then RUN_FINISHED; asserts exactly one of {RUN_ERROR, RUN_FINISHED} appears in the body and it is the last frame.`
- The upstream endpoint helper has no call sites left in Aleph.
  <br>``grep -rn 'add_langgraph_fastapi_endpoint(' apps/ packages/ | wc -l` returns 0 — a CALL, note the paren. **CORRECTED 2026-08-22:** without it the grep counts Aleph's own docstrings, which name the helper to explain why it is not used. It returns 3 mentions and **0 calls**, so the criterion is met and the command said otherwise. A grep that forbids naming the thing you replaced can only be satisfied by deleting the explanation.
- The failure the browser is shown and the traceback in the log carry the same searchable id.
  <br>``uv run pytest apps/api/tests/unit/test_agent_endpoint_errors.py::test_run_id_links_error_to_log -q` — uses `caplog`; extracts the uuid out of the RUN_ERROR message and asserts the same string appears in a captured ERROR record and in the `X-Aleph-Run-Id` header.`
- The quality gates stay clean.
  <br>``uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest apps/api/tests/unit -q` all exit 0.`

**Review.** Mutation testing in both directions. (a) Delete the `except Exception` block from the new route and confirm `test_graph_exception_yields_run_error` fails; restore. (b) Delete the terminal latch and confirm `test_no_events_after_terminal` fails; restore.
<br>**Iterate.** Second pass classifies the failure instead of just reporting it: map exception types to stable codes (`PermissionDenied` → `forbidden`, `httpx.TimeoutException` → `upstream_timeout`, `openai.RateLimitError` → `rate_limited`, anything else → `internal`) and put the code alongside the message in the RUN_ERROR payload.
<br>**Depends on:** —
<br>**Risk.** Low, and bounded. The risk is behavioural drift from upstream: if `ag_ui_langgraph` changes `LangGraphAgent.run()`'s contract, Aleph now owns the wrapper and must follow. Mitigated by keeping the wrapper to the three additions above and continuing to call `agent.run()` unchanged — Aleph owns the envelope, never the event translation.


#### WS-E1b · A tool failure becomes a message the agent can read, not a dead conversation

**What it is.** The assistant has 27 tools. If any one of them throws — a permission check, a database hiccup, a missing dictionary key, a 404 from a page that does not exist — the entire conversation dies on the spot. That is not how agents are supposed to work: a normal agent reads the error, says 'that did not work, let me try another way', and keeps going. Ours cannot, because it never gets to see the error. The rule causing this is LangChain's default: re-raise anything that is not a schema-validation error.

**Why.** This is the primary mechanism behind E1, and it is a structural defect rather than a bug in one place: 6 of the 11 orchestrator tools contain no `try:` at all, and every single tool — guarded or not — calls `_project_id_from_config` OUTSIDE its try block, which reaches `require_project_access` and can raise `PermissionDenied` (copilot_agent.py:301-336, 339-374).

**How.** New module `apps/api/src/aleph_api/agent_middleware.py` defining `AlephAgentMiddleware(AgentMiddleware)` implementing `awrap_tool_call` — verified available at .venv/.../langchain/agents/middleware/types.py:744-810, with `ToolCallRequest` carrying `tool_call` (name/args/id) and `runtime` (langgraph/prebuilt/tool_node.py:133-147). On exception it returns `ToolMessage(content=<one-line description>, tool_call_id=request.tool_call['id'], status='error')` instead of re-raising, which is exactly what ToolNode's default handler refuses to do (langgraph/prebuilt/tool_node.py:383-392, wired with no `handle_tool_errors` at langchain/agents/factory.py:1061-1064).

**Criteria:**

- A tool that raises PermissionDenied yields a tool message and the run still finishes. FAILS TODAY.
  <br>``uv run pytest apps/api/tests/unit/test_agent_tool_guard.py::test_permission_denied_becomes_tool_message -q` — invokes a deep agent built with one tool that raises `PermissionDenied`; asserts the resulting message list contains a `ToolMessage` with `status == 'error'` and that no exception escaped `ainvoke`.`
- Every subagent carries the guard, enforced by a sweep rather than by memory.
  <br>``./scripts/check-agent-middleware.sh` exits 0 AND `apps/api/tests/unit/test_agent_tool_guard.py::test_every_subagent_spec_really_carries_the_guard` passes. Both, because for a while neither was enough: the sweep asserted `AlephAgentMiddleware` in the `create_deep_agent` `middleware=` list (a real AST walk) and then decided the SUBAGENT half by counting `'"middleware"'` in the module TEXT. Measured 2026-08-22 — empty the retriever's list to `"middleware": []`, leave the import and the comment alone, and the sweep stayed rc=0 with every tool that subagent carries one exception away from ending the turn. The sweep now walks each `build_*_subagent`'s own AST for the value bound to the `"middleware"` key; the test BUILDS every subagent (discovered by `pkgutil`, so a seventh is covered the day it lands) and asserts an instance is in the returned spec. No arrangement of imports or comments satisfies either.`
- No agent tool is left relying on its own try/except for survival.
  <br>``uv run pytest apps/api/tests/unit/test_agent_tool_guard.py::test_every_registered_tool_is_wrapped -q` — builds the real graph via `build_assistant_deep_agent` with stub runtime, enumerates `tool_node.tools_by_name`, and asserts each one, when made to raise via monkeypatch, returns a ToolMessage.`
- The browser's three agent-driven tools cannot throw into the AG-UI stream.
  <br>``./scripts/check-agent-middleware.sh` also greps apps/web/src/components/CopilotChatSurface.tsx and asserts 0 occurrences of `await dispatchAction(` that are not inside a `try {` block. Today the count of unguarded ones is 3.`
- The agent can obtain a real page identifier, so `open_page` has a reachable success path.
  <br>``uv run pytest apps/api/tests/unit/test_search_wiki_returns_ids.py -q` — asserts each result line emitted by `search_wiki` contains a parseable UUID or slug token. Fails today: the formatter at copilot_agent.py:404-421 emits title/kind/score/summary only.`

**Review.** Mutation testing, three probes. (a) Remove `AlephAgentMiddleware` from the orchestrator's `middleware=` list and confirm `check-agent-middleware.sh` and `test_permission_denied_becomes_tool_message` both fail; restore. (b) Remove it from exactly one subagent dict and confirm the sweep fails naming that file; restore.
<br>**Iterate.** Second pass makes the error message useful to the model rather than merely non-fatal: include the tool name, the argument that was rejected, and a suggested next action per exception class (`NotFound` on a slug → 'call search_wiki first and use the page_id it returns').
<br>**Depends on:** WS-E1a
<br>**Risk.** Medium. The real risk is over-catching: swallowing a `PermissionDenied` and handing the model a friendly sentence must not become a way for the agent to keep probing a project it has no access to. Mitigate by making the guard re-raise nothing but still emitting a ledger-visible event for authorization failures (C3a records it), and by keeping `test_agent_project_authorization.p…


#### WS-E1c · The agent stops starving and timing out on itself: connection pool, request timeout, retry

**What it is.** Three settings currently make the assistant fragile under any load at all. First, the database connection pool the agent uses holds exactly one connection — every saved checkpoint, every memory read and every concurrent subagent queues behind the same single connection, and after 30 seconds of waiting the call fails. Second, every model call gives up after 60 seconds with only two retries; a slow turn or a rate-limit response from the gateway becomes an exception that (before E1b) killed the run. Third, there is no backoff — the retries are immediate, which is the worst possible response to being rate limited.

  <br>``uv run pytest apps/api/tests/unit/test_agent_store_pool.py::test_no_model_timeout_or_retry_literal_remains -q` passes — it walks the AST and asserts no `ChatOpenAI(...)` call node carries a literal `timeout` or `max_retries`. **CORRECTED 2026-08-22:** the original grepped the whole file for `timeout=60|max_retries=2`, which also matches `httpx.AsyncClient(timeout=60.0)` at :1164 — an HTTP client timeout in a tool, nothing to do with the model budget. Driving it to 0 would mean deleting an unrelated and correct line.

**How.** Three concrete changes plus one measurement. (1) Give the agent pool a real `max_size` and a settable timeout in `build_agent_store` (copilot_agent.py:1375-1406), defaulting to something comparable to the SQLAlchemy engine and reading from `apps/api/src/aleph_api/settings.py` so an operator can size it. Keep the langgraph-mandated kwargs (autocommit, prepare_threshold=0, dict_row) untouched. (2) Move the request timeout and retry count off the literals in `_gateway_chat_model` and onto settings, and raise the default — 60s is below the p99 of a tool-heavy turn against a shared gateway. (3) Implement `awrap_model_call` on the `AlephAgentMiddleware` built in E1b (hook verified at langchain/agents/middleware/types.py:586-635;

**Criteria:**

- The agent's Postgres pool is no longer single-connection. FAILS TODAY (max_size resolves to 1).
  <br>``uv run pytest apps/api/tests/unit/test_agent_store_pool.py::test_pool_max_size_is_not_one -q` — calls `build_agent_store(...)` and asserts `pool.max_size >= 8` and `pool.max_size > pool.min_size`.`
- Model timeout and retry budget are configuration, not literals.
  <br>``uv run pytest apps/api/tests/unit/test_agent_store_pool.py::test_no_model_timeout_or_retry_literal_remains -q` passes. NOT a file-wide grep for `timeout=60`: an unrelated `httpx.AsyncClient(timeout=60.0)` at copilot_agent.py:1401 keeps it red for a reason that has nothing to do with the model client, and green again if that innocent line is renamed. The test walks the `ChatOpenAI` CALL NODE with the AST, which is the actual property. Old form, for the record: `grep -n 'timeout=60|max_retries=2' … | wc -l` returns 0 (2 at the time), and `uv run pytest apps/api/tests/unit/test_agent_store_pool.py::test_no_model_timeout_or_retry_literal_remains -q` asserts the built model's `request_timeout` equals the settings value when that val…`
- A rate-limit response is retried with backoff instead of killing the turn. FAILS TODAY.
  <br>``uv run pytest apps/api/tests/unit/test_agent_model_retry.py::test_rate_limit_is_retried_with_backoff -q` — the middleware's `awrap_model_call` is driven with a handler that raises a rate-limit error twice then succeeds;`
- Exhausting the retry budget produces a typed, reportable failure — not a bare exception.
  <br>``uv run pytest apps/api/tests/unit/test_agent_model_retry.py::test_budget_exhaustion_is_typed -q` — handler always raises; asserts the escaping exception carries the `rate_limited` code E1a's iteration step consumes, and asserts the attempt count equals the configured budget.`
- The per-turn upstream request count is a printed number, so E5 can be argued about with data.
  <br>``./scripts/acceptance.sh --part H` runs row H2 (`scripts/_acceptance/agent_turn_probe.py`), which prints the per-turn upstream chat-completion count read from `model_calls`, plus time-to-first-token. Part H, not E — the probe's own docstring already names this criterion and only the part letter was wrong. Needs a reachable gateway and `ALEPH_ACCEPTANCE_DRIVE_AGENT=1`; it spends tokens. Fails today only in the sense that no such row exists;`

**Review.** Mutation testing plus a load probe. (a) Set `max_size` back to 1 and confirm `test_pool_max_size_is_not_one` fails; restore. (b) Make the backoff sleep a no-op and confirm `test_rate_limit_is_retried_with_backoff` fails on the sleep assertion; restore.
<br>**Iterate.** Second pass adds a concurrency ceiling with a queue rather than only retrying: a semaphore around the model call sized from settings, so a six-way subagent fan-out issues a bounded number of concurrent gateway requests instead of all of them at once. Retry is what you do after being rate limited; a ceiling is what stops you getting there. Pair it with the counting probe so the before/after is a number.
<br>**Depends on:** WS-E1b
<br>**Risk.** Medium-low. Raising the pool ceiling raises Postgres connection pressure in a process that already runs a 10+20 SQLAlchemy engine — check `max_connections` on the compose Postgres before picking a default, or the fix moves the failure rather than removing it.


#### WS-C3a · Every chat turn becomes a recorded run, with subagent identity — and H7 gets decided

**What it is.** Right now nothing about a conversation with the assistant is written down. There is no record that a turn happened, which tools it called, how long they took, which subagent did what, or how it ended. The only thing that survives is the raw message transcript in LangGraph's checkpoint — no timings, no errors, no attribution. Meanwhile Aleph already has a perfectly good table for exactly this (`agent_runs` + `agent_events`) and an SSE endpoint that streams it, both used only by the background worker jobs.

**Why.** The backlog claims C3 can be 'rebuilt on data Aleph already has'. It cannot — that data does not exist, and this is the workstream that creates it. Verified: `grep -rn 'AgentRun(' apps packages` finds 17 producers and none of them is in copilot_agent.py or subagents/; the only writers of AgentEvent rows are packages/aleph-db/src/aleph_db/repos/agent_events.py:48/67/89, called exclusively from worker workflows. This also settles H7 honestly.

**How.** Four pieces. (1) Mint the run at the endpoint E1a now owns: create an `AgentRun(project_id=..., agent_kind='assistant', correlation_id=f'chat-{uuid7().hex}', status='running', started_at=...)` before calling `agent.run()`, resolving project scope with the existing `_project_id_from_thread_id` (copilot_agent.py:283-298), and set `completed_at`/`status`/`error_text` in the `finally`. Write the `ActionLedgerEvent` in the same transaction, per the standing rule. (2) Plumb the run id: pass `config={'configurable': {'agent_run_id': str(run_id)}}` to the per-request `LangGraphAGUIAgent`.

**Criteria:**

- One chat turn produces exactly one agent_runs row of kind 'assistant'. FAILS TODAY — zero rows are ever written for a chat turn.
  <br>``uv run pytest -m integration tests/integration/test_chat_turn_is_recorded.py::test_one_run_per_turn -q` — drives one turn through the endpoint against live Postgres and asserts `select count(*) from agent_runs where agent_kind='assistant'` increased by exactly 1, with `status in ('completed','failed')` and a non-null…`
- Tool calls are recorded with arguments, duration and outcome.
  <br>``uv run pytest -m integration tests/integration/test_chat_turn_is_recorded.py::test_tool_events_recorded -q` — asserts ≥1 `tool_started` and a matching `tool_finished` sharing a `tool_call_id`, that `payload_jsonb['duration_ms']` is an int ≥ 0, and that the tool name matches the tool actually invoked.`
- A failing tool is recorded as failed rather than vanishing.
  <br>`Same file, `::test_tool_failure_recorded` — monkeypatches one tool to raise, asserts a `tool_failed` event exists whose payload names the exception class, and (cross-checking E1b) that the run's status is still 'completed'.`
- Events carry subagent identity, so the Inspector can attribute work. This is the H7 requirement, met without the experimental stream.
  <br>`Same file, `::test_subagent_attribution` — runs a turn that delegates, then asserts `select count(distinct payload_jsonb->>'subagent') from agent_events where agent_run_id = :id` is ≥ 2 and that one of the values is a real subagent name from {retriever, researcher, wiki_builder, viz_builder, analyst, reviewer}.`
- The already-built read path serves the new rows unchanged.
  <br>``curl -s "$API/v1/projects/$PID/agent-events?agent_run_id=$RUN" | jq 'length'` returns > 0 for a chat run — proving the existing route at apps/api/src/aleph_api/routes/agent_events.py:39 needs no change. Today it returns 0 for any chat turn because no chat turn writes rows.`
- The H7 decision is written down with its evidence.
  <br>``grep -n 'stream.subagents' docs/decisions.md` returns a line, and the surrounding entry cites langgraph/pregel/main.py's v3 kwarg rejection and ag_ui_langgraph's v2 hardcode as the reason. Absent today.`

**Review.** Mutation testing on the plumbing, because that is where this silently breaks. (a) Change the middleware to read `agent_run_id` from `metadata` instead of `configurable` and confirm `test_subagent_attribution` fails — this is the exact trap that made D2's `agent_run_id` permanently NULL, and the test must be able to catch it.
<br>**Iterate.** Second pass adds the state the timeline needs to be readable rather than merely complete: record the agent's plan (`write_todos`) transitions as events so the Inspector can show intent next to activity, and add a per-run rollup (`tool_count`, `token_total`, `wall_ms`, `failure_code`) onto `AgentRun.result_payload` so the run list does not require aggregating events client-side.
<br>**Depends on:** WS-E1a, WS-E1b
<br>**Risk.** Medium. Write amplification is the real one: a chatty turn could emit dozens of events, each currently written through its own short-lived session by design (repos/agent_events.py docstring), and that pattern was sized for worker phases (a handful per job), not per-tool-call chat. Measure event count per turn before shipping and batch if needed.


#### WS-D2 · Cost attribution closes: no uncosted call, no NULL run id, no frozen model name

**What it is.** Aleph is supposed to record what every model call cost and who it was for. On the assistant's path it frequently does not. Four separate holes: a response that does not report token counts is skipped entirely; a call that failed after burning tokens is dropped on the floor; the run id on every recorded row is always empty; and the model name written down is whichever model was resolved when the server started, not the one that actually answered — so changing the project's model profile mid-session mislabels everything after it.

**Why.** The standing rule is 'every LLM call writes a ModelCall + CostLedgerEvent', and CLAUDE.md already admits the agent path partially bypasses it. That admission is now understated. Verified: `on_llm_error` pops the pending entry and records nothing (copilot_cost_callback.py:272-274); `_agent_run_id_from_metadata` reads `metadata['agent_run_id']` (:74-84) and `grep -rn agent_run_id apps/api/src/aleph_api/` shows the only two minters are `_self_headers` (copilot_agent.py:270) and a2ui_handlers.py:86, neither of which to…

**How.** Move costing from the callback onto the `awrap_model_call` hook built in E1c, and keep the callback only as a token-extraction helper. The hook is strictly better positioned on all four counts: it receives `ModelRequest.model` so the model name is read per request rather than frozen (langchain/agents/middleware/types.py:95); it can read `runtime.config['configurable']['agent_run_id']` which C3a now populates and which deepagents forwards to subagents (subagents.py:557-564) — closing the NULL; it wraps the call so an exception is observable, letting a failed-but-billed call be recorded with `pricing_source` intact instead of silently dropped; and it sees the response messages directly, so `usage_metadata` extraction happens in one place. Keep `_extract_usage`'s defensive two-shape handling (copilot_cost_callback.py:180+) — it is correct and hard-won.

**Criteria:**

- Every recorded agent ModelCall carries a non-null agent_run_id. FAILS TODAY — the column is unconditionally NULL.
  <br>``uv run pytest apps/api/tests/unit/test_agent_cost_attribution.py -q` (the integration path of that name does not exist and never has, so the command exited 4 — neither a pass nor a fail), or `uv run python scripts/_acceptance/status_numbers.py` reporting number 5 as 0 — runs a turn, then asserts `select count(*) from model_calls where agent_run_id is null and purpose like 'assistant%'` is 0 for rows created during the turn.`
- A model call that fails after producing usage is still costed. FAILS TODAY — `on_llm_error` records nothing.
  <br>``uv run pytest apps/api/tests/unit/test_agent_cost_callback.py::test_a_failed_call_is_recorded_not_dropped -q` — drives `awrap_model_call` with a handler that raises after a partial response carrying usage; asserts one ModelCall row was written.`
- The recorded model name is the model that answered, not the one resolved at boot.
  <br>``uv run pytest apps/api/tests/unit/test_agent_cost_callback.py::test_the_recorded_model_is_the_one_that_answered -q` — builds a request whose `.model` differs from the handler's construction-time model and asserts the written row names the request's model.`
- A usage-free response produces a visible unpriced row, never an absence.
  <br>``apps/api/tests/unit/test_agent_cost_callback.py::test_no_usage_writes_an_unpriced_row_rather_than_nothing` — asserts a row exists with `pricing_source='unknown'` and a populated reason. Written as a full path, not a bare `::name`: `check-acceptance-claims.sh` skips any token whose path part is empty, which is the only reason this citation survived the sweep that caught c2/c3/c6. This replaces `test_skips_when_no_usage` (:262), which pins the defect.`
- Retrieval spend is attributed to the real caller, not to a synthetic dev user.
  <br>``grep -n '_dev_principal(' apps/api/src/aleph_api/copilot_agent.py | wc -l` returns 0 — again a CALL. **CORRECTED 2026-08-22:** 2 mentions, **0 call sites**; both remaining hits are the definition and the comment recording that retrieval spend used to be billed to a synthetic user.
- Cache-write tokens stop being a column nothing writes.
  <br>``uv run pytest apps/api/tests/unit/test_agent_cost_callback.py::test_cache_write_tokens_are_extracted -q` asserts a response carrying cache-creation tokens produces a row with `cache_write_tokens > 0`.`

**Review.** Mutation testing against the exact failure shape that produced this hole. (a) Revert the `agent_run_id` read back to `metadata.get('agent_run_id')` and confirm `test_run_id_is_populated` fails — this proves the test catches the configurable/metadata distinction that made the field NULL for the whole life of the feature. (b) Delete the failed-call recording and confirm `test_failed_call_is_still_recorded` fails.
<br>**Iterate.** Second pass turns attribution into a budget: a per-project spend ceiling checked in `awrap_model_call` before the call, which refuses (or downgrades the model) rather than silently overspending, and surfaces remaining budget on the Inspector's run header. That is the thing the recorded data is actually for; recording without a control loop is bookkeeping.
<br>**Depends on:** WS-C3a
<br>**Risk.** Medium. Removing `_dev_principal` from `_read_wiki_impl` changes who the retrieval router runs as, and the router is legacy wiki code under removal — there is a real chance the real principal lacks something the dev principal implicitly had, which will show up as a permission failure in the retriever subagent rather than at the change site.


#### WS-C3b · The Inspector pane — see what the agent did, and where it broke

**What it is.** A new pane on the workspace board, alongside Wiki and Library, that shows what the assistant actually did: the runs it has performed on this project, and for the selected run a timeline of every tool call with its arguments, how long it took, what came back, which subagent made it, and — when a run fails — the exact point of failure with the error. Today none of this is visible anywhere; the only place an agent failure is legible is the API container's stderr, which is precisely what this removes the need for.

**Why.** The owner named this as the one genuinely valuable idea worth taking from the Enterprise Intelligence product, and rebuilding rather than buying it is already the recorded decision. It matters more for the harness thesis than for research: once the agent authors and activates its own plugins, 'what did it just do and why did it stop' becomes the primary operational question, and there is no answer to it today.

**How.** The pane mechanism already exists and is server-driven, so this is a registry entry plus a surface builder plus renderers, not a new subsystem. (1) Add `PaneKind(id='inspector', title='Inspector', icon='inspector', launchable=True, params=('run_id',))` to packages/aleph-a2ui/src/aleph_a2ui/pane_registry.py:47-61 — the `extend()` seam at :70-82 is documented as the plugin door and is currently unused, so use it as designed rather than editing `_CORE` if the Inspector is to be a plugin-shaped surface. (2) Generalize pane parameters: `_parse_pane_specs` in apps/api/src/aleph_api/routes/surfaces.py:178-203 only reads `page_id` — hardcoded to the point that the grounding pane declares `params=('claim_id',)` in the registry but receives the claim id under the name `page_id` (see the apologetic docstring at surfaces.py:928-929). Make the parser pass the declared params through by name;

**Criteria:**

- The Inspector is a real pane the server advertises. FAILS TODAY — the registry has 7 kinds and none is an inspector.
  <br>``curl -s $API/v1/projects/$PID/panes | jq '[.panes[].id] | index("inspector")'` returns a number, not null; and `./scripts/check-pane-registry.sh` exits 0. No pane COUNT: it was written as '8 kinds (7 today)', there are 12, and a count here goes red every time a plugin adds a pane — which is the feature.`
- A pane parameter other than page_id survives the round trip. FAILS TODAY — the parser drops everything but page_id.
  <br>``uv run pytest apps/api/tests/unit/test_pane_specs.py::test_declared_params_are_parsed -q` — asserts `_parse_pane_specs('inspector:run_id=abc')` yields the run id under the name `run_id`, and that a param not declared on the PaneKind is rejected rather than silently passed.`
- The pane renders a real failing run end to end, including the failure point.
  <br>``uv run pytest -m integration tests/integration/test_inspector_surface.py::test_failed_run_shows_its_failure -q` — seeds a run with a `tool_failed` event, calls `GET /v1/projects/{id}/surfaces/inspector?run_id=...`, and asserts the returned A2UI message list contains the failing tool's name and the error text.`
- The dead ActivityCard is gone and its endpoint has a live consumer again.
  <br>``test -e apps/web/src/components/ActivityCard.tsx` returns non-zero, and `grep -c 'AgentEvent' apps/api/src/aleph_api/routes/surfaces.py` is ≥ 1 — the consumer is the SERVER-side `_inspector_messages` builder. Not `grep -rn 'agent-events' apps/web/src`: its two hits are SSE-budgeting comments in `SurfaceStreamProvider`, so it reads green whether or not the Inspector exists; and 'pointing at Inspector code' is unsatisfiable now that a pane owns no transport and therefore fetches nothing.`
- The new UI is born inside the design spec rather than adding to the 180-violation backlog.
  <br>``grep -cE 'rounded-(sm|md|lg|xl|full)|shadow-(sm|md|lg|xl)|text-(slate|gray|zinc|red|green|blue)-[0-9]' apps/web/src/a2ui/components/InspectorSurface.tsx` returns 0. ONE file: the Inspector shipped as a single component, not the three (`RunTimeline`, `RunList`, `ToolCallCard`) this criterion predicted, so the command exited 2 on three missing paths — neither a pass nor a fail.`
- Every catalog and binding sweep stays green with the new components.
  <br>``./scripts/check-catalog-generated.sh && ./scripts/check-single-catalog.sh && ./scripts/check-surface-bindings.sh && pnpm -C apps/web lint && pnpm -C apps/web build` all exit 0.`

**Review.** Mutation testing on the two seams that fail silently here. (a) Rename one prop in `inspector_surface_v09` without updating the client zod schema and confirm `check-surface-bindings.sh` fails naming it — this is the sweep that exists because the wiki surface once shipped ten categories the client never declared; restore.
<br>**Iterate.** Second pass makes it a debugging instrument rather than a log viewer: filter the timeline by subagent and by outcome, show the model's arguments and the tool's return side by side (truncated with expand), add a copy-as-report action that produces a pasteable failure summary, and link a run's cost rows (D2) into the run header so 'what did this turn cost' is answerable in the same place as 'what did it do'.
<br>**Depends on:** WS-C3a
<br>**Risk.** Medium. The pane parameter generalization touches the parser every existing pane goes through, including the grounding pane whose claim id currently arrives mislabelled as `page_id` — that migration must be done in the same change or grounding breaks silently, which is exactly the failure mode `check-surface-bindings.sh` was written for.


#### WS-H5 · Bounded fan-out: give the agent a scratchpad it can loop in, instead of guessing how many subagents to launch

**What it is.** Today, when the assistant needs to do the same thing to twenty items, the model decides one turn at a time how many helpers to launch — and it is bad at this. It tends to sample a few items rather than cover all of them, and the number of requests it fires at the gateway is unpredictable, which is the most likely cause of the rate limiting the owner has been hitting. An interpreter gives the agent a small sandboxed scratchpad where it writes a few lines of code that loops over the twenty items, calls a helper for each, and keeps the intermediate results out of the conversation.

**Why.** This is the deterministic half of the E5 fix that E1c's retry and ceiling only paper over — retry manages the symptom, bounded fan-out removes the cause, and the two belong together. It also matters directly to the research capability the owner wants to be state of the art: the failure mode the docs describe (the model samples rather than covers) is exactly wrong for a literature sweep where covering every candidate paper is the point.

**How.** Start with the version arithmetic, because it decides the whole shape. Verified against PyPI: `langchain-quickjs` 0.2.0 requires `deepagents>=0.6.8,<0.7.0`, while the latest (0.3.5) requires `deepagents>=0.7.0,<0.8.0`. Aleph is on deepagents 0.6.6 and pins `deepagents>=0.6,<0.7` at apps/api/pyproject.toml:22. So the minimal viable step is a patch bump to 0.6.8 inside the existing pin plus `langchain-quickjs~=0.2.0` — not the major upgrade the backlog implies. `quickjs-rs` ships as a `py3-none-any` wheel, so there is no build toolchain to add to the container.

**Criteria:**

- The dependency step is proven safe before anything is built on it.
  <br>`After `uv sync --all-packages --all-extras` with deepagents pinned to 0.6.8: `uv run pytest -m 'not integration' -q` and `uv run pytest -m integration -q` both exit 0, `uv run pyright` reports 0 errors, and `./scripts/acceptance.sh` shows no new FAIL rows versus the run recorded before the bump.`
- The interpreter tool is actually present on the built graph. FAILS TODAY — `langchain_quickjs` is not installed and no interpreter middleware exists anywhere in the venv.
  <br>``uv run pytest apps/api/tests/unit/test_interpreter_middleware.py::test_interpreter_tool_is_registered -q` — builds the real graph via `build_assistant_deep_agent` and asserts the interpreter's tool name appears in the compiled tool set, and that `CopilotKitMiddleware` still precedes it in the middleware list.`
- A scripted fan-out task covers every item instead of sampling.
  <br>``./scripts/acceptance.sh --part E` runs a fixture task over N=20 known items and prints items-touched / N. The criterion is that the printed ratio is 20/20 after the work; record the pre-work ratio in the same format so the improvement is a number rather than an impression.`
- The same task issues fewer, bounded gateway requests.
  <br>`The same acceptance row prints the per-turn upstream chat-completion count from E1c's counter. The criterion is that the count for the fixture task is deterministic across three consecutive runs (identical number each time), which model-driven fan-out is not.`
- The sandbox stays a sandbox — interpreter code cannot reach Aleph's process.
  <br>``uv run pytest apps/api/tests/unit/test_interpreter_middleware.py::test_interpreter_cannot_reach_the_host -q` — asserts an interpreter program attempting filesystem or network access fails, and that the interpreter's tool access list contains only the agent tools deliberately exposed.`

**Review.** Two-stage, because the dependency and the feature fail differently. Stage one is a revert drill: bump the dependency on a branch, run every gate, and confirm the exact command to go back (`uv sync` with the old pin) restores a green suite — a dependency bump you cannot cheaply undo is not a step, it is a commitment.
<br>**Iterate.** Second pass moves Aleph's own fan-out-shaped work onto it: the reviewer subagent's per-page lint sweep and the researcher's per-paper DOI verification are both loops the model currently drives one call at a time, and both have hard coverage requirements.
<br>**Depends on:** WS-E1b, WS-E1c
<br>**Risk.** High relative to the rest of this cluster, and the highest here. It is the only new runtime dependency in the plan, it is marked beta upstream, and it pulls a JavaScript engine into the API process — which is the same process that hosts the agent in-band with every HTTP request, so a hang or a memory leak in the sandbox is an API outage, not a degraded feature.


#### WS-H6 · Background delegation that works here: long jobs return a ticket, the assistant keeps talking

**What it is.** When the analyst asks for something slow — a deep research pass, a full review sweep — the assistant currently stands still until it finishes, because a delegated helper is called and waited on inline. What is wanted is: the assistant hands the job off, immediately gets back a ticket, tells the analyst 'that is running, I will let you know', and can be asked later how it is going or told to stop. Aleph already does a rough version of this for one case: `start_research` fires a request at its own API and returns straight away.

**Why.** The backlog assumes deepagents' `AsyncSubAgent` is the answer and that a `langgraph.json` unlocks it. It is not, and it does not. `AsyncSubAgent` requires `graph_id` — an assistant id on an Agent Protocol server (deepagents/middleware/async_subagents.py:57, docstring at :1-10) — and the in-process ASGI transport it mentions works by importing `langgraph_api.server.app` (langgraph_sdk/_async/client.py:110-125), which requires the process to BE a LangGraph API server.

**How.** Three tools on the orchestrator, built the way every other Aleph agent tool is built — self-calling tested HTTP routes with a minted agent token, never raw DB (`_self_headers`, copilot_agent.py:258-282). (1) `start_background_task(kind, params)` dispatches an arq job, creates its `AgentRun` with `status='pending'` and a parent link to the chat run C3a minted, and returns the run id as the ticket. Generalize the existing pattern: `_start_research_impl` (copilot_agent.py:825-868) already does exactly this for one kind by POSTing `/synthesize`, and `packages/aleph-research/src/aleph_research/dispatch.py:80` already creates the `AgentRun(status='pending')` → ledger sequence. (2) `check_background_task(run_id)` reads status plus the latest phase events — the read path already exists at apps/api/src/aleph_api/routes/agent_events.py:39 and takes an `agent_run_id` filter.

**Criteria:**

- The assistant can start a long job and get a ticket back within a short bound. FAILS TODAY for anything but research — there is no general start/check/cancel.
  <br>``uv run pytest -m integration tests/integration/test_background_delegation.py::test_start_returns_immediately -q` — asserts `start_background_task` returns in under 2 seconds wall clock while the dispatched job is still `pending` or `running`, and that the returned ticket is a valid `agent_runs.id`.`
- Checking a ticket reports real progress, not a guess.
  <br>`Same file, `::test_check_reports_phases` — asserts the tool's output names the most recent phase from `agent_events` for that run and its status, and that after the job completes the status is terminal.`
- Cancelling actually stops the work and is auditable.
  <br>`Same file, `::test_cancel_stops_the_job` — asserts the run reaches a cancelled terminal status, that no further `phase_started` events are written after the cancel, and that an `ActionLedgerEvent` for the cancellation exists in the same transaction as the status change.`
- A background run is linked to the conversation that started it, so the Inspector can show both.
  <br>`Same file, `::test_parent_link` — asserts the background `AgentRun` carries the chat turn's run id (in `input_payload`) and that `GET /v1/projects/{id}/agent-events?agent_run_id=<chat run>` plus the child run together reconstruct the full chain.`
- The prompt no longer claims an inline subagent runs in the background.
  <br>``grep -n 'Runs in the background' apps/api/src/aleph_api/copilot_agent.py apps/api/src/aleph_api/subagents/*.py` returns 0 hits for any subagent that is invoked with `await subagent.ainvoke`; the phrase survives only where a real ticket is returned.`
- The rejected alternative is recorded so it is not re-litigated.
  <br>``grep -n 'AsyncSubAgent' docs/decisions.md` returns an entry giving the three reasons actually recorded: a detached coroutine has no record across a reload, cancellation must be a checkpoint rather than a signal, and the work does not belong in the API process. NOT the `graph_id` / `langgraph_api.server` clause — neither string appears in `docs/decisions.md`, and what was written is the stronger argument.`

**Review.** Mutation testing plus a failure drill. (a) Make the worker ignore the cancellation flag and confirm `test_cancel_stops_the_job` fails; restore. (b) Break the parent link and confirm `test_parent_link` fails; restore. (c) Failure drill: kill the arq worker while a background run is in flight and confirm the run does not sit at `running` forever — either a heartbeat marks it stale or a startup reaper resolves it;
<br>**Iterate.** Second pass replaces polling with a push: when a background run terminates, emit into the conversation rather than waiting to be asked — the `/agent-events` broker already wakes on a Postgres NOTIFY the instant a row commits (routes/agent_events.py docstring), so the notification path exists and only needs a consumer that reaches the chat surface.
<br>**Depends on:** WS-C3a, WS-C3b
<br>**Risk.** Medium. Cancellation is the hard part and the easiest to fake: a flag nobody checks looks identical to a flag that works until you test it, which is why the drill above is a criterion rather than a nicety. Second risk: this adds a general 'the agent can dispatch background work' primitive, and without the concurrency cap in the iteration step that is a way for a confused agent…


---

### Model endpoint, discovery, and per-model harness profiles


#### WS-MEP-1 · Agent spend gets a real price tag

**What it is.** Aleph writes a row for every model call recording tokens used and money spent. Two different code paths make model calls: the transport path (LiteLLMClient — used by ingest, retrieval, research) and the agent path (the chat assistant and its six helper agents). The transport path is handed a price list when the process boots. The agent path is not: it silently builds its own EMPTY price list, so every single chat message is recorded as 'we do not know what this cost'.

**Why.** CLAUDE.md's stated commitment is 'Cost provenance is recorded, never assumed... an unpriced call is never a silent $0.' Today the reality is worse than a silent $0: it is a permanent 'unknown' on 100% of assistant traffic, which is the most expensive traffic in the system. Backlog E4 blames the gateway for not reporting rates — that is false. claude-sonnet-4-6 is priced in the shipped hints file (packages/aleph-models/src/aleph_models/model_hints.json) and apply_hints fills it (hints.py:107-157).

**How.** 1. Stop the fabrication. AgentCostCallbackHandler._resolve_pricing (apps/api/src/aleph_api/copilot_cost_callback.py:190-196) reads the kernel's PRICING object from the bound runtime, the same lazy way it already reads session_maker at :180-189. bind_runtime (copilot_agent.py:192-205) gains a pricing= parameter; apps/api/src/aleph_api/lifespan.py:102-107 passes reader.get(PRICING). Keep recording 'unknown' when nothing is bound, but log 'no pricing table bound' as an error distinct from 'model absent from table' — today those two very different failures are indistinguishable. 2. Per-model provenance. PricingTable carries ONE _source for the whole table (pricing.py:95-106); merge assigns self._source = other._source (pricing.py:148-152) and from_discovery labels the whole table 'static' if any single priced model came from hints (:137-140).

**Criteria:**

- A handler constructed the way production constructs it (no pricing= argument) prices a known model instead of recording 'unknown'
  <br>`New test apps/api/tests/unit/test_agent_cost_callback.py::test_handler_built_the_production_way_uses_the_bound_pricing_table — mirrors copilot_agent.py:1506 exactly, binds a populated table via bind_runtime, asserts recorded pricing_source != 'unknown'.`
- The cost callback can no longer invent an empty price list
  <br>`apps/api/tests/unit/test_agent_cost_callback.py::test_the_callback_no_longer_invents_a_table, ::test_an_unbound_table_is_not_cached_so_a_late_gateway_still_prices and ::test_no_pricing_table_bound_is_reported_as_its_own_failure pass. NOT a grep for `PricingTable()`: that returns 2 today and can only reach 0 by deleting the docstring at :209 that explains the fabrication AND the empty-table fallback at :258 the design requires, so the check was red forever and rewarded removing the explanation.`
- Two models in one table keep different provenance labels
  <br>`uv run pytest packages/aleph-models/tests/test_pricing.py -q, with a new test merging a gateway-reported model and a hint-filled model into one table and asserting breakdown() returns source 'gateway' for the first and 'static' for the second. FAILS TODAY: pricing.py:148-152 assigns one label to the whole table.`
- A gateway that was down at boot produces priced calls once it comes up, with no restart
  <br>`Integration test with a fake catalog and an injected clock: first call records pricing_source='unknown', advance the clock past one refresh interval, second call records 'gateway'. No real sleeping. FAILS TODAY: capabilities.py:294 is the only refresh, at boot.`
- Agent cache-write tokens are billed at their premium rate rather than as ordinary input
  <br>`uv run pytest apps/api/tests/unit/test_agent_cost_callback.py -q with a case asserting cost_usd for 1000 cache-write tokens differs from cost_usd for 1000 plain input tokens. FAILS TODAY: _extract_usage never reads cache_creation.`
- An operator can supply their own rates without editing compose
  <br>`grep -c ALEPH_MODEL_HINTS_PATH deploy/compose/.env.example returns >= 1 (returns 0 today).`

**Review.** Mutation testing on all three mechanisms, each reverted after the check. (a) Remove the pricing= argument from the lifespan bind_runtime call and confirm the new production-wiring test fails. (b) Restore 'self._source = other._source' in PricingTable.merge and confirm the two-provenance test fails.
<br>**Iterate.** v2 turns the number into something an operator sees rather than something an auditor could reconstruct: GET /v1/projects/{id}/cost gains a breakdown by pricing_source, so the answer reads '$12.40 gateway-priced, $3.10 asserted from hints, $0.00 across 4 unpriced models' and unpriced spend becomes a figure on a screen instead of an ERROR line in container logs.
<br>**Depends on:** —
<br>**Risk.** The background refresh task is the risk, not the pricing maths. The models capability is protected = true in both boot manifests (apps/api/aleph.toml:36-40 and apps/workers/aleph.toml), so a refresh task that does not cancel cleanly stops BOTH processes from exiting. Cancel with a timeout and assert shutdown completes inside scripts/_acceptance/kernel_boot.py.


#### WS-MEP-2 · One metered door to the gateway

**What it is.** 'Weirdly rate limited' means Aleph sends the model server more requests than it will accept. Nothing in Aleph counts how many requests are in flight — searching the entire codebase outside tests for a concurrency limiter returns zero hits. Four separate things fire unbounded bursts: the assistant runs every tool call in one turn simultaneously, so six helper agents can all start at once; 'Configure from gateway' calls every model the gateway advertises at the same time; the Docker healthcheck pings the model server every 15 seconds forever; and ten worker jobs can each be mid-call.

**Why.** Aleph's stated design is that it connects OUT to whatever endpoint the operator has — a shared LiteLLM key, a laptop running Ollama, a Bedrock proxy with a per-minute quota. Every one of those has a ceiling and Aleph currently has no concept of one. Without this, MEP-4 makes the situation strictly worse rather than better: per-project endpoints mean N clients each with their own unbounded connection pool.

**How.** 1. A GATEWAY_LIMITER kernel capability in packages/aleph-runtime/src/aleph_runtime/capabilities.py, sitting beside http_clients (:184-200): an async token bucket plus concurrency semaphore keyed by endpoint, limits from settings (ALEPH_GATEWAY_MAX_CONCURRENCY, ALEPH_GATEWAY_RPM). LiteLLMClient._post_with_retry (client.py:477-493) and probe_model / discover_models acquire it. The agent path is the hard case because ChatOpenAI owns its own httpx client: give _gateway_chat_model (copilot_agent.py:1483) an http_async_client= built from a limiter-aware httpx transport. That is the only seam where the limiter can sit without forking langchain. 2. Honour Retry-After. packages/aleph-models/src/aleph_models/retry.py waits 1/2/4s blind (:28-31) and inspects no headers, while packages/aleph-scholar/src/aleph_scholar/http.py:51-56 already parses and honours it correctly.

**Criteria:**

- Concurrent gateway calls never exceed the configured ceiling
  <br>`grep -c GATEWAY_LIMITER packages/aleph-runtime/src/aleph_runtime/capabilities.py returns >= 1 (7 today) — the limiter is a kernel capability wherever its semaphore lives, and it lives in aleph_models/limiter.py by design, so grepping capabilities.py for `Semaphore` is red forever and would be satisfied by moving working code into the wrong package. The behaviour half is the check: a unit test firing 20 concurrent LiteLLMClient.chat() calls at a fake gateway that records max in-flight, asserting it never exceeds ALEPH_GATEWAY_MAX_CONCURRENCY.`
- A 429 carrying Retry-After: 7 causes a ~7 second wait, not a 1 second one
  <br>`Unit test in packages/aleph-models/tests/ using a fake clock, asserting the computed wait. FAILS TODAY: retry.py:28-31 uses wait_exponential(min=1,max=4) and no code in that file reads any header.`
- Being rate limited during autoconfigure does not disqualify a model
  <br>`Unit test: probe_model returns a rate-limited outcome for model X; select_default_bindings still binds X. FAILS TODAY: discovery.py:335-336 returns an error string and routes/model_profile.py:154 folds it into `unreachable`, which discovery.py:472 filters out.`
- Autoconfigure against a 40-model gateway probes within the concurrency ceiling
  <br>`Integration test with MEP-3's fake counting concurrent in-flight probes — asserted by the counter, not by wall-clock timing.`
- Two /readyz hits one second apart produce exactly one outbound gateway request
  <br>`Test counts requests on the fake gateway. FAILS TODAY: routes/health.py:49 calls the gateway on every hit, so it produces two.`
- No capability is offered in Settings that nothing can resolve
  <br>`python3 scripts/_lib/capability_offers.py exits 0 — every capability the Settings picker offers has a resolution policy and a production caller. NOT `rerank appears exactly once`: that criterion assumed rerank was a dead offer to be deleted, and WS-RS6 did the opposite — it is now 27 hits with a production caller at retrieval/router.py:429 and a measured eval arm, so the old form can only pass by removing shipped capability. Original wording, for the record: exactly 1 (the enum member alone), 4 at the time (enum, policy at discovery.py:392, and Drawers.tsx:88 and :107).`

**Review.** One mutation per criterion, each restored afterward. Raise ALEPH_GATEWAY_MAX_CONCURRENCY above the burst size and confirm the concurrency test fails. Delete the Retry-After parse and confirm the wait test fails. Revert the 429 branch in probe_model and confirm the binding test fails. Point /readyz back at litellm.health() and confirm the one-request test fails.
<br>**Iterate.** v1 bounds request COUNT. v2 bounds SPEND: the limiter reads the per-project cost total MEP-1 makes trustworthy and refuses to start a new agent turn once a project's configured budget is exhausted, returning a message the assistant can say out loud rather than surfacing someone else's 429.
<br>**Depends on:** —
<br>**Risk.** The highest risk in the cluster is item 1's agent half. Passing a custom http_async_client to ChatOpenAI bypasses the openai SDK's own connection management, and its max_retries=2 (copilot_agent.py:1505) then stacks on top of the limiter's queueing — a request waiting for a token can be retried by the SDK while still queued, doubling the queue depth under exactly the conditions…


#### WS-MEP-3 · A second endpoint that exists only for tests, and docs that stop describing one that does not

**What it is.** The point of the next two workstreams is 'point Aleph at a different model server and it works'. You cannot demonstrate that with one server. There is currently no way to run Aleph against a second OpenAI-compatible endpoint in a test, and four documents tell you there is — CLAUDE.md, docs/operations.md, docs/architecture.md and docs/acceptance.md all describe a `--profile local-llm` compose service that was deliberately deleted.

**Why.** Every measurable criterion in MEP-1, MEP-2, MEP-4 and MEP-5 needs a gateway a test controls. Without one, those criteria collapse into 'it worked when I tried it against my LiteLLM' — precisely the class of evidence CLAUDE.md's preamble says allowed a broken retrieval path to survive seven work packages. It is also the honesty item this cluster owes: deploy/local-gateway/ was removed in 483816d with explicit reasoning ('Aleph serves no models...

**How.** 1. One shared fake, imported by several packages' tests rather than reimplemented per package: a Starlette/FastAPI app serving /model/info, /v1/models, /v1/chat/completions and /v1/embeddings, driven by a config object. Mount it with httpx.ASGITransport so no socket opens — which matters because the agent runs in-process inside FastAPI and anything on the request path must be async. 2. Scriptable failure modes, each one a real defect this cluster fixes: /model/info answering 403 (the normal restricted-virtual-key case that discovery.py:272-280 falls back from), rates present versus absent, 429 with and without Retry-After, a model that lists happily and 400s on invocation (the Bedrock inference-profile case documented at discovery.py:305-311), a slow response for timeout tests, plus a request counter and a max-concurrency recorder that MEP-2's criteria read. 3.

**Criteria:**

- The fake reproduces every gateway misbehaviour this cluster handles
  <br>`uv run pytest -m 'not integration' -q -k gateway_fake passes with at least 8 tests covering: /model/info 403 fallback, rates absent, 429 with Retry-After, 429 without, list-but-400-on-invoke, slow response, embedding mode, request counting.`
- No document names a path that does not exist
  <br>`./scripts/check-dead-refs.sh exits 0. There is no scripts/_acceptance/docs_claims.py and none is planned — check-dead-refs.sh is the sweep that shipped for this, and it now also reads audit/claims.yaml and asserts docs/operations.md names every sweep.`
- The deleted local gateway is gone from the prose as well as the tree
  <br>`grep -rn 'local-llm\|local-gateway' CLAUDE.md docs/operations.md docs/architecture.md docs/acceptance.md | wc -l returns 0. Scoped deliberately: over all of docs/ it returns 11, and 8 of those are docs/plan.md quoting this criterion, so the unscoped form can never reach 0 while the criterion exists to be read. docs/plan.md and docs/update/ are out of scope — the plan records history and the update reports are history.`
- The fake is genuinely shared, not copied per package
  <br>`grep -rl 'FakeGateway' apps packages | wc -l returns >= 3, with exactly one file defining it (grep -rl 'class FakeGateway' returns 1).`
- The docs sweep is proven capable of failing
  <br>`scripts/acceptance.sh --self-check reports `can fail` for the probes 'check-dead-refs notices a path that is not there' (self_check.sh:179) and 'check-dead-refs notices a sweep the operations doc stopped naming' (:220). There is no docs-claims check to include — check-dead-refs.sh is the sweep, per c2.`

**Review.** Mutation on the sweep first, since it has the most leverage: add a line to docs/operations.md referencing deploy/does-not-exist/, confirm docs_claims.py exits non-zero AND names the file and line number rather than just failing, then remove it.
<br>**Iterate.** v2 promotes the fake into a conformance suite: one parametrised test running Aleph's entire model path against three endpoint profiles — a full-metadata gateway, an ids-only restricted-key gateway, and an ids-only gateway with no hints available — asserting what Aleph does in each.
<br>**Depends on:** —
<br>**Risk.** Low, with one trap worth naming: a fake that is too permissive silently weakens every test built on it. Guard against that by making the fake's DEFAULT configuration the hostile one — restricted key so /model/info 403s, no rates reported — so a test wanting a well-behaved gateway has to ask for it explicitly.


#### WS-MEP-4 · Gateway endpoints become data: a table, a secret store, a resolver, and a probe

**What it is.** Today the address of the model server and its key are read from environment variables once when the process starts (apps/api/src/aleph_api/settings.py:60-61, cached at :101-103) and baked into a single client object at boot (packages/aleph-runtime/src/aleph_runtime/capabilities.py:295-303). Changing either means editing a file on the server and recreating containers. This makes an endpoint a normal row in the database — a name, a URL, an encrypted key, owned by a project — and replaces 'the one client built at boot' with 'look up the client this project should use'.

**Why.** This is the item the backlog itself calls 'the one that blocks using Aleph against anything but the currently configured gateway' (docs/backlog.md:91-92). The product thesis is a harness whose abilities are added and swapped at runtime; a harness that cannot change which model serves it without a redeploy is not that harness.

**How.** 1. Schema. A gateway_endpoints table in packages/aleph-db/src/aleph_db/models/, carrying CommonColumns per the project_id rule: name, base_url, cipher_blob, cipher_scheme, key_id, is_default, last_probe_at, last_probe_ok, last_probe_error. Alembic migration under apps/api/alembic/versions/ following the YYYYMMDD_HHMM_<slug>_<message>.py pattern; `alembic check` must stay clean. ModelProfile (packages/aleph-db/src/aleph_db/models/model_profile.py:24-27 — today only name/project_id/is_template/bindings_jsonb) gains an endpoint_id, and ModelBindingIn drops the provider field pinned by regex to the literal 'litellm' (packages/aleph-core/src/aleph_core/schemas/model_profile.py:55) in favour of the endpoint reference. Note extra='forbid' at :52 means every schema consumer must be updated together. 2. Secrets, done properly this time.

**Criteria:**

- The new schema lands without model drift
  <br>`cd apps/api && uv run alembic upgrade head && uv run alembic check exits 0 with gateway_endpoints present.`
- Two projects on two different endpoints see two different model lists
  <br>`Integration test creating two endpoints against two instances of MEP-3's fake serving disjoint model lists, binding project A to one and B to the other, asserting the per-project models route returns disjoint sets. FAILS TODAY: there is no per-project endpoint anywhere in the tree.`
- A call made under project B's scope reaches B's endpoint and never A's
  <br>`Integration test asserting each fake's request counter — B's incremented, A's unchanged.`
- A rotated encryption key produces a detectable failure, not a silent wrong answer
  <br>`Test writing a row under key_version=1, rotating to key_version=2, asserting the read reports undecryptable-with-key-version rather than raising an opaque error.` **CORRECTED 2026-08-22:** the column is `key_version`, not `key_id`, and it already exists — `packages/aleph-connectors/src/aleph_connectors/credentials.py` plus migration `20260822_0030_p7_credential_key_version.py`. This workstream reuses the `ConnectorCredential` cipher and its versioning rather than inventing a second one, so the secret half of MEP-4 is much cheaper than the plan assumed.
- The test-connection route reports what the endpoint actually said
  <br>`Against a fake that 403s /model/info it returns 200 with model_info_allowed false and a non-empty model list; against an unreachable URL it returns the transport error verbatim in the body. Asserted on response content, not status alone.`
- No code reads a single process-wide model client any more
  <br>Both surfaces, excluding comments and docstrings: **one** `app.state.litellm` in `apps/api/src` — `routes/health.py`'s `/readyz` leg, named as the deliberate exception — and **zero** `ctx["litellm_client"]` / `ctx["gateway_catalog"]` in `apps/workers/src` (13 before `WS-MEP-4`, in `arq.py` and eleven job modules), each paired with a two-fake request-counter isolation test at that layer: `tests/integration/test_worker_gateway_endpoints.py` and `tests/integration/test_agent_profile_reaches_the_wire.py`. Count the CODE, not the mentions — fixing this defect and explaining the fix took the naive grep from 3 to 6, so the grep rewarded leaving the prose out. Measured 2026-08-22: 6 mentions in `apps/api/src`, all prose but the `/readyz` call; 4 in `apps/workers/src`, all prose. **CORRECTED 2026-08-22:** the target was 0, and 0 is wrong. Readiness is a statement about the DEPLOYMENT — "ready for which project?" has no answer — so the liveness probe legitimately reads the process-wide client. Every PROJECT-scoped caller now resolves through `litellm_for_project`, which is what the criterion is actually about: a `gateway_endpoints` row that reads back correctly and routes its traffic somewhere else.
- An endpoint key never leaves the server
  <br>`Test asserting the plaintext key appears in no response body and in no ledger payload for gateway_endpoint.create / .update.`

**Review.** Mutation on the isolation guarantee first, because that is the one that becomes a security incident if it regresses: make the resolver ignore its cache key and always return the default client, confirm BOTH the disjoint-model-list test and the request-counter test fail, restore. Then mutation on redaction: return the decrypted key in the output schema and confirm the leak test fails.
<br>**Iterate.** v1 scopes an endpoint to a project. v2 adds ordered fallback: a list of endpoints per capability, so when the primary answers 429 or 503 the resolver moves to the next and records which endpoint served each ModelCall. That converts MEP-2's limiter from a brake into a router, and it is the point where 'connect to any OpenAI-compatible endpoint' becomes 'survive one of them going down' — which is the difference between…
<br>**Depends on:** WS-MEP-1, WS-MEP-2, WS-MEP-3
<br>**Risk.** Three real ones. (1) Secret migration: existing ConnectorCredential rows are encrypted under sha256(agent_token_secret || project_id) with no key id, so the migration must re-encrypt under the new key inside a transaction and must be reversible, or a failed deploy destroys every connector credential in the deployment. Write and TEST the down-migration.


#### WS-MEP-5 · The endpoint in the UI: add it, test it, switch to it, never see the key again

**What it is.** The settings panel today offers exactly three model controls: pick a named profile, pick a model per job, and 'Configure from gateway' (apps/web/src/components/Drawers.tsx:118, :207-346). There is no field for the server address, no field for the key, and no button that says 'check this works'. This adds a list of endpoints for the project, an add/edit form with the key masked once saved, a Test connection button that reports what the endpoint actually answered, and a visible mark on models that were proven unreachable so the picker stops offering them.

**Why.** MEP-4 without this is a database table nobody can reach — the exact producer-with-no-consumer defect CLAUDE.md names as the dominant class in this codebase, reproduced at the very moment the plan sets out to fix it. Beyond that, the owner's bar is explicit that the UI 'cannot be half-baked with stale or dead code laying around and every part needs to function as expected', and the model settings have three visible defects independent of endpoints: PROFILE_NAMES is hardcoded to ['aleph-dev','aleph-production'] at Dr…

**How.** 1. A GatewayEndpointsSection component in its OWN file under apps/web/src/components/, rendered above ModelProfileSection (Drawers.tsx:120): rows from GET /v1/projects/{id}/gateway-endpoints, an add form (name, base_url, api_key as a password input), a per-row Test connection calling MEP-4's probe route and rendering the verbatim result, and a Make default action. 2. The key is write-only in the UI. After save the row shows the redacted key_hint plus an explicit 'replace key' affordance. Never populate a password input from a GET response — that is how a redacted secret becomes a visible one. 3. Delete the hardcoded PROFILE_NAMES (Drawers.tsx:118) and drive the profile switcher from the templates query already present at :124-127. 4. Reachability becomes durable rather than ephemeral: GatewayModelOut gains reachable and last_probe_error, fed from the row MEP-4 persists;

**Criteria:**

- The UI can express a model endpoint at all
  <br>`grep -rn 'gateway-endpoints' apps/web/src | wc -l returns >= 1 (returns 0 today), and the new component file contains both a base_url input and a password-typed key input.`
- No hardcoded profile names remain in the web app
  <br>`grep -rn 'PROFILE_NAMES' apps/web/src | wc -l returns 0. Returns 2 today.`
- The web app still builds and lints clean
  <br>`pnpm -C apps/web build (tsc --noEmit && vite build) exits 0 and pnpm -C apps/web lint exits 0.`
- A bad endpoint shows the real error, not a generic failure
  <br>`Playwright test adds an unreachable URL, clicks Test, and asserts the visible text contains the transport error class rather than a generic 'failed'. FAILS TODAY: no such UI exists.`
- A model proven unreachable cannot be selected
  <br>`Playwright test asserts the option is disabled via toBeDisabled(), not via screenshot comparison. FAILS TODAY: GatewayModelOut has no reachability field.`
- The endpoint key never reaches the browser
  <br>`Playwright spec intercepts every response on the settings route and asserts none contains the known secret string.`

**Review.** Mutation on the leak check first, because it is the one with consequences: temporarily have the API return the plaintext key, confirm the Playwright secret-interception assertion fails, revert. Then mutation on reachability: force reachable: true for every model and confirm the disabled-option test fails.
<br>**Iterate.** v2 moves this off the slide-over drawer and onto the board as a settings pane (backlog B1), where per-plugin settings cards have to land anyway. That is also the moment packages/aleph-a2ui/src/aleph_a2ui/settings_card.py gets its first consumer — a 279-line JSON-Schema-to-A2UI settings-surface generator with a 194-line test suite and, verified by grep, ZERO importers outside its own tests, whose canonical test fixtur…
<br>**Depends on:** WS-MEP-4
<br>**Risk.** Moderate, mostly about placement. Drawers.tsx is 742 lines and the backlog calls it simultaneously the settings drawer B1 replaces, the most drifted file in the tree, and where A4's per-plugin cards must land. Building a substantial new section inside a file already scheduled for replacement wastes the work;


#### WS-MEP-6 · Unfreeze the agent: it uses the project's models, not the ones it was born with

**What it is.** When Aleph starts, it reads ONE globally-named model profile from the database, builds the chat assistant and its six helper agents around those exact models, compiles the whole thing once, and never changes it (apps/api/src/aleph_api/lifespan.py:88-97 feeding :102-107, then :119-124 through apps/api/src/aleph_api/copilotkit_endpoint.py:47-60). Changing a project's models in Settings has no effect on the assistant whatsoever — and the assistant has a tool that tells the user the opposite, returning 'New LLM/agent calls use that profile's models' (apps/api/src/aleph_api/copilot_agent.py:1287-1291).

**Why.** Two of this cluster's items are dead letters without it. MEP-4 lets a project pick a different endpoint — the assistant would keep talking to the boot endpoint regardless. MEP-7 tunes the harness per model — with one frozen model set there is nothing to vary. Independently this is a correctness bug a user can see: a tool reporting a change it did not make is worse than a tool that fails, because it teaches the user to trust a false report.

**How.** 1. Turn build_assistant_deep_agent (copilot_agent.py:1628+) into a factory keyed by a resolution signature: (endpoint_id, hash of the project's bindings_jsonb). Cache compiled graphs in a BOUNDED LRU. Two projects sharing an endpoint and bindings share one graph; a project that rebinds gets a fresh one on its next turn. 2. The mount is the obstacle, not the factory. setup_copilotkit (copilotkit_endpoint.py:47-60) hands add_langgraph_fastapi_endpoint a single compiled graph via LangGraphAGUIAgent(graph=graph). Replace it with a thin route that resolves the project from the request using the SAME extractor apps/api/src/aleph_api/middleware/agent_scope.py already uses — the one apps/api/tests/unit/test_agent_thread_scope.py::test_thread_parsers_agree pins against the agent's own thread-id parser — then selects the graph from the factory and delegates. Wrap ag_ui_langgraph; do not fork it. 3.

**Criteria:**

- Two projects bound to different models actually use different models
  <br>`Integration test: project A bound to model 'alpha', project B to 'beta', one turn each against MEP-3's fake; assert the fake saw one request naming alpha and one naming beta. FAILS TODAY: both name the boot template's model.`
- Changing a binding takes effect without a restart
  <br>`Integration test: PATCH /model-profile changes the synthesis binding, then the NEXT agent turn uses the new model. FAILS TODAY: the graph is compiled once at lifespan:119-124.`
- Per-project graphs do not leak memory
  <br>`Test asserting the compiled-graph cache holds at most N entries after 50 distinct (endpoint, bindings) signatures.`
- No model name is hardcoded on the agent path
  <br>`grep -n 'claude-sonnet-4-6' apps/api/src/aleph_api/copilot_agent.py | wc -l returns 0 — the fallback becomes a stated 'no model bound' error rather than a guessed id. Returns 1 today (line 1436).`
- The existing gateway guarantees survive the refactor
  <br>`resolve_agent takes its endpoint from the project's gateway_endpoints row, and `apps/api/tests/unit/test_subagents.py::test_subagent_model_points_at_the_resolved_endpoint_not_the_boot_setting` asserts the built model points at the RESOLVED endpoint where it differs from settings.litellm_base_url. Both named tests were green before this workstream and are indifferent to whether any endpoint is resolved, so 'both still pass' cannot fail — the clause 'now against a resolved endpoint' is the whole criterion and needs its own case.`
- The set_model_profile tool's own claim is true of the system
  <br>`Test switches the profile, then asserts the next turn's model matches the new profile — i.e. the sentence the tool returns is verified, not just its wording. FAILS TODAY.`

**Review.** Mutation on the cache key, which is where this fails silently rather than loudly: drop endpoint_id from the signature and confirm the two-project isolation test fails; drop the bindings hash and confirm the PATCH-takes-effect test fails; restore both.
<br>**Iterate.** v2 makes model resolution observable instead of inferable: GET /v1/projects/{id}/agent/resolution returning which endpoint, which model per capability, which harness profile and which cache generation the next turn will use. Today that information exists only inside a closure created at boot, which is why 'the assistant is using the wrong model' is currently undiagnosable without reading container logs.
<br>**Depends on:** WS-MEP-4
<br>**Risk.** The dominant risk is authentication. Resolving a project per request on the agent endpoint re-touches the surface where /copilotkit previously sat on an auth skip list and took its project scope from a client-supplied thread id — a defect CLAUDE.md documents as fixed by two independent defences, both of which must survive.


#### WS-MEP-7 · HarnessProfile: a different prompt and tool set for a different model

**What it is.** Aleph sends the same 6.5k-character instruction block and the same 11 orchestrator tools to every model, whether that is a frontier model or a 7B model on a laptop. deepagents — the library Aleph already runs, version 0.6.6 — ships a mechanism for exactly this: register a 'harness profile' against a model and it can replace or extend the system prompt, rewrite individual tool descriptions, remove tools the model handles badly, and edit the helper agents' prompts, all loadable from a YAML file.

**Why.** The reason this belongs in this cluster rather than anywhere else: once MEP-4 lets an operator point a project at Ollama or vLLM, 'one prompt for every model' stops being a tuning nicety and becomes the thing that makes small models unusable. And the lookup key is the non-obvious fact that decides the entire design.

**How.** 1. Ship the context-window fix first; it is small and depends on nothing. _gateway_chat_model (copilot_agent.py:1483-1514) builds ChatOpenAI with no profile= argument. Pass profile={'max_input_tokens': <binding.max_input_tokens>} from the resolved ModelBindingIn, which already carries that field (packages/aleph-core/src/aleph_core/schemas/model_profile.py:58). Verified in this venv: ChatOpenAI accepts the kwarg on langchain-core 1.4.8, and deepagents.middleware.summarization.compute_summarization_defaults then returns ('fraction', 0.85) instead of ('tokens', 170000). create_summarization_middleware is applied to every subagent at deepagents/graph.py:594, so this affects seven agents, not one. 2. Profiles as operator-editable data.

**Criteria:**

- Summarisation fires at the right point for a small-window model
  <br>`Unit test asserting compute_summarization_defaults(_gateway_chat_model(...)) returns ('fraction', 0.85) for a binding declaring an 8192-token window. FAILS TODAY — verified by running it: ChatOpenAI(...).profile is None and the function returns ('tokens', 170000).`
- No profile is ever registered under a bare provider key
  <br>`Unit test asserting that after registration _get_harness_profile('openai') is None while _get_harness_profile('openai:<a-discovered-id>') is not. FAILS TODAY: grep -rn 'register_harness_profile' apps packages returns 0 lines — nothing is registered at all.`
- A model without function calling gets a smaller tool set
  <br>`Test builds a graph for a model discovery reports as supports_function_calling: false and asserts the resulting tool set is smaller BY NAME, listing which tools were removed — not by count alone.`
- Registration provably precedes graph construction
  <br>`Test registers a sentinel profile whose prompt suffix must appear in the built agent's assembled system prompt; moving registration after create_deep_agent makes it fail.`
- A malformed profile file fails loudly at boot
  <br>`Test asserting a broken YAML file raises with the filename in the message rather than being skipped; plus uv run pyright and uv run ruff check . exit 0 with the loader included.`
- The effect of a profile is a recorded number, not an opinion
  <br>`Run aleph_evals over a fixed task set with and without the small-model profile against MEP-3's fake configured as a small model, reporting tool-call error rate for BOTH arms. The check fails if either arm produced no number, or if the profiled arm's tool-call error rate is worse than the control arm's by more than 5 points. The original wording — 'the number is produced and recorded, not that it clears a threshold' — admits in its own text that it cannot fail: a run that reports 100% errors satisfies it. A floor relative to the control arm is still not a quality bar, but it is falsifiable.`

**Review.** Mutation on the key shape first, since it is the failure this design exists to prevent: register a profile under 'openai' instead of 'openai:<id>', confirm the bare-key test fails, and — more importantly — confirm that an unrelated second model now picks up that profile's prompt suffix, demonstrating the blast radius on the record. Restore.
<br>**Iterate.** v2 turns profiles from operator-authored into measured: run the eval per profile class on a schedule, record tool-call error rate and token cost per class in the same ledger MEP-1 made trustworthy, and require a profile change to be justified by a number.
<br>**Depends on:** WS-MEP-6
<br>**Risk.** Two. (1) Harness profiles change prompts, and prompt changes are the least testable thing in the system — a profile that helps by opinion and hurts by measurement is the default outcome, not the edge case. That is why criterion six is a number rather than a threshold: refuse to ship profile content with no measurement attached, even a crude one.


---

### UI/UX: settings as panes, plugin cards, and eliminating drift and dead code


#### WS-UI-1 · Web quality gates that can fail, and the dead code they let you delete

**What it is.** Nothing in the automated build can currently notice a React file that nothing imports, a CSS rule nothing uses, or a hardcoded colour. The `web` job in CI (.github/workflows/ci.yml:142-158) runs a type check and a production build; both pass happily on ~900 lines of code that never renders. This workstream writes three small checking scripts — 'is every file reachable from the app's entry point', 'is every shared CSS class actually used', 'how many hardcoded colours/corners/shadows are there' — wires them into CI, and then deletes what they find.

**Why.** CLAUDE.md names 'written correctly and read by nothing' as this codebase's dominant defect class, and there are Python sweeps for it. There are none on the web side, and the result is measurable: 4 of 58 modules under apps/web/src are unreachable from main.tsx (A2UISurfaceView.tsx 213 lines, ActivityCard.tsx 349, ReadingRegion.tsx 110, A2UIRightPanel.tsx 44), and a fifth (LeftPanel.tsx, 175 lines) survives only because a one-line type `DrawerKind` lives inside it — ContextBar.tsx:116 already carries the comment 'it…

**How.** 1) `scripts/check-web-dead-code.sh` — walk static `import` / `export … from` / dynamic `import()` edges from apps/web/src/main.tsx, resolving the `@/` alias from apps/web/vite.config.ts. Any .ts/.tsx under apps/web/src not reached is an error. Treat components/Icons.tsx as a second entry point (Rail.tsx:56 does `Icons[kind.icon]`, a runtime string lookup) and check icon keys separately against the `icon` values in packages/aleph-a2ui/src/aleph_a2ui/pane_registry.py plus the drawer tuple at Rail.tsx:97-105. An allowlist file apps/web/.deadcode-allow requires a backlog id and an ISO date per entry, so an exemption is a dated decision rather than silence. 2) Delete a2ui/A2UISurfaceView.tsx, components/A2UIRightPanel.tsx, components/ReadingRegion.tsx;

**Criteria:**

- Every module under apps/web/src is reachable from the entry point, or is allowlisted with a backlog id and a date. FAILS TODAY: 4 unreachable.
  <br>``./scripts/check-web-dead-code.sh` exits 0; `awk 'NF && !/^#/' apps/web/.deadcode-allow` shows every entry carries a backlog id and an ISO date`
- At least 890 lines removed from apps/web/src and styles.css, with no replacement file over 40 lines.
  <br>``git diff --stat main -- apps/web/src | tail -1` shows deletions ≥ 890`
- No CSS class declared in styles.css's @layer components block is unused. FAILS TODAY: 9 unused selectors.
  <br>``./scripts/check-web-dead-css.sh` exits 0`
- The drift ratchet passes on main and fails when one violation is added.
  <br>``./scripts/check-web-drift.sh --ratchet` exits 0; then `perl -pi -e 's/border-r border-line bg-surface/border-r border-line rounded-lg bg-surface/' apps/web/src/components/Rail.tsx && ./scripts/check-web-drift.sh --ratchet` must exit non-zero naming rounded-lg; `git checkout apps/web/src/components/Rail.tsx``
- All three scripts run in CI.
  <br>``grep -c 'check-web-' .github/workflows/ci.yml` returns 3`
- The three new checks are covered by the self-check harness that proves a check can fail.
  <br>``./scripts/acceptance.sh --self-check` names check-web-dead-code, check-web-dead-css and check-web-drift and reports each as verifiably failing on mutation`

**Review.** One mutation per script. (a) Write apps/web/src/components/Orphan.tsx exporting a component nobody imports — the dead-code check must exit 1 naming it; delete. (b) Add `.zombie { color: red }` inside @layer components — the dead-CSS check must exit 1; delete. (c) Add one `rounded-lg` and one `text-slate-500` — the ratchet must exit 1 twice, naming both counters; revert.
<br>**Iterate.** v2 replaces the hand-rolled reachability walk with `knip`, once its config is tuned for this app's dynamic lookups (Icons[kind.icon] at Rail.tsx:56, the catalog registration table at aleph-catalog-v09.tsx:592). knip also finds unused exports, which the walk does not — CATALOG_VERSION and ACTION_NAMES (a2ui/catalog.ts:7,35) have zero importers and would be caught.
<br>**Depends on:** —
<br>**Risk.** Medium-low. The real risk is deleting something reached dynamically rather than by import. The A2UI catalog registers its views in a literal table (aleph-catalog-v09.tsx:592) so those are import-reachable, but Icons[kind.icon] is a runtime string lookup and the walk must not conclude an icon is dead — handled by the second entry point plus the registry cross-check.


#### WS-UI-2 · A UI test harness, and a browser smoke that keeps the console clean

**What it is.** There is currently no way to test any part of the interface. `git ls-files tests` returns exactly 7 Python files; there is no Vitest, no Playwright, no test script in apps/web/package.json, and the Playwright suite that used to exist was deleted in the harness reset while its data-testid hooks remain scattered through the code. This workstream installs two things: Vitest with React Testing Library, which renders one component in a fake DOM and asserts what it shows, and a Playwright smoke run that boots the real docker compose stack, opens a project and walks the main paths — asserting, among other things, that t…

**Why.** Every other workstream in this cluster changes behaviour: a click that did nothing now opens a pane, a settings field now saves, a chart's text becomes readable. Without a runner, 'it works' is a screenshot and a memory. The owner's bar — 'every part needs to function as expected' — is a claim only a check that can fail can defend.

**How.** 1) Add vitest, @vitest/coverage-v8, @testing-library/react, @testing-library/user-event and jsdom to apps/web/package.json devDependencies, with `"test": "vitest run"`. Vitest reads vite.config.ts, so the `@/` alias comes free. 2) Day 1 is a spike, not a commitment: prove that @a2ui/react/v0_9's MessageProcessor + A2uiSurface render in jsdom. If they will not, fall back to testing the views directly — they take a plain `{component, onAction}` prop (RendererProps in a2ui/components/_shared.tsx) and are ordinary React — and move the binder coverage to Playwright. 3) First six component tests, chosen to pin what this cluster is about to change: Block (a verb whose handler is absent must not render a button — Block.tsx:171 already guards, Board.tsx:254-256 defeats the guard with `() => undefined`);

**Criteria:**

- A component test suite exists and runs. FAILS TODAY: apps/web has no test script and no runner.
  <br>``pnpm -C apps/web test 2>&1 | tail -3` reports 'Tests' with ≥6 passed and exits 0`
- The tests can fail on a real regression, not just pass.
  <br>`Delete the body of the onClick at apps/web/src/a2ui/components/ClaimCard.tsx:48, run `pnpm -C apps/web test`, confirm >= 1 failure, all of them in specs that exercise ClaimCard's open action (3 today, in `a2ui/navigate.test.tsx` — 'exactly 1' was measured before the spec grew), then `git checkout` the file`
- A browser smoke opens a project and tiles two panes against the live stack.
  <br>``./scripts/bootstrap-local.sh && pnpm -C tests/playwright exec playwright test workspace-three-panel-shell.spec.ts` exits 0`
- The smoke fails on any console error or warning. FAILS TODAY on the Lit dev-mode notice.
  <br>`Inject one `console.error` during workspace load and confirm the smoke fails; remove it; confirm 0 console errors or warnings. The lit-html premise is gone — `vite.config.ts` has no `resolve.conditions` and `lit-html` appears nowhere in the tree, so a spec 'failing naming lit-html' cannot happen either way.`
- Vitest runs in CI.
  <br>``grep -c 'apps/web test' .github/workflows/ci.yml` returns ≥1`
- A coverage floor is recorded so later workstreams cannot silently drop it.
  <br>``pnpm -C apps/web test:coverage` prints a statements percentage and exits 0 against the four thresholds in `apps/web/vitest.config.ts`; the number is recorded in `docs/acceptance.md` E12. NOT `pnpm … test -- --coverage`: pnpm 11 passes `--` through to vitest verbatim, so the flag is swallowed and the command prints no percentage and exits 0 — the criterion's literal form was satisfied by a run that measured nothing.`

**Review.** Mutation on each of the six component tests: break exactly what each asserts — remove a rail entry from the mocked /panes payload, invert Block's verb-render condition, drop a field from the settings fixture — and confirm precisely that test fails and no other. For Playwright, add `console.warn("mutation")` to App.tsx and confirm the smoke goes red; remove it.
<br>**Iterate.** v2 adds two things the harness makes cheap. (a) Visual regression: Playwright screenshots of the Board and one pane in both themes, which turns workstream G's token drift from a grep into an image diff. (b) An axe-core accessibility pass in the smoke, which immediately catches the missing focus trap and Escape handler on Drawers.tsx:29-30 (which declares role='dialog' aria-modal='true' and implements neither) and the…
<br>**Depends on:** WS-UI-1
<br>**Risk.** Medium. Two named risks. (1) If A2UI will not render in jsdom, the fallback of testing views directly loses coverage of the binder — which is exactly where UI-3's defects live — so UI-3's criteria would have to move to Playwright. Decide this on day 1, not in week 3. (2) Playwright against docker compose is the classic flaky-CI source;


#### WS-B1a · Panes a plugin can actually add

**What it is.** The workspace is a canvas of panes — Wiki, Library, Notes and so on. The server decides what panes exist and the client just draws whatever it is given, which is the right shape. But the door is only half built. The server has a registry with an `extend()` method its own file describes as 'the seam a plugin uses' (pane_registry.py:16, :70), and if a plugin used it the app would break: the code that builds a pane's content is a hardcoded if/elif chain that raises NotFound on an unknown name (routes/surfaces.py:138-166), and it is called inside an unguarded loop in the single streaming connection that feeds every o…

**Why.** This is the load-bearing prerequisite for everything else in this cluster and for the product thesis. B1 (settings as panes) and A4 (per-plugin settings cards) both mean registering new pane kinds, and both land directly on the crash. More broadly, 'an agent that authors plugins for itself' is not achievable while adding a surface means editing an if/elif chain in a 1028-line route file.

**How.** 1) Give the registry a builder alongside the kind. Add `SurfaceBuilder = Callable[[Session, UUID, dict[str,str], str], Awaitable[list[dict]]]` and register it with the PaneKind so extend() takes both. Replace _build_tab_messages (routes/surfaces.py:138-166) with a registry lookup; make the artifacts→library alias an explicit registry entry rather than an `in` test. 2) Isolate faults per pane: wrap each pane's build in _build_all (surfaces.py:246-251) in try/except and, on failure, emit a one-component error surface for that pane naming the pane and the exception class, instead of letting it escape the SSE generator. This is what stops one plugin killing the workspace. 3) Parse declared params, not just page_id. _parse_pane_specs (surfaces.py:186-199) hardcodes `if k == "page_id"`; read PaneKind.params (pane_registry.py:41) and pass a dict through.

**Criteria:**

- A pane registered only through PANE_REGISTRY.extend() streams a real surface. FAILS TODAY: _build_tab_messages raises NotFound.
  <br>`An integration test registers PaneKind(id='probe_pane', title='Probe pane', icon='notes') with a builder, opens GET /v1/projects/{id}/surfaces/stream?panes=wiki,probe_pane and asserts two createSurface frames arrive`
- A pane whose builder raises does not kill the other panes. FAILS TODAY: the exception escapes _build_all and ends the SSE generator.
  <br>`An integration test registers a builder that raises, requests ?panes=wiki,bad_pane, and asserts the wiki surface still arrives and bad_pane arrives as an error surface`
- A pane id that is not the lowercased title round-trips. FAILS TODAY: paneKey mints 'claim map', dropped by _parse_pane_specs.
  <br>`Register PaneKind(id='claim_map', title='Claim map'), click it in the rail in the Playwright smoke, and assert a createSurface with surfaceId='claim_map'`
- check-pane-registry.sh fails on the six hardcoded pane names present today, and passes once they are gone. FAILS TODAY only in the sense that it wrongly passes.
  <br>`After the sweep is made case-insensitive, run it on unmodified main and confirm exit 1 naming CopilotChatSurface.tsx and workspace-ui.tsx; run after the client fix and confirm exit 0`
- Opening a pane does not re-create the already-open surfaces.
  <br>`A Vitest test asserts `new MessageProcessor` is constructed once across a pane-set change on SurfaceStreamProvider. One change, not two — that is what the test performs.`
- Declared params reach the builder. FAILS TODAY: only page_id is parsed.
  <br>`An integration test requests ?panes=grounding:claim_id=<uuid> and asserts the grounding builder receives claim_id`

**Review.** Mutation on the seam itself. Delete the per-pane try/except and confirm criterion 2 goes red. Revert the registry lookup to the if/elif chain and confirm criterion 1 goes red. Reintroduce one "Wiki" literal in CopilotChatSurface.tsx and confirm the upgraded sweep catches it (it does not today).
<br>**Iterate.** v2 makes pane registration per-project, which GET /v1/projects/{id}/panes already anticipates in its docstring ('once a plugin can be enabled per project, "what can this workbench do" stops having one global answer', surfaces.py:56-60), and invalidates the ['panes', projectId] react-query key on activation instead of staleTime: Infinity (workspace-ui.tsx:69).
<br>**Depends on:** WS-UI-2
<br>**Risk.** Medium. The id/title migration touches the agent's focus_tab tool and the nav.tab contract the server produces (a2ui_handlers.py:751-782), so an incomplete migration leaves navigation that silently does nothing — the exact defect class this cluster exists to remove.


#### WS-A4 · The plugin settings contract, wired end to end

**What it is.** A plugin should be able to say 'here is my configuration, as a schema' and get a working settings screen without shipping any browser code. That generator already exists and works: packages/aleph-a2ui/src/aleph_a2ui/settings_card.py is 279 lines, unit-tested (packages/aleph-a2ui/tests/test_settings_card.py), a pure function, and emits only primitives the client already renders (the basic catalog is merged in at aleph-catalog-v09.tsx:592). It has zero importers outside its own tests.

**Why.** The backlog files A4 as NOT BUILT; it is half built, and the built half is the expensive one. What is missing is precisely the 'ship a consumer with every producer' gap CLAUDE.md names as dominant. Beyond that, this is the mechanism that makes the plugin thesis visible to a person: an agent-authored capability nobody can configure is one you trust blindly or edit .env for.

**How.** 1) Describe the plugin. Add a `UIContribution` dataclass in aleph-runtime carrying plugin_id, title, description, config_schema (JSON Schema), panes (tuple[PaneKind, ...]) and trust (Literal['core','verified','authored'] — the three tiers A4 asks for). Do NOT hang it on CapabilitySpec (packages/aleph-kernel/src/aleph_kernel/spec.py:75) if that forces aleph-kernel to know about A2UI; the strict DAG in CLAUDE.md is real and aleph-kernel is leaf-ward. A registry in aleph-runtime keyed by capability name is the safer shape. 2) Store the values. New `plugin_settings` table in aleph-db: project_id, plugin_id, values JSONB, plus CommonColumns (created_at, updated_at, created_by) per the standing rule that every row carries them — `access_scope` is gone (`decisions.md` D7). Unique on (project_id, plugin_id). Alembic revision under apps/api/alembic/versions/; `cd apps/api && uv run alembic check` must show no drift.

**Criteria:**

- A plugin declaring only a JSON Schema gets a working settings surface with zero UI code. FAILS TODAY: settings_card has no caller and no pane.
  <br>`An integration test registers a contribution whose config_schema has a string, a boolean, an enum and a number, opens ?panes=settings:plugin=probe, and asserts the frames contain TextField, CheckBox, ChoicePicker and Slider components`
- Saving persists and is auditable in one transaction. FAILS TODAY: no handler, no table.
  <br>`POST the plugin.settings.save action, then assert exactly one plugin_settings row with the submitted values AND one ActionLedgerEvent of kind plugin_settings.update`
- A value that violates the declared schema is refused, not stored.
  <br>`Submit {"max_concurrent_runs": "banana"} against {"type":"integer"}; assert a refusal response and zero rows written`
- A password-format field never reaches plugin_settings.values.
  <br>`Submit a schema with `format: password` or `writeOnly: true`; assert `SecretFieldRefused` at the generator, so no such field can render and therefore no such value can be submitted. Secret-SHAPED keys that slip past the schema are redacted before persistence, and `SELECT values::text FROM plugin_settings` contains no plaintext. Not a credential reference in the JSONB: a settings value reaches `card_actions` and the append-only ledger, so the design is refusal, not storage — credentials go through `ConnectorCredential`. `packages/aleph-a2ui/tests/test_secret_redaction.py::test_a_schema_declaring_a_secret_is_refused_before_it_can_be_submitted` and `::test_a_secret_by_name_only_still_reaches_the_screen_and_is_redacted_on_write` pin both halves.`
- settings_card.py has a non-test importer. FAILS TODAY: returns 0.
  <br>``grep -rn 'from aleph_a2ui.settings_card import' --include='*.py' apps packages | grep -v tests | wc -l` returns ≥1. The import form, not the bare name: twelve of the twenty bare-name hits are docstrings explaining what `settings_card.py` is, so the loose grep counts the prose documenting the fix`
- The three trust tiers are observable at the API. **The 'and change behaviour' half is WITHDRAWN — `docs/decisions.md` D13.**
  <br>``GET /v1/projects/{id}/plugins` returns each contribution's trust value. It does NOT return `requires_approval` at any tier, and that is pinned rather than merely absent: `tests/integration/test_plugin_settings_contract.py::test_a_save_does_not_branch_on_the_declared_trust` asserts it over the whole serialised response at all three tiers, and `::test_the_agent_cannot_reach_the_settings_save_action` is an AST pass proving the reachable action set is decidable and does not contain it. Withdrawn because `plugin.settings.save` is already OWNER-gated, so approval would ask an owner to approve their own change, and because no agent tool can dispatch an arbitrary card action — the tier of the PLUGIN is not the authority of the ACTOR. D13 records what would reopen it.`
- No schema drift is introduced.
  <br>``cd apps/api && uv run alembic check` exits 0 after the new migration`

**Review.** Mutation on each half of the producer/consumer pair: (a) remove the plugin.settings.save registration → criterion 2 goes red; (b) remove the schema validation → criterion 3 goes red; (c) delete the pane builder registration → criterion 1 goes red.
<br>**Iterate.** v2 handles the schema shapes settings_card.py currently renders as a visible 'not editable here' line (_unsupported, settings_card.py:148-155) — arrays and nested objects are the common ones — and adds conditional field visibility so a field appears only when another is set.
<br>**Depends on:** WS-B1a
<br>**Risk.** Medium. Two things to get right or regret. (1) Where secrets live: putting them in plugin_settings.values would be fast and would put plaintext credentials in a JSONB column that both ledger diffs and settings surfaces read. Do not.


#### WS-B1 · Settings becomes panes, and the drawer dies

**What it is.** Settings today is a slide-over panel that covers the workspace: apps/web/src/components/Drawers.tsx, 742 lines, the largest and most drifted file in the web app (12 rounded-*, 1 shadow-*, 24 hardcoded palette colours). It sits outside the pane model everything else obeys, it claims to be a modal dialog (role='dialog' aria-modal='true' at Drawers.tsx:29-30) while implementing none of the behaviour — a repo-wide grep for 'Escape' across apps/web/src returns 0 — and it carries its own copies of two lists the server owns.

**Why.** Two reasons, and the second is structural. First, consistency: a workbench where one surface obeys different rules than the rest has a bolted-on part, which is what the owner asked to remove. Second: per-plugin settings cards have nowhere to land in a hand-written drawer. A drawer with a SettingsBody function per section means a new plugin needs a new React function; a pane built from the plugin's declared schema means a new plugin needs nothing. The drawer is the thing that makes settings unpluggable.

**How.** 1) Register the panes: PaneKind(id='settings', ...), plus per-plugin `settings:plugin=<id>` via B1a's param parsing. Rail.tsx:97-105's four-tuple drawer list (settings, logs, notifications, profile) collapses into the registry. `logs` becomes a real pane — GET /v1/projects/{id}/ledger/verify (ledger.py:52) exists with no caller and the hash chain CLAUDE.md treats as a core invariant has no UI at all. `notifications` and `profile` become panes or are honestly deleted if they hold nothing. 2) Port the sections: ModelProfileSection (Drawers.tsx:120-204) and CapabilityBindings (:211-359) become one settings pane fed by the existing routes; ConnectorsSection (:360-457) becomes another. Where a section's shape is expressible as JSON Schema, use A4's generator rather than hand-writing it — that is the test of whether A4 is real.

**Criteria:**

- settings is a pane kind served by the registry. FAILS TODAY: 7 panes, none of them settings.
  <br>``curl -s localhost:8000/v1/projects/$P/panes | jq -r '.panes[].id'` includes 'settings'`
- Drawers.tsx no longer exists and nothing imports it.
  <br>``test ! -f apps/web/src/components/Drawers.tsx` and `git grep -n 'components/Drawers' -- 'apps/web/src' 'apps/*/src' 'packages/*/src' | wc -l returns 0 (scoped to SOURCE: over the whole repo nine files carry the string, four of them the prose documenting the deletion and one of them this criterion, so the repo-wide form is red forever)`, with `./scripts/check-web-dead-code.sh` still exiting 0`
- Zero client-side copies of server-owned lists. FAILS TODAY: 3+ hits.
  <br>``git grep -n 'PROFILE_NAMES\|"aleph-dev"\|Capability order mirrors' apps/web/src | wc -l` returns 0`
- Escape closes every modal and focus is trapped inside it. FAILS TODAY: `git grep -c '"Escape"' apps/web/src` returns 0 files.
  <br>`A Playwright test opens the source-upload modal, presses Tab five times and asserts focus never leaves the dialog, presses Escape and asserts it closes and focus returns to the trigger`
- The theme can be changed from inside a project. FAILS TODAY: no control is rendered inside the workspace.
  <br>`A Playwright test opens a project, activates the theme control and asserts document.documentElement.dataset.theme flipped`
- Settings panes tile beside content rather than covering it.
  <br>`A Playwright test opens Wiki and Settings and asserts two Blocks on the Board with distinct surfaceIds and no fixed-position overlay`

**Review.** Mutation: remove the settings registry entry → criterion 1 red; remove the focus-trap hook → criterion 4 red. The substantive review is a regression walk, because the failure mode here is a feature quietly disappearing: write the old-section → new-pane mapping table in the PR description first, then walk it item by item against the running stack — model profile change lands (verify with GET /v1/projects/{id}/model-pr…
<br>**Iterate.** v2 makes a settings pane addressable by URL so a layout including it is shareable and restorable, which needs pane layout persisted server-side — currently WorkspaceUIProvider state is in-memory only (workspace-ui.tsx:181). v3 adds search across all plugin settings, which becomes worth building the moment a second suite exists and is trivial once every setting is a declared schema field rather than a hand-written for…
<br>**Depends on:** WS-A4
<br>**Risk.** Medium-high, and the risk is regression rather than difficulty. Drawers.tsx is where model profile, capability binding, connectors, credentials, cost rollup, members, the ledger view and the profile all live; porting it piecemeal is how one of those quietly vanishes.


#### WS-UI-3 · The A2UI prop contract, in both directions, across all producers

**What it is.** The interface's data flows through a declarative catalog: the server sends component names and props, and the browser resolves them against a schema. If the server sends a prop the browser's schema does not declare, the browser drops it silently — the payload is correct, the view reads nothing, and nothing errors. There are three descriptions of what a component's props are — the canonical catalog.json, the browser's zod schemas, and what the Python producers actually emit — and they disagree in both directions. The one CI check for this reads one producer file out of three and looks only one way;

**Why.** This is the defect CLAUDE.md rule 5 was written for, one layer deeper. It also blocks the plugin thesis: catalog.json is what a plugin's own catalog would merge into (backlog A3), and it currently misdescribes the components it already owns. Four of the five surface entries share ZERO props with the schema that actually binds — catalog.json says WikiSurface takes {current_page_id, filters, view_mode}; it actually takes {pages, open, categories, health}.

**How.** 1) Rewrite scripts/_lib/surface_bindings.py. run() (:118-127) reads exactly one producer file, packages/aleph-a2ui/src/aleph_a2ui/components/surfaces.py, and producer_props() detects only {"path": …} bindings — so apps/api/src/aleph_api/routes/surfaces.py (1028 lines, where the real surface builders live) and packages/aleph-a2ui/src/aleph_a2ui/components/cards.py (248 lines, the literal-prop card path) have zero coverage. Read all three and detect literal props as well as path bindings. 2) Compare three ways, not one: producer → zod (a bound prop the client drops), catalog.json → zod (the canonical file disagreeing with the running client), and zod → view (a prop declared and never read — 28 today, including FindingCard.evidence_refs, which routes/surfaces.py:900 genuinely sends and FindingCard.tsx:6-11 does not destructure).

**Criteria:**

- The binding sweep covers every producer and every component. TODAY it reports 5 components and 11 bound props.
  <br>``./scripts/check-surface-bindings.sh` reports ≥21 components and ≥108 props inspected`
- Zero drift between catalog.json and the client zod schemas. FAILS TODAY: 21 mismatched props (9 catalog-only, 12 zod-only).
  <br>`The sweep's catalog↔zod comparison exits 0; reproduce today's number with the python comparison in this plan's approach`
- Zero props declared in a zod schema and never read by the view that owns them. FAILS TODAY: 28.
  <br>`The sweep's zod→view direction exits 0`
- The sweep fails on a newly introduced mismatch.
  <br>`Add "foo_id" to WikiSurface's props in catalog.json, run `uv run python scripts/gen_catalog.py && ./scripts/check-surface-bindings.sh`, confirm exit 1 naming WikiSurface.foo_id, then revert and re-run gen_catalog`
- Exactly one @a2ui/web_core version resolves in the lockfile. **CORRECTED 2026-08-22:** the count of 2 is a `packages:` entry plus a `snapshots:` entry for ONE version, which is how pnpm writes a single resolution — so "returns 1" was never achievable. State it as: exactly one distinct VERSION resolves — `grep -oE "@a2ui/web_core@[0-9]+\.[0-9]+\.[0-9]+" pnpm-lock.yaml | sort -u | wc -l` returns 1. **The criterion itself still FAILS:** two genuinely different versions resolve today, 0.9.0 and 0.10.0, and a component registered against one catalog cannot render under the other. Only the counting method was wrong, not the concern.
  <br>`The headline above is the check: `grep -o "@a2ui/web_core@[0-9.]*" pnpm-lock.yaml | sort -u | wc -l` plus `web-core-duplication.test.ts`, which pins the accepted pair. The old sub-bullet grepped `^ '@a2ui/web_core@` with ONE leading space; the lockfile indents with two, so it returned 0 whatever the truth was and contradicted the corrected headline directly above it`
- Every prop the agent is told about is bindable. FAILS TODAY: 2 (ChartCard.dataset_version_id, ApprovalCard.diff_card_id).
  <br>`The sweep cross-checks catalog.json's agent.props block against the zod declarations and reports 0 discrepancies`

**Review.** Mutation in all three directions, one each: add a producer prop with no client declaration; add a catalog.json prop with no zod entry; add a zod prop no view reads. The sweep must fail on each, naming the file and the prop. Then confirm the fixes are real rather than cosmetic: for each of the 9 catalog-only props kept, open the surface in the browser and confirm the value renders;
<br>**Iterate.** v2 makes GET /v1/a2ui/catalog (apps/api/src/aleph_api/routes/cards.py:48 — a route with zero callers) do the job the generated file's own header already claims it does: 'The web app fetches the canonical schema from GET /v1/a2ui/catalog at startup and validates against it' (a2ui/catalog.ts:2-4). No such fetch exists.
<br>**Depends on:** WS-UI-2
<br>**Risk.** Medium. The lockfile de-duplication is the risky item: forcing @copilotkit/a2ui-renderer onto @a2ui/web_core@0.10 may break its rendering, and the installed CopilotKit is 1.58 — the 'v2' in several source comments (lib/copilot.tsx, CopilotChatSurface.tsx:1, ThemeToggle.tsx:13) is the /v2 subpath of the 1.58 package, not a major version.


#### WS-UI-4 · Every control does something

**What it is.** A number of things in the interface render, look interactive, and do nothing when used. Every block on the canvas has three footer buttons — Keep, Again, Sources — wired to empty functions (Board.tsx:254-256, `() => undefined` three times). Every block shows a four-of-four trust meter and a 'DECL' band badge that are hardcoded literals (Board.tsx:248-249), so the entire trust display is identical on every block regardless of what it holds. Clicking a claim card's 'Open claim' does nothing.

**Why.** This is the owner's bar stated literally: 'every part needs to function as expected'. It is also the highest-visibility instance of this codebase's named defect class, because it fails in front of a person rather than in a log. The Grounding case is the sharpest: it is what makes the claim → chunk → char-span chain — the thing CLAUDE.md describes as the point of the durable knowledge layer — visible, and scripts/check-pane-registry.sh carries a comment asserting this exact bug was fixed. It was not;

**How.** 1) Grounding, reachable: add a `claim` branch to _open returning the grounding pane id and the claim id as a declared param. With B1a's param parsing the page_id-carries-a-claim-id workaround at surfaces.py:928-929 goes away. 2) Board verbs: decide per verb rather than wiring three empty functions. 'Sources' has an obvious meaning — open the grounding pane for what this block shows, and pass a count through Block's already-declared `sources` prop (Block.tsx:72, never passed). 'Keep' means pin, and a pin path exists (`unpin` is registered at a2ui_handlers.py:1256). 'Again' means re-run the producer, which only some blocks have. Block already guards with `{onKeep && …}` (Block.tsx:171), so passing undefined is the correct fix for the rest, not an empty arrow.

**Criteria:**

- Clicking 'Open claim' opens the Grounding pane on that claim. FAILS TODAY: no claim branch in _open, so no navigation happens at all.
  <br>`A Playwright test clicks the control on a ClaimCard and asserts a Block with surfaceId grounding:claim_id=<uuid> appears`
- Zero no-op handlers in the web app. FAILS TODAY: 3.
  <br>``grep -rn '=> undefined' apps/web/src --include='*.tsx' --include='*.ts' | grep -v '\.test\.' | wc -l` (the unfiltered form counts nine test doubles and the Board.tsx:303 comment explaining the fix) returns 0`
- Zero ternaries whose two arms are identical string literals. FAILS TODAY: 2.
  <br>``python3 -c "import re,pathlib;p=re.compile(r'\\?\\s*(\"(?:[^\"\\\\]|\\\\.)*\")\\s*:\\s*(\"(?:[^\"\\\\]|\\\\.)*\")');print(sum(1 for f in pathlib.Path('apps/web/src').rglob('*.tsx') for m in p.finditer(f.read_text()) if m.group(1)==m.group(2)))"` returns 0`
- Zero inert hover/focus classes. FAILS TODAY: 7.
  <br>`A check that, for each className string, no hover:X or focus:X token has bare X in the same string; returns 0`
- Every registered action kind has an emitter. FAILS TODAY: 3 of 21 (clarify, mark_handedit, clear_handedit).
  <br>`For each name registered in build_action_router(), assert ≥1 occurrence outside a2ui_handlers.py, the two catalogs and tests`
- A note with no sections is editable and the badge tells the truth. FAILS TODAY: the write is skipped and 'saving' never resolves.
  <br>`An integration + Playwright pair creates a note with zero sections, types in the editor, reloads and asserts the body persisted; and a Vitest test asserts setSaved('saved') is not called before the mutation resolves`
- The trust display varies with the block. FAILS TODAY: every block is hardcoded trust='signed' = 4 of 4 bars.
  <br>`A Vitest test renders two Blocks from panes with different source/trust and asserts different lit-bar counts`

**Review.** Mutation, mostly by reverting: put `() => undefined` back on one verb → criterion 2 red; remove the claim branch → criterion 1 red; re-add one inert hover class → criterion 4 red. Then the part that is not optional here: a human click-everything pass with the stack running.
<br>**Iterate.** v2 turns that manual pass into a Playwright crawler that walks the accessibility tree, activates every enabled control on each surface, and asserts each produced either a network request, a DOM change, or a declared no-op annotation — which makes 'renders but does nothing' a check rather than a habit.
<br>**Depends on:** WS-B1a, WS-UI-3
<br>**Risk.** Low-medium. The main risk is scope: the click-everything pass will find more inert controls than the ones enumerated, and each is a small decision that can turn into a feature. Timebox it — anything that cannot be made to work quickly gets the control removed and a backlog entry, which is strictly better than shipping it inert.


#### WS-G · Design-token conformance: 187 to zero, ratcheted

**What it is.** The interface has a specification recorded in code — apps/web/src/styles/tokens.css: square corners (--radius: 0px at :67), hairline borders, no shadows, and colour reserved for state. Most components predate it and still carry rounded corners, drop shadows and Tailwind's default colour palette. A hardcoded text-slate-500 is not a cosmetic issue: it does not respond to the theme at all, so it renders identically on both grounds, which is why parts of the app look right in one theme and wrong in the other. This workstream drives the count to zero and turns workstream UI-1's ratchet into a hard gate.

**Why.** Prod-ready is the bar, and an interface that is correct in one theme and broken in the other is not. There is a compounding reason to do it last: the earlier workstreams delete or rewrite the worst offenders, so the paint gets dramatically cheaper. Drawers.tsx alone is 12 rounded + 1 shadow + 24 palette (37 of the 187) and B1 deletes it; UI-1 deletes ~21 more with ActivityCard, LeftPanel and the dead CSS block. Doing this first would mean carefully styling files that are about to be deleted.

**How.** Verified baseline, measured on main across apps/web/src/**/*.tsx: rounded-{sm..full} = 56, shadow-{sm..xl} = 9, Tailwind palette-scale classes = 122 — 187 under the backlog's own counting rules (the backlog says 180; the gap is DiffCard and WikiBodyMarkdown). Three categories the backlog's rules exclude and that are real drift: bare `rounded` = 50 (Tailwind's 0.25rem, still not --radius: 0), `var(--token, LITERAL)` fallbacks = 25, raw hex/rgba in .tsx = 27. All-in: 264 in .tsx plus 4 in styles.css's dead block (removed by UI-1). Work order after UI-1 and B1: WikiPageCard.tsx (3+11 rounded, 1 shadow, 20 palette, 430 lines), GroundingSurface.tsx (3+3, 0, 16, 194), _shared.tsx (2+4, 2, 2, plus 10 hex), then ApprovalCard, HypothesisMatrix, WikiBodyMarkdown, DiffCard.

**Criteria:**

- Zero strict-rule violations. FAILS TODAY: 187.
  <br>``grep -rEoh 'rounded-(sm|md|lg|xl|2xl|3xl|full)|shadow-(sm|md|lg|xl|2xl)|(bg|text|border|ring|from|to|via|divide|placeholder|decoration|outline|accent|caret|fill|stroke)-(slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-[0-9]{2,3}' apps/web/src --include='*.tsx' | wc -l` returns 0. **CORRECTED 2026-08-22:** the command was truncated mid-path in the plan file itself (`apps/…`), so it could not be run at all.
- Zero hardcoded fallbacks inside var(). FAILS TODAY: 25, disagreeing with each other about the accent.
  <br>``grep -rEoh 'var\(--[a-z-]+, *(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))\)' apps/web/src --include='*.tsx' | wc -l` returns 0`
- Zero bare `rounded` and zero raw hex/rgba in .tsx. FAILS TODAY: 50 and 27.
  <br>`The drift script's remaining two counters both return 0`
- The drift check runs in CI and fails on one reintroduced violation.
  <br>``./scripts/check-web-drift.sh --ratchet` exits 0 against the all-zero pin; add one `rounded-lg` to any component and it exits 1 naming the class and the file; revert. There is no `--zero` mode and there is not going to be — `check-web-drift.sh:18-21` records the decision that the ratchet subsumes it, so the criterion named a flag the script deliberately refuses.
- Chart text is legible on the dark ground. FAILS TODAY: #475569 on #14171A.
  <br>`The pair that exists: `ChartCard.test.tsx` asserts the axis colours come from the live tokens and re-embed on a theme flip, and `styles/tokens.test.ts` asserts >= 4.5:1 for `--text-secondary` on `--surface-raised` in dark. No canvas pixel sampling — Playwright never grew one, and a token assertion is the stronger check anyway because it names the failing colour.`
- No surface renders identically in both themes unless it is genuinely achromatic.
  <br>`A Playwright pass screenshots each surface in light and dark and asserts the images differ; any surface that does not differ is triaged as either achromatic by design or carrying a hardcoded colour`

**Review.** The mutation is criterion 4 and it is cheap; the substantive review is visual. Screenshot every surface in both themes before and after and diff. A token fix that changes nothing visible means the token was already resolving and the change was cosmetic; a fix that changes something unexpectedly means a token was doing work nobody knew about.
<br>**Iterate.** v2 removes the possibility rather than the instances: a Tailwind preset that deletes the default colour palette and the rounded/shadow scales from the theme entirely, so text-slate-500 stops being a valid class and the build fails instead of a grep. That is strictly stronger than a sweep and is the right end state.
<br>**Depends on:** WS-B1, WS-UI-4
<br>**Risk.** Low technically, medium in practice. The risk is that it gets deferred forever because nothing breaks without it — which is why UI-1's ratchet exists, so the number can only fall. Second risk is over-correction: stripping colour from something that genuinely encoded state.


#### WS-E3 · Typography and the compiled document: offline and themed

**What it is.** Two rendering problems that only show up outside the happy path. First, the app's fonts load from Google's CDN at apps/web/index.html:7-11, so in a deployment with no internet — which is what a docker compose install on a private network looks like — the whole type system silently falls back and the interface stops looking like itself. Second, the reader can show a server-generated HTML version of a wiki page inside a sandboxed frame;

**Why.** The owner's constraints are explicit: Aleph serves no models and deploys via docker compose, into environments that may be air-gapped. A UI whose typography depends on a CDN is not deployable there, and nothing currently reports the failure. The compiled document matters for a different reason: it is the rendered-from-the-belief-layer artifact CLAUDE.md describes as the point of the knowledge layer, and it is the one surface the design system does not reach.

**How.** First, correct the record in docs/backlog.md. E3 states the iframe blocks scripts and the page preloads /_fs-ch-*/assets/inter-var.woff2. Neither holds. sandbox="" at HtmlDocCard.tsx:48 is deliberate and correct — the compiled document contains no scripts by construction (html_compiler.py:10-11) and the API pairs it with a Content-Security-Policy: sandbox header (routes/wiki.py:462); adding allow-scripts would weaken it for no gain. And neither `_fs-ch` nor `inter-var` appears anywhere in the repository (I grepped): compile_page_html emits one inline <style> (html_compiler.py:28-63) and a system-font stack (_FONT_STACK, :26) with no external references at all. The observed request was almost certainly a browser extension. Then the two real fixes. 1) Self-host the three faces.

**Criteria:**

- The app renders with its own typography with no network access. FAILS TODAY: all three faces fall back.
  <br>`A Playwright run aborting **://fonts.googleapis.com/** and **://fonts.gstatic.com/** asserts getComputedStyle(document.body).fontFamily resolves to Public Sans and document.fonts.check("1em 'JetBrains Mono'") is true`
- Zero external asset requests from the app shell. FAILS TODAY: 3 references in index.html.
  <br>``grep -c 'https://fonts' apps/web/index.html` returns 0, and a Playwright run recording page.on('request') logs no cross-origin request during boot`
- The compiled document matches the app's theme. FAILS TODAY: always #ffffff.
  <br>`A Playwright test opens a page's document view in dark theme and asserts the iframe's document.body background equals the dark surface token value`
- The compiler stays byte-deterministic after the theme change.
  <br>``uv run pytest packages/aleph-wiki/tests -k html_compiler` includes a test asserting two calls with identical inputs produce identical bytes, and still passes`
- The compiled document carries no radius violations. FAILS TODAY: 4.
  <br>``grep -cE 'border-radius: *[^0;]' packages/aleph-wiki/src/aleph_wiki/html_compiler.py` returns 0`
- The backlog's E3 entry states the verified diagnosis. FAILS TODAY.
  <br>``grep -c '_fs-ch' docs/backlog.md` returns 0, and the entry names html_compiler.py's inline style and index.html's font links as the actual causes`

**Review.** Mutation: restore the Google Fonts <link> and confirm criterion 2 fails; restore #ffffff in _STYLE and confirm criterion 3 fails. Then a real offline check, which the aborted-route test only approximates: boot the compose stack on a machine with the host's outbound DNS blocked, load the app, and confirm the type is correct.
<br>**Iterate.** v2 generates the compiled document's stylesheet from tokens.css at build time rather than maintaining a second Python copy of the palette, so the two cannot drift — the same problem the A2UI catalog has and the same fix: one editable source, a generator, and a check that the generated copy matches (the pattern scripts/check-catalog-generated.sh already implements).
<br>**Depends on:** WS-G
<br>**Risk.** Low. The one real risk is font subsetting: an over-aggressive subset drops glyphs the content needs — the app already renders ›, ℵ, ☀, ☾, ⚠ and arbitrary source text in any language. Ship full woff2 rather than a Latin subset unless the size is measured to matter, and add a test that the mono face renders a representative glyph set.


---

### Production readiness: tests, CI, deploy, observability, security


#### WS-P1 · A test harness that speaks HTTP to a real database — and the four defect pins CLAUDE.md still claims exist

**What it is.** Almost every test in Aleph is a pure-logic test: call a Python function with fake inputs, check the answer. That is why 779 tests finish in six seconds (`uv run pytest -m "not integration" -q` → 779 passed, 1 skipped in 5.99s). Nothing starts the web server and talks to a real database in the same test. Five files in the entire repo ever start the app (apps/api/tests/unit/{test_agent_thread_scope,test_asset_stream_auth,test_copilotkit_auth,test_cors_survives_errors,test_scholar_routes}.py) against 31 route files in apps/api/src/aleph_api/routes/.

**Why.** CLAUDE.md's "Fixed, with the test that pins each" section is the mechanism that stops fixed bugs from silently un-fixing. Four of those pins point at files that do not exist: CLAUDE.md:312 and :314 cite tests/e2e/test_retrieval_finds_body_text.py and tests/e2e/test_search_corpus.py, :324 cites tests/e2e/test_belief_spine.py, :326 cites tests/e2e/test_grounding_surface.py, and apps/api/src/aleph_api/routes/assets.py:17 makes the same claim about tests/e2e/test_asset_stream.py.

**How.** 1) Extend tests/integration/conftest.py beside the existing `session` fixture: an `app` fixture calling `aleph_api.main.create_app()` driven through lifespan (asgi-lifespan's LifespanManager, since apps/api/src/aleph_api/lifespan.py mounts the kernel manifest and the fixtures must not skip it), a `client` fixture over `httpx.AsyncClient(transport=ASGITransport(app))`, and a `project` fixture that POSTs /v1/projects and yields the UUID. Run with ALEPH_AUTH_MODE=local so middleware/auth.py synthesizes the dev principal. Keep `app` session-scoped and wrap each test in a rolled-back transaction so the suite stays fast.

**Criteria:**

- Every test file CLAUDE.md and the route docstrings name as a defect pin actually exists — **CORRECTED 2026-08-22:** the original command's regex matched path SUFFIXES, so eleven real files reported MISSING and it could never exit 0. `./scripts/check-dead-refs.sh` already resolves every path named in CLAUDE.md, the gates and the source docstrings, and it exits 0.
  <br>`grep -ohE 'tests/[a-z0-9_/]+\.py' CLAUDE.md apps/api/src/aleph_api/routes/assets.py | sort -u | while read f; do test -f "$f" || { echo "MISSING $f"; exit 1; }; done — exits 0. FAILS TODAY: four paths are missing (tests/e2e/ was deleted in 483816d).`
- The integration suite covers more than the wiki linter
  <br>`uv run pytest -m integration -q --collect-only 2>&1 | tail -1 reports >= 90 collected. FAILS TODAY: 36 collected, 19 of which are tests/integration/test_wiki_lint.py.`
- More than five files in the repo start the application under test
  <br>`grep -rl 'TestClient\|ASGITransport' --include='*.py' apps packages tests | wc -l >= 12. FAILS TODAY: 5.`
- The composition root's live probes run in CI, not only under a script CI never calls
  <br>`uv run pytest -m integration tests/integration/test_capability_probes.py -q passes and the test asserts the composition root's capability NAMES equal the ten parsed from apps/api/aleph.toml — and a self_check.sh probe rewriting one probe body to problem("forced") makes the boot raise. Same for apps/workers/aleph.toml. There is no `ready` list and there cannot be one: a failed probe aborts the boot (Part 4 item 17), so `len(ready) == 10` had no subject. FAILS TODAY: no such test; aleph-runtime has zero tests over 783 LOC.`
- No route answers 500 on a well-formed request for a real project
  <br>`uv run pytest -m integration tests/integration/test_route_smoke.py -q — the test enumerates app.routes at runtime, so a newly added router is covered without editing the test.`
- The whole integration job stays green in CI against the pgvector service
  <br>`the existing .github/workflows/ci.yml `python-integration` job (lines 96-135) passes on `uv run pytest -m integration -q` with no new steps.`

**Review.** Mutation-test every restored pin individually, not the suite as a whole. For each: revert the fix it was written for, run only that test, require a failure, restore, require a pass. Concretely — re-AND the query gate in aleph_rks retrieval so it is body-blind again; null out `Citation.source_id` at the write site in aleph_wiki.belief_service; drop the `body_text` column from the wiki_index projection.
<br>**Iterate.** Turn test_route_smoke.py from "does not 500" into contract enforcement: for each route assert the JSON body validates against the declared OpenAPI response model, and for each mutating route assert exactly one ActionLedgerEvent row appeared in the same transaction.
<br>**Depends on:** —
<br>**Risk.** The recovered tests were written 25 commits ago; the wiki restructure landed since (a73dc2b through afe19db changed frontmatter, slug resolution and link resolution). Expect two of the six to be cheaper to rewrite than to port — budget for that rather than forcing a port.


#### WS-P2 · A 500 you can find in the logs — request correlation on the error path

**What it is.** When the API throws an unhandled exception, the log line it writes contains four keys — environment, event, level, timestamp — and nothing that identifies which request it was, which user, or which project. The HTTP response carries no `x-request-id` header either, even when the browser sent one. So a user saying "I got a 500 at 14:22" cannot be matched to any line in the log. The cause is middleware ordering: apps/api/src/aleph_api/main.py:87-89 adds Auth, then RequestID, then Error, and Starlette's add_middleware prepends — so Error ends up outside RequestID.

**Why.** This is small, cheap, and it is the reason the expensive items in the backlog are expensive. E1 ("Agent run fails, then the runtime tries to keep streaming — the primary RUN_ERROR has not been traced yet") is hard precisely because a browser-side failure cannot be joined to a server-side log line. The backlog's answer to E1 is C3/H7, an Inspector pane — substantial work that would show agent-side events and still would not connect a browser 500 to its log line.

**How.** 1) Move the log-and-respond behaviour so that it runs inside the request context. Two viable shapes: put ErrorMiddleware innermost of the three (Auth, Error, RequestID ordering so RequestID is outermost of the pair) while keeping CORSMiddleware outermost — the commit 2712665 fix that made CORS outermost must not regress; or keep the ordering and have RequestIDMiddleware catch, stamp and re-raise. Prefer the reorder, and pin the resulting order with a test. 2) Bind more than the request id: user id and project id are both resolvable after AuthMiddleware, and aleph_observability.logging already exposes bind_request_context (used at middleware/auth.py, imported at request_id.py:13-16). Bind principal id and, where the path carries {project_id}, the project. 3) Stamp `x-request-id` on error responses too — set it in ErrorMiddleware's JSONResponse construction as well as on the success path.

**Criteria:**

- An unhandled 500 returns the client's own x-request-id
  <br>`a test that POSTs to a deliberately-raising route with header `x-request-id: RID-12345` asserts `response.headers['x-request-id'] == 'RID-12345'`. FAILS TODAY: the header is None (request_id.py:30 is unreachable on the exception path).`
- The unhandled-exception log line carries the request id
  <br>`a test that captures structlog output via structlog.testing.capture_logs (or a JSONRenderer to a StringIO with configure_logging()) asserts 'request_id' in the emitted event dict for the 'unhandled exception' record. FAILS TODAY: the captured keys are exactly ['environment','event','level','timestamp'].`
- The log line also names the principal and, where the route is project-scoped, the project
  <br>`the same test asserts 'user_id' and 'project_id' keys are present for a 500 raised from a route under /v1/projects/{project_id}/. FAILS TODAY.`
- CORS on errors did not regress while fixing this
  <br>`uv run pytest apps/api/tests/unit/test_cors_survives_errors.py -q stays green — it asserts the Access-Control-Allow-Origin header survives a 500, which is the invariant commit 2712665 established.`
- The middleware order is pinned so nobody reintroduces the bug by adding a fourth middleware
  <br>`a test asserting the class names of app.user_middleware in order equals the expected tuple; it fails on any insertion or reorder.`

**Review.** Mutation: reinstate the original ordering in main.py:87-89, run the new tests, require both the header test and the log test to fail, restore, require both to pass. Then a second mutation — add a no-op middleware in the wrong position — and require the order-pinning test to fail. Add both mutations as probes in scripts/_acceptance/self_check.sh alongside the six existing static probes (self_check.sh:60-91).
<br>**Iterate.** Surface the request id in the UI. The RFC 7807 problem body built at middleware/errors.py:23-29 has an `instance` field and no correlation id; add the request id to the body and render it in the web app's error toast, so a user-reported failure arrives with the id already attached instead of a timestamp and a guess. Pairs directly with P9's trace correlation.
<br>**Depends on:** —
<br>**Risk.** Low, with one real trap: middleware ordering in Starlette is counter-intuitive (add_middleware prepends), and this exact area already produced one shipped defect where every 500 surfaced in the browser as a CORS failure (backlog B4). Getting the order wrong a second time is the failure mode. The order-pinning test exists specifically to make that impossible to do silently.


#### WS-P3 · A stack that survives a reboot, an unreachable gateway, and a large upload — plus a CI job that actually builds it

**What it is.** Five things, all in the deploy layer, all currently untrue. (a) No compose service declares a restart policy — `grep -n 'restart:' deploy/compose/docker-compose.yml` returns exactly one hit, `restart: "no"` at line 144 on the one-shot `migrate` service — so any crash or host reboot leaves the whole stack down until a human notices. (b) The API's container health gate is /readyz (docker-compose.yml:165), and /readyz includes the remote model gateway in its verdict (apps/api/src/aleph_api/routes/health.py:47-55: `all_ok = all(...)` over a dict that contains `litellm_gateway`), so an unreachable model endpoint marks…

**Why.** The owner's bar is "everything prod ready". Aleph deploys via docker compose and that is the deployment. A stack with no restart policy, no memory limit on the process that reads uploads into RAM, root containers, and a tracing profile that cannot boot on a fresh volume is not a deployment — it is a demo that happens to be running.

**How.** 1) `restart: unless-stopped` on postgres, redis, runner-redis, api, workers, code-runner, copilot-runtime, web; leave `migrate` as `restart: "no"`. Add a comment naming why migrate is the exception, per this repo's convention. 2) Split readiness. Keep /readyz as the container gate but scope `all_ok` to the dependencies Aleph itself owns — postgres, redis, asset store — and report `litellm_gateway` as informational in the body with its own boolean. Add /readyz?strict=1 or a separate /readyz/deps for an operator who wants the gateway in the verdict. Also move the asset-store probe (health.py:42 `store.put_bytes`) off the synchronous path — compose calls this every 15s (docker-compose.yml:166-167), so today a blocking filesystem write runs on the event loop four times a minute.

**Criteria:**

- Every long-running service restarts itself
  <br>`docker compose -f deploy/compose/docker-compose.yml config | grep -c 'restart: unless-stopped' >= 8. FAILS TODAY: 0 (the only restart key in the file is `restart: "no"` at line 144).`
- The stack boots to healthy with no model gateway reachable
  <br>`LITELLM_BASE_URL=http://127.0.0.1:1 docker compose -f deploy/compose/docker-compose.yml up -d --wait exits 0, and curl -sf localhost:8000/readyz returns 200 with checks.litellm_gateway.ok == false. FAILS TODAY: health.py:54 folds litellm_gateway into all_ok, so --wait times out and web/copilot-runtime never start.`
- A large upload is refused rather than absorbed
  <br>`an integration test POSTing a 200MB body to /v1/projects/{id}/sources/upload asserts 413, and `docker compose config | grep -c mem_limit` >= 4. FAILS TODAY: no size check exists anywhere in apps/api/src (grep for slowapi|max_upload|content_length returns nothing) and mem_limit appears once, on code-runner.`
- --profile tracing boots on a fresh volume
  <br>`docker compose down -v; docker compose --profile tracing up -d --wait exits 0 and `psql -c "select datname from pg_database"` lists `langfuse`. FAILS TODAY: nothing creates that database (no initdb hook in deploy/, no CREATE DATABASE anywhere).`
- No image runs as root
  <br>`for f in apps/*/Dockerfile*; do grep -q '^USER' $f || { echo $f; exit 1; }; done exits 0. FAILS TODAY: 4 of 5 have zero USER lines.`
- A Dockerfile that does not build fails CI
  <br>`the new `images` job in .github/workflows/ci.yml builds all five and runs `docker compose config -q`. Verify by pushing a branch with a deliberate typo in apps/api/Dockerfile and confirming the job is red. FAILS TODAY: no CI job touches a Dockerfile.`
- A placeholder secret does not boot
  <br>`ALEPH_AGENT_TOKEN_SECRET=CHANGE-ME-run-openssl-rand-hex-32 uv run python -c 'from aleph_api.main import create_app; create_app()' exits nonzero with a named error. FAILS TODAY: accepted verbatim.`

**Review.** Three mutations against the running stack, each restored after. (1) `docker kill aleph-api-1` and confirm it comes back within 30s — today it stays dead. (2) Point LITELLM_BASE_URL at a black hole and confirm `up -d --wait` still succeeds and the web UI loads — today the whole stack fails to come up.
<br>**Iterate.** Second pass adds the operator-facing half: healthcheck-driven alerting hooks, a documented resource sizing table in docs/operations.md (what each service needs at rest and under an ingest), and a compose override file for a single-host production deployment separate from the dev file — the current apps/web/Dockerfile.dev running `npm run dev` behind an unpinned `npm install` (Dockerfile.dev:10, with no lockfile copie…
<br>**Depends on:** —
<br>**Risk.** Adding USER to the API image will break the `assets:/app/data/assets` named volume (docker-compose.yml:160) unless ownership is set at build time — this is exactly the failure the stale comment at apps/api/Dockerfile:29-31 was written about, and it is why the image currently runs as root.


#### WS-P4 · The web app can be tested at all — a unit runner, and the browser suite restored

**What it is.** apps/web has no way to run a test. Its package.json scripts are dev, build, preview, typecheck, lint; its devDependencies contain no vitest, jest or playwright; and `find apps/web/src -name '*.test.*' -o -name '*.spec.*' -o -name '__tests__'` returns nothing. The browser suite that used to exist, tests/playwright/, was deleted in 483816d — yet pnpm-workspace.yaml:3 still lists it as a workspace member, and audit/checks/e2e/ still holds seven real .spec.ts files (project-create, projects-list, session-create, chat-streams-response, viz-renderers, wikilink-navigation, workspace-three-panel-shell) whose runner is a…

**Why.** The owner's stated bar is that the UI "cannot be half-baked with stale or dead code laying around and every part needs to function as expected". There is currently no mechanism that could detect a half-baked UI — every UI defect on the backlog (E1's RUN_ERROR, E3's blocked iframe assets, G's 180 token violations across 43 components) was found by a human opening a browser. That does not scale to a self-improving harness whose whole premise is that an agent adds capability, because agent-added capability ships UI.

**How.** 1) Add vitest + @testing-library/react + jsdom to apps/web, with `test` and `test:run` scripts and a vitest.config.ts that reuses the vite aliases. Target the parts that hold logic, not pixels: apps/web/src/lib/workspace-ui.tsx (MAX_PANES and the pane reducer), apps/web/src/a2ui/SurfaceStreamProvider.tsx (multiplexing and stream resume — the provider that gives the whole reading region one SSE connection), apps/web/src/lib/api.ts (auth header attachment), apps/web/src/lib/auth.ts (getAccessToken's local vs oidc branches at :71-80). 2) Recreate tests/playwright/ as a real pnpm workspace member — the entry in pnpm-workspace.yaml:3 is already there and currently dead — with @playwright/test and the seven specs recovered from audit/checks/e2e/. Point them at the compose stack. Delete the dangling symlink and the audit/ copies so there is one location, not two.

**Criteria:**

- The web app has a test command that runs tests
  <br>`pnpm -C apps/web test:run exits 0 and reports >= 15 tests. FAILS TODAY: the script does not exist in apps/web/package.json.`
- There are front-end tests to run
  <br>`find apps/web/src tests/playwright -name '*.test.ts*' -o -name '*.spec.ts' | wc -l >= 20. FAILS TODAY: 0.`
- CI runs them
  <br>`grep -cE 'test:run|playwright' .github/workflows/ci.yml >= 2. FAILS TODAY: 0.`
- No dead test references remain in the repo
  <br>`a new scripts/check-dead-refs.sh asserting every pnpm-workspace.yaml member directory exists and every symlink under audit/ and tests/ resolves; exits 0. FAILS TODAY: pnpm-workspace.yaml:3 names a deleted directory and audit/checks/e2e/node_modules is dangling.`
- A real UI invariant is defended
  <br>`change MAX_PANES in apps/web/src/lib/workspace-ui.tsx to any other value → pnpm -C apps/web test:run exits nonzero. Restore → exits 0.` **CORRECTED 2026-08-22:** it is 24, not 3, so "from 3 to 4" changes nothing and the mutation is a no-op. CLAUDE.md said three panes too, and now says 24.`
- Every browser spec passes against a live stack — **CORRECTED 2026-08-22:** "the seven recovered" is now 13, and naming a count makes the criterion go stale every time a spec is added. `pnpm -C tests/playwright test` against the compose stack: every spec passes, 0 failures, no retries (`retries: 0` is deliberate — a test that needs a retry is a defect in the test).
  <br>`docker compose up -d --wait && pnpm -C tests/playwright exec playwright test → 7 spec files, 0 failures.`

**Review.** Three mutations across three layers, each restored: MAX_PANES (unit, tests the reducer), the SSE URL builder in SurfaceStreamProvider.tsx:96 (unit, tests the transport the whole reading region shares), and the rail's launchable filter driven by GET /v1/projects/{id}/panes (playwright, tests the server-driven pane registry end to end).
<br>**Iterate.** Add Playwright trace and screenshot upload on failure, then a visual baseline for the nine components the backlog's G audit scores as `clean` (Board, Block, ContextBar, AssistantDock, ReadingRegion, AlephLogo, Icons, CopilotChatSurface, ProjectWorkspace).
<br>**Depends on:** WS-P3
<br>**Risk.** Browser e2e in CI is the classic flaky-test trap, and a flaky required job trains everyone to re-run rather than read. Mitigate by keeping the e2e job on push-to-main rather than every PR at first, and by treating any test that needs a retry as a defect in the test rather than a fact of life.


#### WS-P5 · Make the acceptance gate honest, and put it in CI

**What it is.** CLAUDE.md names scripts/acceptance.sh as "the gate to trust" — the thing that counts skips separately and can prove its own checks fail. Right now it is the weaker of the two claims made about it. Twenty of its check invocations name test files that were deleted in 483816d: acceptance.sh:167,169,171,173 (checks B1, B2, B3, B7) and :231-257 (C1, C3, C4, C6, C7, C8) call into tests/e2e/test_retrieval_finds_body_text.py, test_search_corpus.py, test_belief_spine.py and test_retraction_walk.py, none of which exist.

**Why.** This repo's founding lesson, stated in CLAUDE.md's opening paragraph and again in the CI header, is that a green light meaning nothing is worse than no light. The acceptance harness is the machinery built to prevent exactly that, and it has drifted into being an instance of it. One piece of it is still earning its keep and proves the design works: the deliberately-red check E5 (acceptance.sh:290-291) flipped to PASS when aleph-belief finally acquired a consumer in aleph_wiki.belief_service, and reported "PASS — FIX…

**How.** 1) Add a preflight to acceptance.sh that runs before any check and independently of whether services are up: for every pytest path or node-id named anywhere in the script, resolve the file on disk. Unresolvable → status MISSING, counted in its own bucket, and exits nonzero always. MISSING is not SKIP: SKIP means "this machine cannot run it", MISSING means "the subject is gone". 2) Fix services_up() (acceptance.sh:66-70) to honour ALEPH_TEST_DATABASE_URL's host and port rather than hardcoding localhost:5432, so a developer running the normal stack (Postgres on 5442) gets real results instead of a screen of skips. 3) Add `--strict`, which makes SKIP a failure, for use in CI where services are guaranteed.

**Criteria:**

- A check whose subject file is gone is reported as MISSING and fails the run
  <br>`delete any one file a check names (e.g. packages/aleph-core/tests/test_rrf.py, which check B4 invokes), run ./scripts/acceptance.sh --quick → the row reads MISSING and the script exits 1. Restore → exits 0. FAILS TODAY: there is no MISSING status; the run reports SKIP or FAIL depending on whether a port answers.`
- Zero checks currently name a nonexistent file
  <br>`./scripts/acceptance.sh --quick reports missing=0. FAILS TODAY: 20 tests/e2e references across acceptance.sh (grep -c 'tests/e2e' scripts/acceptance.sh = 20) resolve to nothing.`
- The gate runs in CI
  <br>`grep -c 'acceptance.sh' .github/workflows/ci.yml >= 2. FAILS TODAY: 0.`
- The self-check proves the five CI sweeps can fail, not just the kernel
  <br>`./scripts/acceptance.sh --self-check prints >= 11 'can fail' lines including one per sweep by name. FAILS TODAY: 6 probes, none of which touch a sweep.`
- Skips are real, not an artifact of a hardcoded port
  <br>`with the dev stack up (Postgres on 5442), ALEPH_TEST_DATABASE_URL=postgresql+asyncpg://aleph:...@localhost:5442/aleph ./scripts/acceptance.sh reports skip=0 for every service-backed check. FAILS TODAY: services_up() probes localhost:5432 unconditionally, so every service-backed check skips.`
- docs/operations.md describes the CI that exists
  <br>`a check in scripts/check-dead-refs.sh (or a doc test) asserting every scripts/check-*.sh named in docs/operations.md exists and every existing sweep is named there; exits 0. FAILS TODAY: the doc names two sweeps and says five were deleted; five exist and all five are CI-wired.`

**Review.** Mutation in two directions. Downward: delete a subject file, require MISSING and exit 1, restore. Upward: introduce a real regression that a service-backed check should catch — e.g. break the RRF fusion in packages/aleph-core so B4 fails — and confirm `--strict` in CI goes red rather than reporting INCOMPLETE and exiting 0.
<br>**Iterate.** Once the gate is honest and green, make it the thing the docs are generated from rather than the thing they describe. Emit a machine-readable result (JSON) per run and have docs/acceptance.md's status table rendered from it, so the table cannot claim a part is done when its check is MISSING — the drift that produced this workstream in the first place.
<br>**Depends on:** WS-P1
<br>**Risk.** Turning MISSING into a hard failure will make the gate red on the day it lands unless P1 has restored the tests first — hence the dependency. If P1 slips, the honest interim is to land the mechanism and let CI run it non-blocking for exactly one sprint with a dated comment, rather than weakening MISSING to a warning, because a warning is how this drift started.


#### WS-P6 · Sweeps that cannot silently pass — plus the one invariant with no sweep at all

**What it is.** Aleph has five CI sweeps that check invariants no type system can: that the generated A2UI catalogs match their source, that no LangGraph node writes an undeclared state key, that there is one catalog identity, that client and server agree on pane kinds, and that every surface prop a Python producer binds is declared in the client's zod schema. All five exist, all five are wired into .github/workflows/ci.yml:38-84, and all five exit 0 today — that part of the backlog's "Done" list is accurate and I verified it. Two things are wrong with them.

**Why.** CLAUDE.md's list of "Rules that are real but only held by review" is honest about which commitments have no enforcement, and project scoping is the one that matters most: the entire security story of this system is per-project scoping, and the F1 fix (an agent endpoint whose tools took their project scope from a client-supplied thread id) was exactly a failure of it.

**How.** 1) Fix the fail-open: make scripts/_lib/surface_bindings.py:122-123 raise rather than return [], matching check-pane-registry.sh:60-62's pattern, and add the case to tests/unit/test_surface_bindings_sweep.py which already exists and already pins both directions of the comparison. 2) Widen the scope honestly: either bring the 16 card components under the binding check by having cards.py declare path bindings, or record in the sweep's own output how many components it compared versus how many exist, so `5 components` reads as a coverage number rather than a completeness claim. The second is a two-line change and stops the sweep from overclaiming.

**Criteria:**

- A sweep whose subject file moves fails loudly from the check, not incidentally from a later read
  <br>`git mv packages/aleph-a2ui/src/aleph_a2ui/components/surfaces.py /tmp/ && ./scripts/check-surface-bindings.sh; assert exit 1 AND stderr names the missing file. Restore. FAILS TODAY: run() returns [] and the nonzero exit comes from an unhandled FileNotFoundError in the wrapper's second read.`
- Six sweeps run in CI, not five
  <br>`grep -c 'scripts/check-.*\.sh' .github/workflows/ci.yml >= 6. FAILS TODAY: 5.`
- Every project-scoped route handler takes ProjectScopeDep
  <br>`./scripts/check-project-scope.sh exits 0. FAILS TODAY: it does not exist, and when written it reports at least one offender — apps/api/src/aleph_api/routes/surfaces.py:54 list_pane_kinds(project_id: UUID).`
- The new sweep genuinely fires
  <br>`replace `project_id: ProjectScopeDep` with `project_id: UUID` in any handler in apps/api/src/aleph_api/routes/notes.py → ./scripts/check-project-scope.sh exits 1 naming file and line; restore → exits 0.`
- The analyzer is tested, not just the wrapper
  <br>`uv run pytest tests/unit/test_project_scope_sweep.py -q passes, with cases for a scoped handler, an unscoped handler, an allowlisted handler, and a non-route function named project_id — the false-positive class that got the surface-bindings sweep a dedicated test file already.`
- Each sweep's coverage is stated as a number, not implied
  <br>`./scripts/check-surface-bindings.sh prints 'N of M catalog components compared'. FAILS TODAY: it prints '5 components, 11 bound props' with no denominator, which reads as complete.`

**Review.** Mutation-test all six sweeps in one pass and record every mutation in scripts/_acceptance/self_check.sh (P5 adds the probe slots): hand-edit apps/web/src/a2ui/catalog.ts; add an undeclared state-key write to a LangGraph node; declare a second ALEPH_V09_CATALOG_ID; add a pane kind to the server registry only; add a `{"path": ...}` binding the client zod schema does not declare; drop ProjectScopeDep from a handler.
<br>**Iterate.** Extend the project-scope sweep from "takes the dependency" to "checks the role": several handlers call require_at_least(principal, project_id, at_least=ProjectRole.EDITOR) (e.g. routes/sources.py:73) and several do not, and which mutating routes require which role is currently a per-route judgement with no record.
<br>**Depends on:** WS-P5
<br>**Risk.** An over-eager sweep is worse than no sweep, because the response to false positives is to disable it. The mitigation is the one this repo already discovered: ship the analyzer with a unit test that pins both directions (fires on the defect, quiet on the lookalike) before wiring it into CI. Second risk: the allowlist becomes a dumping ground.


#### WS-P7 · One secret doing three jobs — separate the keys, version them, and test the only cryptography in the repo

**What it is.** ALEPH_AGENT_TOKEN_SECRET is used for three unrelated things. (1) It signs the short-lived HS256 agent tokens workers use to call back into the API (packages/aleph-security/src/aleph_security/agent_token.py:62,67). (2) It is the master key from which every stored connector credential's encryption key is derived — sha256(master_secret || project_id.bytes) into a libsodium SecretBox (packages/aleph-connectors/src/aleph_connectors/credentials.py:87-95). (3) Compose hands the same value to Langfuse as all three of NEXTAUTH_SECRET, SALT and ENCRYPTION_KEY (deploy/compose/docker-compose.yml:352-354).

**Why.** This is the clearest answer to "what would not survive a security incident". Aleph's connector credentials are real third-party API keys and OAuth blobs — the Consensus OAuth blob is bootstrapped by scripts/connect-consensus.py — and the recovery procedure for a leaked signing key currently destroys all of them with no warning and no way back.

**How.** 1) Split the key. Add ALEPH_CREDENTIAL_MASTER_KEY as its own required setting in apps/api/src/aleph_api/settings.py (which already declares aleph_agent_token_secret at :79) and apps/workers/src/aleph_workers/settings.py:49, and give Langfuse its own LANGFUSE_NEXTAUTH_SECRET / LANGFUSE_SALT / LANGFUSE_ENCRYPTION_KEY in .env.example and compose rather than reusing the token secret at docker-compose.yml:352-354. 2) Collapse the three derivations into one function in packages/aleph-connectors — the package that owns the cipher — and delete the copies at scholar.py:204-206, connector_credentials.py:47-50 and tools.py:89-91. Remove the `.ljust(32, b"0")` padding so the cipher's own >= 32 bytes guard (credentials.py:82-85) actually applies, and fail loudly at startup rather than at first decrypt.

**Criteria:**

- The signing key and the credential-encryption key are different settings
  <br>`grep -rn 'aleph_agent_token_secret' apps packages --include=*.py | grep -c 'encode("utf-8")' == 0 (0 today, against 26 legitimate SIGNING uses of the setting), plus test_cipher_construction_sites.py green. The property is that no ENCRYPTION KEY is derived from the signing secret, not that the setting is unmentioned — the original scope reaches 0 only by deleting agent-token minting. FAILS TODAY: 3 sites do (scholar.py:204, connector_credentials.py:47, tools.py:89 via master_secret_bytes).`
- The key derivation exists exactly once
  <br>`uv run pytest packages/aleph-connectors/tests/test_key_derivation_is_single.py -q` passes — it asserts over the CODE (one derivation, no padding) rather than over the text of the repository. **CORRECTED 2026-08-22:** the original greps count prose. All three `ljust(32` hits are docstrings describing the removed defect — one of them is this criterion quoted verbatim inside the very test that enforces it — and two of the three `sha256(.*master` hits are the same. As written it can only be satisfied by deleting the explanations of why the rule exists.`
- Rotating the token secret does not destroy credentials
  <br>`an integration test that stores a credential, rotates ALEPH_AGENT_TOKEN_SECRET, and asserts the credential still decrypts and a freshly minted agent token verifies. FAILS TODAY: rotation makes every stored credential permanently undecryptable.`
- A short master key is refused at boot, not tolerated by padding
  <br>`ALEPH_CREDENTIAL_MASTER_KEY=short uv run python -c 'from aleph_api.main import create_app; create_app()' exits nonzero. FAILS TODAY: the value is padded to 32 bytes with ASCII zeros and accepted.`
- The only cryptography in the repo has tests
  <br>`uv run pytest packages/aleph-connectors -q reports >= 12 tests. FAILS TODAY: packages/aleph-connectors contains only pyproject.toml and src/ — 0 tests across 1,649 LOC.`
- Rotation is a documented procedure someone can follow
  <br>`grep -c 'rotat' docs/operations.md >= 1 and the named script/command in it runs end-to-end in the rotation test. FAILS TODAY: the word does not appear in the file.`

**Review.** Rehearse the incident. On a stack with at least two projects each holding a real connector credential: rotate the token secret and confirm agent tokens still mint and verify while credentials still decrypt; then rotate the credential master key through the re-encrypt path and confirm every row is readable after and unreadable with the old key;
<br>**Iterate.** Move the master key out of environment variables entirely — an interface with one file-based implementation (reading a mounted secret) and one that calls out to a KMS or Vault, chosen by setting. That is what makes rotation routine rather than an incident procedure, and it is the same shape as the connector-credential contract that already exists, so it is a small step once the key is separated.
<br>**Depends on:** WS-P1, WS-P3
<br>**Risk.** The highest-consequence workstream here: a mistake makes existing credentials undecryptable for real rather than hypothetically. Mitigation is strict ordering — add the key-version column and the read-both-versions path first, deploy that, then re-encrypt, then remove the old-version read. Never in one step.


#### WS-P8 · Backup, restore, and a migration rollback that has actually been run

**What it is.** There is no backup or restore story for Aleph anywhere in the repo. Searching deploy/, scripts/ and docs/operations.md for backup, pg_dump, restore, pgbackrest or wal returns only unrelated hits — a wiki test name and self_check.sh's own file-mutation backup. All data lives in named docker volumes: `postgres-data` and `assets` (docker-compose.yml:54-58). No dump service, no schedule, no documented procedure, no quota, no monitoring.

**Why.** The prod-ready bar has a floor, and this is it: a system whose data cannot be restored is not deployed, it is running. Aleph's data is not reproducible — ingested sources, extracted claims, the hash-chained action ledger, analyst notes and hand-edits are all irreplaceable by re-running anything. The ledger case is sharper than the rest: it is append-only and hash-chained specifically so its history is evidence, and evidence that exists in exactly one place on one unmonitored docker volume is not evidence.

**How.** 1) A `backup` one-shot compose service (mirroring the existing `migrate` one-shot at docker-compose.yml:128-147) running pg_dump with a custom-format dump to a mounted host directory, plus a tar of the assets volume. Invoked by `scripts/backup.sh`, restorable by `scripts/restore.sh`, both with a --dry-run. 2) A restore drill script that provisions a scratch stack from a dump and asserts the restored data is coherent: row counts per table, the ledger hash chain verifies end to end (tests/integration/test_ledger_immutability.py already covers the trigger, so the verifier exists), and at least one asset round-trips through the store.

**Criteria:**

- A backup can be taken and restored into an empty stack
  <br>`./scripts/backup.sh && docker compose down -v && ./scripts/restore.sh <dump> && uv run python scripts/_acceptance/restore_drill.py` exits 0. **CORRECTED 2026-08-22:** the drill is `scripts/_acceptance/restore_drill.py`, not `scripts/restore-drill.sh`, which has never existed. It restores into a scratch database, compares per-table row counts against the source and re-runs the ledger hash-chain verification. It is still invoked by no gate — that half is UNMET, not stale.`
- The restored database is verified, not assumed
  <br>`scripts/_acceptance/restore_drill.py` asserts per-table row counts match the pre-backup counts and re-runs the ledger hash-chain verification; it exits nonzero if any table lost rows. Prove it can fail by deleting one row from the dump's restore target before verification. **CORRECTED 2026-08-22:** the filename. `restore-drill.sh` does not exist.`
- The newest migration's downgrade actually runs
  <br>`a CI step: cd apps/api && uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head && uv run alembic check — exits 0. FAILS TODAY: no CI job ever invokes downgrade (grep -c downgrade .github/workflows/ci.yml = 0).`
- A broken downgrade fails CI
  <br>`mutate the newest revision's downgrade() to drop a table it did not create → the new CI step goes red; restore → green.`
- The procedure is written where an operator looks
  <br>`grep -cE 'backup|restore|downgrade' docs/operations.md >= 3, and each named command exists on disk. FAILS TODAY: 0 for backup/restore, and 'downgrade' does not appear in the file.`
- Volume growth is visible before it fills a disk
  <br>`the /readyz body (or the /metrics endpoint from P9) reports bytes used for postgres-data and the asset root; a test asserts the field is present and numeric.`

**Review.** A real drill, not a script review: take a backup from a stack with a populated project, `docker compose down -v` to destroy everything including volumes, restore, and confirm the web UI opens the same project with the same sources, claims and notes. Then a negative drill: corrupt one page of the dump and confirm restore-drill.sh reports failure rather than a partially restored stack that looks fine.
<br>**Iterate.** Move from dump-and-restore to point-in-time recovery: WAL archiving to the object store (MinIO already exists behind `--profile s3` at docker-compose.yml:380) and a documented recovery target time. Add a scheduled drill — a CI cron that restores the most recent dump into a scratch stack weekly — because a backup that has never been restored on a schedule is a backup nobody has verified this month.
<br>**Depends on:** WS-P3
<br>**Risk.** Near-certain: at least one of the 23 unexecuted downgrades will fail the first time it runs, most likely one that drops a column with a dependent index or an enum type. That is the point of the exercise, but budget for fixing two or three migrations rather than only adding a CI step.


#### WS-P9 · Numbers, not just traces — metrics, and a trace id you can follow from browser to log line

**What it is.** Aleph has tracing and no metrics. Tracing is installed and correctly opt-in: packages/aleph-observability/src/aleph_observability/tracing.py:56-68 skips the OTLP exporter when the endpoint is empty, with a written reason (an unconditional exporter previously buried real errors under endless export-failure lines), init_otel is mounted as a kernel capability at aleph_runtime/capabilities.py:86 and instrument_fastapi is called from apps/api/src/aleph_api/main.py:141. But there are no metrics of any kind — no Prometheus, no OpenTelemetry meter, no counters, no histograms, no /metrics route;

**Why.** Traces answer "what happened in this one request". Metrics answer "is the system healthy, is it getting slower, how often does this fail" — and Aleph currently cannot answer any of those. That gap has already cost: backlog E5 is "gateway rate limiting, reported as weirdly rate limited, not yet characterised" — a question that is unanswerable by reading logs and trivial with a per-purpose request-rate counter, which is precisely what the subagent fan-out hypothesis needs to be confirmed or dropped before H5's interp…

**How.** 1) Add an OpenTelemetry meter to packages/aleph-observability alongside the existing tracer, exposed the same way (a start_span-style helper so call sites stay uniform), and a /metrics route on the API in Prometheus exposition format — authenticated or bound to the internal network, not published to 0.0.0.0 like /readyz and /docs currently are. 2) Instrument the five things that answer real questions rather than everything: HTTP request rate/latency/status by route (the FastAPI instrumentation at main.py:141 already gives the hook), LLM calls by capability and purpose and outcome — which directly characterises E5 — token and cost by pricing_source, so the share of unpriced spend from E4 is a number; arq queue depth and job outcome by queue (ingest, research, reviewers, code_runner); and ingest/retrieval latency.

**Criteria:**

- A metrics endpoint exists and exposes real series
  <br>`curl -s localhost:8000/metrics | grep -c '^aleph_' >= 12. FAILS TODAY: no /metrics route exists (grep for '"/metrics"' across apps returns nothing).`
- LLM traffic is countable by purpose
  <br>`after driving one agent turn, curl -s localhost:8000/metrics | grep 'aleph_llm_requests_total' shows at least two distinct purpose labels. FAILS TODAY: no counter exists anywhere (zero hits for Counter(/Histogram( across apps and packages).`
- Unpriced spend is a number, not an anecdote
  <br>`aleph_model_call_cost_total carries a pricing_source label with gateway/static/unknown values, so the unpriced share is one query. Backlog E4 becomes answerable. FAILS TODAY.`
- The API is instrumented like a request path, not like an afterthought
  <br>`grep -c 'with start_span(' over apps/api/src is >= 8, and every request-path stage in aleph_observability.metrics.STAGES has at least one call site. Call sites, not prose: the bare-name count is 17, of which 4 are imports and 5 are comment text, so a docstring mentioning start_span moves it. FAILS TODAY: 2, against 27 in the legacy wiki package.`
- One id joins the log line, the span and the response
  <br>`an integration test asserting the response's x-request-id equals the request_id bound in the log record and equals the aleph.request_id attribute on the root span. FAILS TODAY on all three counts (P2 fixes the first two).`
- Metrics are not published unauthenticated
  <br>`a test asserting GET /metrics from a NON-LOOPBACK peer with no ALEPH_METRICS_TOKEN returns 403` — and `/metrics` must not be added to `_PUBLIC_PATHS` in `middleware/auth.py` alongside `/healthz`, `/readyz`, `/docs`, `/redoc` and `/openapi.json`. **CORRECTED 2026-08-22:** the original said "in oidc mode … 401". There is no oidc mode — `docs/decisions.md` D6 removed OIDC — so the criterion named a configuration that cannot be selected. The gate that exists is loopback-or-token and it answers 403.`

**Review.** Mutation with a question attached. Drive the stack, capture the counters, then deliberately break the model gateway and confirm the LLM failure counter rises and the success counter stops — a metric that does not move when the thing it measures breaks is decoration. Then use the new metrics to actually answer backlog E5: run one agent turn with subagent fan-out and read the per-turn request count off the counter.
<br>**Iterate.** Turn the metrics into the kernel's degradation signal. acceptance D5 asserts "probation: a capability that degrades is retired automatically" and packages/aleph-kernel/tests/test_probation.py passes — but degrade is currently defined against the capability's own probe.
<br>**Depends on:** WS-P2
<br>**Risk.** Label cardinality is the standard way to turn a metrics endpoint into an outage: any label carrying a project id, a user id or a raw path with a UUID in it grows without bound. Use the route template, not the path. Second risk: this becomes a write path with no read path — the exact defect class CLAUDE.md names as dominant — if nothing ever queries the endpoint.


#### WS-D3 · The chat bridge forwards the caller's credential — and port 4000 stops being an open proxy

**What it is.** Backlog D3 says the Node bridge between the browser and the agent does not forward the user's credential, so `oidc` mode cannot authenticate the chat path. The premise is literally true — apps/copilot-runtime/src/server.ts:44 is `new HttpAgent({ url: AGENT_URL })` with no headers — but the conclusion is stale for the installed version, and I verified this against the package in the tree.

**Why.** The backlog parks D3 and D4 together as one deferred project, and that framing costs more than the work does. D3 is one prop on one component plus a test, and parking it behind D4's genuine transport rewrite makes a cheap fix look expensive and keeps oidc mode unusable for the chat path.

**How.** 1) Hold the access token in React state in apps/web/src/lib/copilot.tsx and pass `headers={() => ({ Authorization: \`Bearer ${token}\` })}` to CopilotKitProvider. The only real friction is that the prop is synchronous while apps/web/src/lib/auth.ts:71-80 getAccessToken() is async — so hold it in state and refresh from the UserManager's automaticSilentRenew, which auth.ts:45 already enables. In local mode the LOCAL_BEARER constant is returned synchronously, so nothing changes there. 2) Narrow the runtime's CORS: replace `cors: true` at server.ts:80 with an explicit origin list from an env var, defaulting to the same origins the API accepts (ALEPH_CORS_ORIGINS in deploy/compose/.env.example already carries that list). Compose passes it to the copilot-runtime service, which today gets only ALEPH_AGENT_URL and PORT (docker-compose.yml:251-256).

**Criteria:**

- The browser attaches a credential to the runtime
  <br>`a vitest test asserting CopilotKitProvider is mounted with a headers function whose return value contains an Authorization bearer. FAILS TODAY: apps/web/src/lib/copilot.tsx:122-127 passes runtimeUrl, renderActivityMessages and openGenerativeUI, and no headers prop.`
- The credential survives the bridge to the API
  <br>`an integration test asserting the bridge FORWARDS the caller's Authorization header to /copilotkit/agent/assistant, and that a request arriving without one is not silently given the server's own credential.` **CORRECTED 2026-08-22:** the original was conditioned on oidc mode, which `docs/decisions.md` D6 removed. The forwarding behaviour is the part that outlives the auth mode — it is what makes the chat path authenticable under ANY mode — so that is what the criterion asserts now.`
- Port 4000 is not an any-origin proxy
  <br>`curl -H 'Origin: https://evil.example' against the runtime returns no Access-Control-Allow-Origin for that origin. FAILS TODAY: server.ts:80 sets cors: true.`
- The runtime's allowed origins are configuration, not a constant
  <br>`grep -c 'CORS_ORIGINS' apps/copilot-runtime/src/server.ts deploy/compose/docker-compose.yml >= 2. FAILS TODAY: 0.`
- The bridge is actually in the running stack
  <br>`docker compose ps --services --filter status=running | grep -q copilot-runtime. FAILS TODAY: it is declared at docker-compose.yml:246 but absent from the running stack.`
- The docs stop describing D3 as an open deployment-scale gap
  <br>`CLAUDE.md's Known-broken entry for the runtime bridge and docs/architecture.md:280-281 both updated, and the entry moved to 'Fixed, with the test that pins each' naming the two tests above.`

**Review.** Mutation in both halves. Remove the headers prop from copilot.tsx → the vitest test and the oidc integration test both go red; restore. Set cors back to true in server.ts → the origin test goes red; restore. Then a manual pass in oidc mode with a real IdP: open the chat, send a message, confirm it works;
<br>**Iterate.** Once the header path works, verify the negative direction: that a token for project A cannot drive an agent run against project B through the bridge. middleware/agent_scope.py and copilot_agent's _authorized helper already enforce this at both ends (pinned by apps/api/tests/unit/test_agent_thread_scope.py and test_agent_project_authorization.py), but neither test drives the request through the Node bridge, so the com…
<br>**Depends on:** WS-P4
<br>**Risk.** Low. The one real trap is the sync/async mismatch: if the token is read once at mount and never refreshed, the chat works for an hour and then silently stops — a failure that looks like the agent being broken rather than the token expiring. The expiry test in the review step exists specifically to catch that.


#### WS-D4 · ~~SSE that can carry a credential~~ — **WITHDRAWN**

> Withdrawn 2026-08-21. This existed because every live surface was dark in
> `oidc` mode. OIDC is removed (`decisions.md` D6), so there is no mode in which
> these streams lack a credential. `WS-D3` survives — see below, it was never
> really an OIDC problem.
>
> **One criterion is salvaged, because it was never about OIDC.** The original
> c4 asked that every SSE consumer go through one builder rather than
> constructing `EventSource` inline. Two direct constructions remain
> (`apps/web/src/a2ui/SurfaceStreamProvider.tsx:184`, `apps/web/src/hooks/useWikiLiveSignals.ts:74`),
> and no sweep checks it. That is a transport-shape criterion that outlives the
> auth mode it was written under: **`grep -rn 'new EventSource(' apps/web/src |
> wc -l` returns 1, the shared builder.** Tracked here rather than silently
> dropped with the workstream.

#### ~~WS-D4 original~~ · SSE that can carry a credential — the four streams and the iframe asset route

**What it is.** Backlog D4 says EventSource cannot set an Authorization header, so in oidc mode every server-sent-event stream and the iframe-consumed asset route have no way to carry a token. I verified this and it holds completely. There are four EventSource construction sites, all with withCredentials: false — apps/web/src/a2ui/SurfaceStreamProvider.tsx:96 (the one multiplexed connection the whole reading region shares), apps/web/src/a2ui/A2UISurfaceView.tsx:169, apps/web/src/components/ActivityCard.tsx:129, apps/web/src/hooks/useWikiLiveSignals.ts:74. There is no fetch-based SSE fallback anywhere in apps/web/src.

**Why.** Every live surface in Aleph is an SSE stream. The reading region tiles up to three panes fed by one multiplexed connection; agent events, the changes feed and wiki live signals are all SSE; and compiled artifacts render in a sandboxed iframe served by the asset route. In oidc mode all of that is dark. That is not a partial degradation — it is the workspace not working.

**How.** Decide the transport first, then implement once across all four sites. Three real options, and the choice should be written down in docs/decisions.md because it is a security-relevant design commitment: (a) a short-lived, single-use stream token minted by an authenticated POST and passed as a query parameter — simple, works for the iframe too, but puts a credential in URLs and therefore in access logs, so it must be short-TTL and scoped to one project and one stream, which the existing agent-token machinery already knows how to do (packages/aleph-security/src/aleph_security/agent_token.py:47 caps TTL at 3600s and :67-73 pins algorithms and verifies aud/iss); (b) a same-site cookie set at login — no URL exposure, works with EventSource and the iframe with no client change, but reintroduces CSRF surface that the current bearer-only posture avoids;

**Criteria:**

- All four SSE streams open in oidc mode
  <br>`a Playwright test (from P4) in oidc mode against a stack with a test IdP: open a project, assert the surface stream, agent events, changes feed and wiki live signals all deliver at least one event. FAILS TODAY: all four are unauthenticated and rejected.`
- An unauthenticated stream is still refused
  <br>`an integration test asserting each SSE route returns 401 with no credential in oidc mode, and 403 with a credential scoped to a different project — the property assert_stream_access at middleware/project_scope.py:138-166 already implements and that nothing currently exercises over the wire.`
- The iframe asset route works without inheriting a general-purpose token
  <br>`a test asserting a compiled artifact renders in the sandboxed iframe in oidc mode, and that the credential it used cannot be replayed against a non-asset route.`
- No EventSource is constructed without a credential path
  <br>`a sweep (added to the P6 set) asserting every `new EventSource(` site in apps/web/src goes through the shared authenticated builder; `grep -c 'new EventSource' apps/web/src/**/*.tsx` outside that builder == 0. FAILS TODAY: 4 direct constructions.`
- The transport choice is recorded
  <br>`docs/decisions.md gains a numbered decision naming the option chosen, the two rejected, and why — matching the D1/D5 format already in that file.`
- If option (a): a stream token expires and cannot be reused
  <br>`tests asserting a token past its TTL is refused and a token already used to open a stream is refused a second time.`

**Review.** Mutation across all four sites plus the server: remove the credential from the shared builder → all four stream tests go red; restore. Point a stream token at a different project id → the 403 test fires. Then a manual oidc session: leave a workspace open past the token's TTL and confirm the streams reconnect with a fresh credential rather than silently going quiet — a stream that stops delivering with no error is ind…
<br>**Iterate.** Add stream-level observability once the transport works: connection count, reconnect rate and per-stream event throughput as metrics from P9, and a resume-token audit so a stream that silently stops is visible as a number rather than as a user saying the workspace went quiet. Then extend the same credential path to any new stream by construction — the sweep from the success criteria is what makes that automatic.
<br>**Depends on:** WS-D3
<br>**Risk.** The highest-uncertainty item in this cluster. A query-parameter token puts a credential in every access log and every browser history entry; a cookie reintroduces CSRF surface that the current bearer-only design deliberately avoids;


#### WS-P12 · Supply chain: dependency scanning, pinned bases, and a security job that can fail

**What it is.** The .github directory contains exactly one file, workflows/ci.yml. There is no dependabot.yml, no CodeQL workflow, and ci.yml has zero hits for audit, codeql, trivy, snyk, bandit, semgrep or safety. So no automated check ever looks at a dependency for a known vulnerability, at a container image for a known CVE, or at the Python source for a common security defect.

**Why.** This is the cheapest workstream in the cluster and it closes the loop on the rest of it. The prod-ready bar includes knowing when something you depend on has a published vulnerability, and right now that knowledge arrives only when a human happens to read an advisory — which the pnpm overrides prove is already happening manually.

**How.** 1) Add .github/dependabot.yml covering the uv/pip ecosystem (root pyproject.toml, and the workspace members), npm (apps/web via pnpm-lock.yaml, apps/copilot-runtime via package-lock.json), docker (the five Dockerfiles), and github-actions. Grouped weekly PRs so it does not produce noise. 2) Add a `security` job to ci.yml: `uv run pip-audit` (or `uv export | pip-audit -r -`) for Python advisories, `pnpm audit --audit-level high` for the web tree, and Trivy against the built images from P3's `images` job — reusing that job's build rather than rebuilding. 3) Add CodeQL for python and javascript-typescript on a schedule plus PR. 4) Pin the five base images by digest, which Dependabot's docker ecosystem then keeps current.

**Criteria:**

- Dependency updates arrive automatically
  <br>`test -f .github/dependabot.yml and it declares >= 4 ecosystems (uv/pip, npm x2, docker, github-actions). FAILS TODAY: .github contains only workflows/ci.yml.`
- A known-vulnerable dependency fails CI
  <br>`the new `security` job runs pip-audit and pnpm audit. Prove it can fail by pinning a package with a published high-severity advisory on a scratch branch and confirming the job goes red; revert.`
- Images are scanned
  <br>`grep -c 'trivy\|codeql' .github/workflows/*.yml >= 2. FAILS TODAY: 0.`
- Base images are pinned by digest — **CORRECTED 2026-08-22:** the original asserted `== 5` against six Dockerfiles, one of which is multi-stage and carries two `FROM` lines. State it as a property instead: no `FROM` (or `COPY --from=`) in `apps/*/Dockerfile*` lacks an `@sha256:`.
  <br>`grep -c 'FROM .*@sha256:' apps/*/Dockerfile* == 5. FAILS TODAY: 0 of 5 (python:3.13-slim and node:22-alpine, unpinned).`
- The deployed web container installs what CI verified
  <br>`the web image build uses a lockfile: grep -c 'npm ci\|--frozen-lockfile' apps/web/Dockerfile* >= 1. FAILS TODAY: apps/web/Dockerfile.dev:5 copies only package.json and :10 runs bare `npm install`.`
- The manual overrides are still needed or removed, not silently stale
  <br>`a check asserting each override in pnpm-workspace.yaml still corresponds to a resolved advisory — or is removed once the transitive dependency moved past it.`

**Review.** Mutation on the two checks that matter: introduce a dependency with a known high-severity advisory and confirm the security job fails, then remove it and confirm green. Separately, unpin one base image digest and confirm nothing breaks (proving the pin is not load-bearing for the build) but that Dependabot subsequently opens a PR for it — the second half is verified by waiting one cycle rather than by a command, and…
<br>**Iterate.** Add an SBOM per image (syft or docker buildx's built-in) published as a CI artifact, and a policy gate that fails on new critical findings rather than on any finding at all — the first pass will produce a backlog of low-severity noise in transitive dependencies, and a job that is always red is a job everyone ignores. The second pass is where the severity threshold gets tuned against real output.
<br>**Depends on:** WS-P3
<br>**Risk.** The first run will produce a wall of findings, most of them transitive and low severity, and the natural response is to set the threshold so high that the job never fires. Set the initial gate at high-and-above with a written note about why, and revisit.
## Part 4 — What the reviewers rejected

Six adversarial reviewers read the six plans. **All six returned `needs-work`. None approved.**
They found 74 criteria that could not fail or could not be run, 68 false claims about the
codebase, 77 missing pieces of work, and 43 sequencing problems.

The corrections below are applied to the criteria above. These are the ones worth reading,
because each is a way a plan can look rigorous and measure nothing.


**1. WS-MEP-1 — grep -c 'PricingTable()' apps/api/src/aleph_api/copilot_cost_callback.py returns 0**

*Why it fails as a criterion:* It is a text-shape check on one line, not a behaviour check, and it is trivially satisfied without fixing anything — renaming the fabrication to `_empty_table()` or `PricingTable(table={})` makes it pass while the agent path still records `unknown`. Verified: the only match today is copilot_cost_callback.py:195 inside `_resolve_pricing`, which also MEMOISES its result (`self._pricing = PricingTable()`); the plan says to mirror `_resolve_session_maker` at :180-189, but that method deliberately does NOT cache. If the new lazy read caches on a miss, criterion 4 (recovery without restart) is silen

*Replaced with:* Assert identity and recovery in one test: `h = AgentCostCallbackHandler(model=M, purpose='p')` (exactly the copilot_agent.py:1506 shape), with no runtime bound, records pricing_source='unknown' and emits the distinct 'no pricing table bound' log; then bind_runtime(pricing=PRICING) and assert `h._resolve_pricing() is PRICING` (object identity, so in-place `refresh_pricing` merges reach it) and that a second call records 'gateway'.


**2. WS-MEP-1 — grep -c ALEPH_MODEL_HINTS_PATH deploy/compose/.env.example returns >= 1 (returns 0 today)**

*Why it fails as a criterion:* Baseline is right (0 today) but satisfying it BREAKS an existing green test. apps/api/tests/unit/test_env_settings_reconciled.py::test_env_aleph_keys_map_to_settings_fields asserts every `ALEPH_*` key in .env.example maps to a field on `Settings` or `WorkerSettings` unless listed in `ALEPH_KEY_IGNORE`. `ALEPH_MODEL_HINTS_PATH` is read via `os.environ` at packages/aleph-models/src/aleph_models/hints.py:72 and is a field on neither model. The same trap catches MEP-2's proposed ALEPH_GATEWAY_MAX_CONCURRENCY / ALEPH_GATEWAY_RPM and MEP-4's ALEPH_SECRET_KEY. The plan never mentions this test.

*Replaced with:* 'ALEPH_MODEL_HINTS_PATH is in .env.example AND `uv run pytest apps/api/tests/unit/test_env_settings_reconciled.py -q` exits 0' — which forces the honest choice: promote it to a real settings field (and change hints.py to read the field, not os.environ), or add a justified ALEPH_KEY_IGNORE entry.


**3. WS-MEP-2 — grep -rn 'Semaphore\|AsyncLimiter' packages/aleph-runtime/src/aleph_runtime/capabilities.py returns >= 1 line**

*Why it fails as a criterion:* A grep for an implementation keyword inside one file proves a symbol was typed, not that anything is bounded. It also cannot fail in any interesting way — a `Semaphore` created and never acquired passes. The paired max-in-flight test is the only part that carries weight; the grep should be dropped, not kept alongside it.

*Replaced with:* Drop the grep. Keep only: a test firing 20 concurrent LiteLLMClient.chat() calls plus one autoconfigure probe sweep at a fake gateway that records peak in-flight, asserting peak <= configured ceiling; and a second assertion that peak == ceiling (i.e. the limiter is saturating, not accidentally serialising to 1).


**4. WS-MEP-2 — grep -rn '"rerank"\|Capability.RERANK' apps packages --include=*.py --include=*.tsx | grep -v tests | wc -l returns exactly 1. Returns 4 today.**

*Why it fails as a criterion:* Returns 3 today, not 4, and the cited web line numbers are wrong (the hits are Drawers.tsx:189 and :201, not :88 and :107). Worse, the criterion can pass while dead code survives: Drawers.tsx:201 is `rerank: "Reorders retrieved chunks",` — an UNQUOTED object key, so it never matches `"rerank"`. Delete the policy and Drawers.tsx:189 and the count hits 1 with orphaned help text still shipping.

*Replaced with:* A two-way sweep test (in the style of scripts/_lib/surface_bindings.py): every `Capability` member offered by the Settings picker must appear in `CAPABILITY_POLICIES`, and every member in `CAPABILITY_POLICIES` must have at least one resolving call site. It fails on both an unresolvable offer and an orphan help string.


**5. WS-MEP-2 — Two /readyz hits one second apart produce exactly one outbound gateway request**

*Why it fails as a criterion:* Measurable and it can fail, but it encodes a regression the plan does not acknowledge. deploy/compose/docker-compose.yml:162-170 uses /readyz as the API's readiness probe for `up -d --wait`. Serving the gateway leg from GatewayCatalog's cached view (TTL 300s, discovery.py:531-537) makes the stack report READY for up to five minutes after the gateway dies, and makes `--wait` return green against a dead endpoint — which is exactly the failure the healthcheck comment at docker-compose.yml:163-164 says /readyz was chosen to prevent.

*Replaced with:* '/readyz reports the gateway leg with an explicit `checked_age_s`, never older than N seconds, and flips to not_ready once the last successful probe is older than N' — plus a test with an injected clock: gateway goes down, advance past N, assert 503. That bounds request rate AND keeps readiness meaningful.


**6. WS-MEP-3 — grep -rn 'local-llm\|local-gateway' CLAUDE.md docs/ | wc -l returns 0. Returns 5 today.**

*Why it fails as a criterion:* Returns 8 today, not 5. The three the plan missed are docs/research/compose-deployment.md:154, :175 and :728, where `local-llm` is described as one profile option in a design study. Driving this grep to 0 forces edits to a research artifact that is legitimately describing a considered-and-rejected option, which is not the defect being fixed.

*Replaced with:* Scope it to the four load-bearing docs: `grep -rn 'local-llm\|local-gateway' CLAUDE.md docs/operations.md docs/architecture.md docs/acceptance.md | wc -l` returns 0 (returns 5 today), and leave docs/research/ out of the sweep's scope explicitly.


**7. WS-MEP-3 — grep -rl 'FakeGateway' apps packages | wc -l returns >= 3, with exactly one file defining it**

*Why it fails as a criterion:* Measurable, but the plan states no mechanism that satisfies it in this workspace, and every obvious mechanism collides with an existing constraint. tests/conftest.py is scoped to the tests/ subtree — pytest will not apply it to packages/aleph-models/tests/ — so it cannot host a cross-package fixture. A new workspace package breaks docs/acceptance.md E4, which asserts the count does not grow and pyproject.toml lines 12-32 already list exactly 21. A sys.path shim is precisely the pattern commit 483816d removed from scripts/check-graph-state-keys.sh for making a gate depend on the test suite exis

*Replaced with:* Name the home and measure that instead: 'the fake lives at packages/aleph-models/src/aleph_models/testing/gateway.py, is exported from the installed distribution, is imported by tests in >= 3 packages, and `[tool.uv.workspace] members` still lists 23 entries. Not `grep -c members pyproject.toml`, which counts LINES containing the word and so returns 1 no matter how the list grows; and not 21, which was already wrong when written.' That is checkable and it forces the placement decision now rather than at implementation time.


**8. WS-MEP-4 — grep -rn 'app.state.litellm' apps/api/src apps/workers/src | wc -l returns 0. Returns 2 today.**

*Why it fails as a criterion:* Returns 3 today (deps.py:43, routes/health.py:49, routes/assistant.py:230). More importantly it measures the wrong surface: it can go green while per-project isolation is still broken, because the other process-wide gateway singletons are not named — `app.state.gateway_catalog` (routes/model_profile.py:71 and :134), `app.state.gateway_http` (routes/model_profile.py:149), and in workers `ctx['litellm_client']` / `ctx['gateway_catalog']` (arq.py:91, :99) consumed by nine job modules. `GET /v1/gateway/models` reading the boot catalog is the single most user-visible way this feature fails, and thi

*Replaced with:* 'grep -rnE "app\\.state\\.(litellm|gateway_catalog|gateway_http|pricing)" apps/api/src | wc -l returns 0' (returns 6 today) AND 'grep -rn "ctx\\[.(litellm_client|gateway_catalog).\\]" apps/workers/src | wc -l returns 0' (returns 11 today), each paired with the two-fake request-counter isolation test.


**9. WS-MEP-5 — All four Playwright criteria (real error text, disabled unreachable option, key never reaches the browser, add-and-test flow)**

*Why it fails as a criterion:* They cannot be run at all. There is no tests/playwright directory (`ls tests/` returns only conftest.py, integration/, unit/), no `@playwright/test` in apps/web/package.json (scripts are dev/build/preview/typecheck/lint only) and none at the repo root, and pnpm-workspace.yaml:3 still declares `tests/playwright` as a workspace member of a directory that does not exist. Commit 483816d rebuilt tests/ deliberately without e2e or playwright. Building the harness — config, fixtures, a booted stack or a mocked API, CI wiring — is unscheduled and unestimated, and the plan budgets almost nothing fo

*Replaced with:* Either (a) add an explicit MEP-5 precondition 'tests/playwright exists, `pnpm -C tests/playwright test` runs and one deliberately-broken spec is observed red' with its own estimate, or (b) restate these four as Vitest/RTL component tests plus one API-level assertion, e.g. 'a response-body test asserts the plaintext key string appears in no response from any /gateway-endpoints route, and a component test asserts the option for a model with reachable:false has the disabled attribute'.


**10. WS-MEP-6 — Per-project graphs do not leak memory — cache holds at most N entries after 50 distinct signatures**

*Why it fails as a criterion:* N is never given, so any implementation satisfies it and the criterion cannot fail. Each cached graph carries middleware plus six subagents (verified: build_assistant_deep_agent passes six subagents at copilot_agent.py:1645-1652), so the bound is the whole point of the criterion.

*Replaced with:* 'After 50 distinct (endpoint_id, bindings-hash) signatures the cache holds exactly 8 entries and the 42 evicted graphs are unreferenced (assert via weakref or gc), and RSS growth over the 50 builds is under a stated ceiling.'


**11. WS-MEP-6 — grep -n 'claude-sonnet-4-6' apps/api/src/aleph_api/copilot_agent.py | wc -l returns 0**

*Why it fails as a criterion:* Baseline verified correct (1 today, line 1436). But it leaves the same defect class alive two hundred lines away and calls the job done: `available = ["aleph-dev", "aleph-production"]` is hardcoded at copilot_agent.py:1306 and the same pair is asserted in set_model_profile's docstring at :1261 ('one of "aleph-dev" (Sonnet/Haiku) or "aleph-production" (Opus/Sonnet)'), which is a committed claim about models on someone else's gateway — the exact thing pricing.py:8-12 says was removed.

*Replaced with:* 'grep -nE "claude-(sonnet|opus|haiku)|aleph-dev|aleph-production" apps/api/src/aleph_api/copilot_agent.py | wc -l returns 0' (returns 4 today: lines 1261, 1306, 1436, 1444), with the tool's template list served from GET /v1/model-profile-templates instead.


**12. WS-MEP-7 — The effect of a profile is a recorded number, not an opinion — 'the criterion is that the number is produced and recorded, not that it clears a thresh**

*Why it fails as a criterion:* Stated outright as unable to fail. That is honest, but it means nothing gates profile content, and the workstream's own risk section says 'a profile that helps by opinion and hurts by measurement is the default outcome'. A criterion that admits it cannot fail is a note, not a gate.

*Replaced with:* 'The eval runs both arms and the check fails if the profiled arm's tool-call error rate is worse than the unprofiled arm by more than X points, or if either arm produced no number.' Set X generously on the first pass; a regression gate that only catches making things worse is still a gate.


**13. WS-MEP-7 — Summarisation fires at the right point for a small-window model — compute_summarization_defaults returns ('fraction', 0.85) for a binding declaring an**

*Why it fails as a criterion:* The mechanism is verified real (I ran it: no profile gives {'trigger': ('tokens', 170000)}, profile={'max_input_tokens': 8192} gives {'trigger': ('fraction', 0.85)}), but the criterion tests a fixture, not the production path, and the production path cannot supply a true window. ModelBindingIn.max_input_tokens defaults to 200_000 (packages/aleph-core/src/aleph_core/schemas/model_profile.py:58); the UI writes `model.max_input_tokens ?? 200000` (Drawers.tsx:235); and the seeded templates hardcode 200000 (apps/api/alembic/versions/20260527_1200_inc0_initial.py:33 onward). For an Ollama model with

*Replaced with:* Add a second, end-to-end criterion: 'a model the fake advertises with an 8192 window, autoconfigured through POST /model-profile/autoconfigure, produces a binding whose max_input_tokens is 8192, and the graph built for that project triggers summarisation at ~6963 tokens' — plus 'a model reporting no window is left with max_input_tokens unset and the built model carries no profile, rather than silently claiming 200k'.


**14. WS-P1 — Every test file CLAUDE.md and the route docstrings name as a defect pin actually exists — grep -ohE 'tests/[a-z0-9_/]+\.py' CLAUDE.md apps/api/src/ale**

*Why it fails as a criterion:* I ran the grep verbatim. It emits seven paths, two of which are substrings sliced out of longer real paths: CLAUDE.md:326 cites `packages/aleph-rks/tests/test_claim_grounding.py` and the regex extracts `tests/test_claim_grounding.py`; CLAUDE.md:327 yields `tests/test_chunk_offsets.py`. Neither exists at the extracted path (the real files are packages/aleph-rks/tests/test_claim_grounding.py and .../test_chunk_offsets.py), so `test -f` fails forever — the criterion cannot exit 0 even after all six e2e files are restored. It also scans only two files, missing apps/api/tests/unit/test_scholar_rout

*Replaced with:* A script scripts/check-dead-refs.sh that extracts path-like tokens anchored at a path boundary (^|[^A-Za-z0-9_/-]) from CLAUDE.md, docs/*.md, scripts/acceptance.sh, scripts/_acceptance/self_check.sh and every .py under apps/ and packages/, resolves each against the repo root, and exits 1 naming the file:line of any that does not resolve. Prove it fails by `git mv packages/aleph-rks/tests/test_rrf.py /tmp/`.


**15. WS-P1 — No route answers 500 on a well-formed request for a real project — test_route_smoke.py enumerates app.routes and asserts 2xx/404**

*Why it fails as a criterion:* Three GET routes whose only path parameter is {project_id} are unbounded SSE streams: agent_events.py:87 `/{project_id}/agent-events/stream`, changes.py:137 `/{project_id}/changes/stream`, surfaces.py:207 `/{project_id}/surfaces/stream`. A test that GETs every such route hangs rather than fails. Separately, accepting 404 means a route that 404s because the fixture built the wrong object passes silently — the criterion cannot distinguish 'correct' from 'never reached'.

*Replaced with:* Two tests. (a) For non-stream project-scoped GETs, assert an explicit expected status per route from a table checked into the test, with an assertion that the table covers every enumerated route (so a new router fails the test until it is listed). (b) For routes whose response media_type is text/event-stream, assert the first SSE frame arrives within 5s under an asyncio timeout.


**16. WS-P1 — The whole integration job stays green in CI against the pgvector service — the existing python-integration job (ci.yml lines 96-135) passes on `uv run**

*Why it fails as a criterion:* apps/api/src/aleph_api/settings.py declares nine fields with no default: database_url, redis_url, langfuse_host, langfuse_public_key, langfuse_secret_key, otel_exporter_otlp_endpoint, litellm_base_url, insights_litellm_api_key, aleph_agent_token_secret. The python-integration job sets only DATABASE_URL, REDIS_URL and ALEPH_AUTH_MODE, and CI has no .env for pydantic-settings to read. Booting the real app there raises pydantic ValidationError on seven missing settings, so 'no new steps' is false.

*Replaced with:* 'python-integration passes after adding the seven named env vars to its `env:` block; unsetting any one of them makes the new `app` fixture fail with a pydantic ValidationError that names the missing setting.' Verify by deleting one var on a scratch branch and confirming the job is red.


**17. WS-P1 — tests/integration/test_capability_probes.py asserts len(ready) == 10 against the names parsed from apps/api/aleph.toml**

*Why it fails as a criterion:* There is no READY state. packages/aleph-kernel/src/aleph_kernel/spec.py:22-37 defines ProbeResult as `passed: bool` with ok()/problem(), and apps/api/src/aleph_api/lifespan.py:63-67 states a failed probe unwinds and aborts the boot. So if the `app` fixture yields at all, every probe has already passed — the assertion is a tautology over the fixture and cannot fail independently.

*Replaced with:* Assert the mounted capability names equal the ten parsed from apps/api/aleph.toml (this catches a manifest edit), AND add a mutation probe to scripts/_acceptance/self_check.sh that rewrites one probe body to `return problem("forced")` and requires the fixture to raise. Add the same for apps/workers/aleph.toml, which no test touches at all.


**18. WS-P2 — The log line also names the principal and, where the route is project-scoped, the project — assert 'user_id' and 'project_id' keys on the 'unhandled e**

*Why it fails as a criterion:* AuthMiddleware is a BaseHTTPMiddleware (apps/api/src/aleph_api/middleware/auth.py:26) and calls bind_request_context, which does structlog.contextvars.bind_contextvars (packages/aleph-observability/src/aleph_observability/logging.py:62). Under BaseHTTPMiddleware, call_next runs the downstream app in a task spawned inside dispatch, so contextvars bound by an inner middleware do not propagate back to an outer one's frame. ErrorMiddleware is outer to Auth in every ordering the plan proposes, so it will never see user_id/project_id — the criterion cannot pass by reordering alone, and the plan's ap

*Replaced with:* 'A 500 raised from a route under /v1/projects/{project_id}/ produces a log record carrying request_id, user_id and project_id, and the RFC 7807 body carries the request id.' Implement by binding in a pure-ASGI middleware (not BaseHTTPMiddleware) or by reading request.state.principal / request.scope['path_params'] inside ErrorMiddleware's handler. Pin with a mutation that removes the binding.


**19. WS-P3 — LITELLM_BASE_URL=http://127.0.0.1:1 docker compose -f deploy/compose/docker-compose.yml up -d --wait exits 0**

*Why it fails as a criterion:* LITELLM_BASE_URL appears nowhere in deploy/compose/docker-compose.yml (verified by grep — zero hits). It reaches api/workers only through `env_file: [.env]` on the x-app-env anchor (line 29). Compose precedence means an env_file value is used as-is; a shell variable of the same name is neither interpolated into the file nor injected into the container. The command as written leaves the gateway URL at whatever .env says, so the criterion tests nothing.

*Replaced with:* First add `LITELLM_BASE_URL: ${LITELLM_BASE_URL:?}` to the x-app-env `environment:` block so it becomes interpolatable, then: 'LITELLM_BASE_URL=http://127.0.0.1:1 docker compose up -d --wait exits 0 and curl -sf localhost:8000/readyz returns 200 with .checks.litellm_gateway.ok == false'. Or run against a scratch env file written by the test.


**20. WS-P3 — A placeholder secret does not boot — ALEPH_AGENT_TOKEN_SECRET=CHANGE-ME-... uv run python -c 'from aleph_api.main import create_app; create_app()' exi**

*Why it fails as a criterion:* create_app() constructs no Settings. apps/api/src/aleph_api/main.py:52-57 builds FastAPI(lifespan=lifespan) and reads only os.environ['ALEPH_CORS_ORIGINS']; get_settings() is called inside lifespan. I ran `cd /tmp && env -i .venv/bin/python -c 'from aleph_api.main import create_app; create_app()'` with a completely empty environment and it printed BOOTED OK, rc=0. So the probe can neither fail today nor detect the fix unless the guard is deliberately (and wrongly) put in create_app.

*Replaced with:* 'ALEPH_AGENT_TOKEN_SECRET=CHANGE-ME-run-openssl-rand-hex-32 plus a valid DATABASE_URL/REDIS_URL, driven through the lifespan (LifespanManager or `docker compose up api`), exits nonzero and the message names ALEPH_AGENT_TOKEN_SECRET. With a 32-byte random value it boots.' Same fix applies to P7 criterion 4.


**21. WS-P7 — The signing key and the credential-encryption key are different settings — grep -rn 'aleph_agent_token_secret' apps/api/src/aleph_api/routes/ packages**

*Why it fails as a criterion:* That grep returns 9 hits in routes/ today, and 7 of them are the secret doing its correct job — minting agent tokens: agent_tokens.py:86, reviews.py:142, artifacts.py:180, projects.py:190, sources.py:167, viz.py:85, synthesize.py:96. Only scholar.py:204 and connector_credentials.py:47 are the encryption misuse. Reaching 0 would require removing agent-token minting from the API. The plan's 'FAILS TODAY: 3 sites do' is also wrong for the same reason.

*Replaced with:* 'grep -rn "aleph_agent_token_secret" apps packages --include=*.py | grep -c "encode(\"utf-8\")" == 0' plus a unit test asserting CredentialCipher is only ever constructed from settings.aleph_credential_master_key, enforced by an AST check over every CredentialCipher(...) call site.


**22. WS-P7 — The key derivation exists exactly once — grep -rn 'sha256(.*master' apps packages --include='*.py' | wc -l == 1**

*Why it fails as a criterion:* The two hits today are packages/aleph-connectors/src/aleph_connectors/credentials.py:76 (a docstring, 'HKDF-ish: sha256(master_secret || project_id)') and :87 (the implementation). Reaching 1 means deleting a docstring line, and the grep cannot tell a comment from code — a fourth copy added inside a comment would satisfy it while a legitimate rename would break it.

*Replaced with:* An AST-based unit test: exactly one FunctionDef in apps/ + packages/ contains a hashlib.sha256 call whose argument references a name containing 'master', and the three former call sites (scholar.py, connector_credentials.py, aleph_research/tools.py) import that function by name.
## Part 5 — Horizon: adopt, trial, assess, hold

Five independent scans with deliberately different lenses, each required to name the
number that would move. **63 opportunities: 45 adopt, 10 trial, 3 assess, 5 hold.**


### Production readiness


**`ADOPT` · Decouple /readyz from the model gateway, and make gateway status a first-class application state**
<br>`apps/api/src/aleph_api/routes/health.py:48-56` folds `litellm_gateway` into `all_ok`, so an unreachable operator gateway makes `/readyz` return 503. `deploy/compose/docker-compose.yml:165` uses exactly that endpoint as the API healthcheck, and `copilot-runtime` (line 253) and `web` (line 285) both declare `depends_on: api: condition: service_healthy`.
<br>*Why here:* `deploy/README.md:52` promises the opposite in bold: "**Readiness does not depend on the model endpoint.** The stack comes up whether or not your models are reachable." That statement is false against the code. Aleph's whole deployment thesis is "point it at any OpenAI-compatible endpoint" — the single most likely first-run mistake is a wrong URL, and today that mistake produces a crash-looping st…
<br>*Win:* With `LITELLM_BASE_URL` pointed at a closed port: `docker compose up -d --wait` currently exits non-zero and `web` never starts. Target: exits 0, `docker compose ps --format '{{.Service}} {{.Health}}'` shows 6/6 core services healthy, `curl -s localhost:8000/readyz` returns 200, `curl -s localhost:8000/v1/gateway/statu…


**`ADOPT` · Compose runtime hardening: restart policies, resource limits, log rotation, and loopback-only data ports — enforced by a…**
<br>`deploy/compose/docker-compose.yml` declares 13 services. `grep -n 'restart:'` returns exactly one line (144, `restart: "no"` on the one-shot `migrate`), so **12 long-running services have Docker's default `restart: no`** — a host reboot or an OOM kill leaves them down permanently. `mem_limit` appears once (line 237, `code-runner`), so postgres, redis, api and workers are unbounded.
<br>*Why here:* This is a self-hosted docker compose product — the compose file *is* the deployment. Every one of these is a difference between "ran on my laptop once" and "runs unattended". The Redis exposure is the sharpest: `deploy/compose/docker-compose.yml:100-118` builds an elaborate two-Redis isolation story so the sandbox cannot see agent tokens, and then publishes the privileged Redis on all interfaces w…
<br>*Win:* Four counts, each currently at its worst value, measured by the new script: services with a restart policy 0/12 → 12/12; services with a memory limit 1/12 → 12/12; services with `logging.options.max-size` 0/13 → 13/13; data-store ports bound to loopback 0/4 → 4/4 (postgres, redis, clickhouse, minio).


**`ADOPT` · Ship a production web image — the deployed container is a Vite dev server built from an unpinned `npm install`**
<br>`apps/web/Dockerfile.dev` is the **only** Dockerfile for the web app (`ls apps/web/Dockerfile*` returns one file), and `deploy/compose/docker-compose.yml:272` builds the `web` service from it. Its `CMD` is `npm run dev -- --host 0.0.0.0`.
<br>*Why here:* `apps/copilot-runtime/Dockerfile:7-15` contains a comment documenting this exact bug class having already bitten this project: "The manifest previously floated: it declared `@copilotkit/runtime ^1.58` and resolved 1.63.2… The deployed container only worked because of that accidental float." That lesson was applied to the Node bridge and not to the web app, where the identical hazard is live.
<br>*Win:* Three numbers: `docker build` of the web image twice, 24h apart, currently can yield different `node_modules` trees → target is byte-identical `pnpm-lock.yaml`-derived resolution, checked by `pnpm install --frozen-lockfile` failing the build on drift.


**`ADOPT` · Non-root containers and digest-pinned base images, with a sweep**
<br>`grep -c '^USER' apps/*/Dockerfile*` returns 1 across 5 Dockerfiles — only `apps/code-runner/Dockerfile:33-34` drops to uid 10001. `apps/api/Dockerfile` and `apps/workers/Dockerfile` run as root, and their comments say "the container may run as an arbitrary uid (compose sets `user:` for the shared asset bind mount)" — but `grep -n 'user:' deploy/compose/docker-compose.yml` returns nothing, and `sc…
<br>*Why here:* The API container holds `INSIGHTS_LITELLM_API_KEY`, `POSTGRES_PASSWORD` and `ALEPH_AGENT_TOKEN_SECRET` in its environment, and it hosts the agent in-process. Root inside that container plus a writable `/app` is the difference between a container escape being hard and being routine.
<br>*Win:* `grep -c '^USER ' apps/*/Dockerfile*` 1/5 → 5/5, and `docker compose exec api id -u` returns non-zero. Digest pins: 0/10 external image references → 10/10, checked by `scripts/check-compose-hardening.sh` grepping for `image:` lines without `@sha256:`. Self-check by removing one pin.


**`ADOPT` · Backup and restore, proven by an automated restore test — not by having a backup script**
<br>There is no backup anything: `grep -rniE 'pg_dump|backup' deploy/ scripts/` finds only `scripts/_acceptance/self_check.sh` backing up *source files* during a mutation test. Add a `tools`-profile compose service running `pg_dump -Fc` on a schedule plus an assets tar, and — the part that matters — `scripts/restore-drill.sh` that restores the dump into a scratch database, runs `alembic check` against…
<br>*Why here:* `deploy/README.md:113-118` documents `down -v` as "deletes every project, source and claim. There is no undo." That is honest and also the whole problem: the durable knowledge layer this product exists to build — claims, evidence anchors, the hash-chained action ledger — lives in one unbacked Postgres volume plus a `data/assets` bind mount.
<br>*Win:* Restore drill runtime and outcome, both currently undefined: target `./scripts/restore-drill.sh` exits 0, prints rows restored per table, and asserts `verify_project_chain(...).ok` for 100% of projects. Add it to `scripts/acceptance.sh` as a new part so a broken drill shows as FAIL rather than SKIP.


**`ADOPT` · Prove migration rollback in CI with a downgrade round-trip per revision**
<br>25 revisions in `apps/api/alembic/versions/`, and every one has a `downgrade()` body — several substantial (50 lines in `20260726_0900_drop_budgets`, 42 in `20260815_1200_claim_spine`). But `grep -rn downgrade .github/ scripts/ tests/` returns nothing: **no downgrade has ever been executed**.
<br>*Why here:* CI already enforces `alembic check` for forward drift (`.github/workflows/ci.yml`, `alembic check (no model drift)`), so the forward path is genuinely guarded and the reverse path is entirely unguarded. This is precisely the defect shape CLAUDE.md names as dominant — 25 downgrade functions written correctly and read by nothing.
<br>*Win:* Revisions with a proven-executable downgrade: 0/25 → 25/25, measured by a CI step that loops `alembic downgrade -1` back to base and up again. Expect this to fail on first run — that failure is the value. Self-check by breaking one `downgrade()` and asserting CI goes red.


**`ADOPT` · Supply chain: SBOM, vulnerability scan, secret scan, and automated dependency updates**
<br>`grep -rniE 'sbom|syft|grype|trivy|cyclonedx|pip-audit|osv|gitleaks|codeql|semgrep|bandit'` over the repo returns **zero hits**, and `.github/` contains exactly one file (`workflows/ci.yml`) — no `dependabot.yml`, no Renovate config.
<br>*Why here:* Aleph is distributed as a compose stack that other people run, holding their API keys and their research corpus. The dependency surface is large and fast-moving: `deepagents 0.6.6`, `@copilotkit/*` on carets, `@a2ui/react ^0.10`, `langchain`, `pynacl`, `pyjwt`.
<br>*Win:* Four numbers from zero: CI jobs performing a security scan 0 → 1; known-vulnerable direct dependencies, currently unknown → a published count with a documented policy (e.g. 0 critical, 0 high); SBOM artifacts per release 0 → 5 (one per image); dependency PRs raised per week 0 → whatever Dependabot finds.


**`ADOPT` · Rate limiting, per-project concurrency caps, and spend caps at the gateway boundary**
<br>There is no inbound rate limiting anywhere — `grep -rniE 'rate.?limit|slowapi|limiter|throttl'` over `apps/` finds only outbound throttles (`packages/aleph-scholar/src/aleph_scholar/http.py:35`, the Consensus monthly counter in `consensus.py:49-55`).
<br>*Why here:* `docs/backlog.md` E5 records the live symptom — "Gateway rate limiting… reported as 'weirdly rate limited'; not yet characterised" — and correctly guesses the cause is agent subagent fan-out. Aleph runs the agent **in-process inside FastAPI** on a single uvicorn worker with five SSE endpoints (`agent_events.py:160`, `changes.py:208`, `surfaces.py:312` and `:446`, `assistant`), so one project's run…
<br>*Win:* Three: 429 responses returned by Aleph, currently 0 (it has no mechanism) → a nonzero count under a load test that exceeds the configured budget; concurrent in-flight gateway calls per project, currently unbounded → capped at a configured N, asserted by a test that launches N+1 and observes the (N+1)th queue rather tha…


**`ADOPT` · Prove multi-tenancy isolation with a generated cross-tenant route sweep; assess Postgres RLS underneath it**
<br>Isolation is enforced entirely in application code by `ProjectScopeDep` (`apps/api/src/aleph_api/middleware/project_scope.py`), used 125 times across `apps/api/src/aleph_api/routes/`. It is careful code — the route-template matching comment at lines 28-37 documents a real lockout bug it already fixed.
<br>*Why here:* CLAUDE.md lists "every row carries `project_id`" under *Rules that are real but only held by review*, with the explicit note "No sweep checks this; new models are caught in review or not at all." The same is true of the routes.
<br>*Win:* Sweep: project-scoped routes covered by a cross-tenant refusal assertion, currently 1 of roughly 116 → 116 of 116, printed as a count by the test so it cannot silently shrink. Self-check by removing `ProjectScopeDep` from one handler and asserting the sweep goes red.


**`ADOPT` · Secret hygiene and rotation: refuse placeholders at boot, version the keys, harden on `aleph_env=prod`**
<br>`apps/api/src/aleph_api/settings.py` declares `aleph_agent_token_secret: str` with no minimum length and no validator (the file has zero `field_validator`s across all 103 lines), so a stack started with `.env.example`'s literal `CHANGE-ME-run-openssl-rand-hex-32` boots happily and signs agent tokens with it.
<br>*Why here:* `local` auth mode synthesises a fixed dev principal for every unauthenticated request (`apps/api/src/aleph_api/middleware/auth.py:12-16`). It is the compose default and, per CLAUDE.md, "the only deployed mode". Combined with the 0.0.0.0 port binds, a stack deployed as documented is an unauthenticated API on a routable address.
<br>*Win:* Concretely testable: booting with `ALEPH_ENV=prod ALEPH_AUTH_MODE=local` currently starts the API → target exits non-zero with a named error, pinned by a unit test. Booting with the literal `.env.example` placeholder secret currently starts → target exits non-zero.


**`ADOPT` · Kill the dead checks and the doc drift — an operator following the docs today hits deleted files**
<br>Three verified rots. (1) `audit/checks/e2e/node_modules` is a symlink to `../../../tests/playwright/node_modules`, and `tests/playwright/` no longer exists — so `audit/run.sh:51-55` never sets `E2E_OK=1` and **all six e2e checks silently SKIP forever**, which is the precise failure mode CLAUDE.md warns about under "A green `audit/run.sh` is weaker evidence than it looks".
<br>*Why here:* The owner's standard is "no stale or dead code laying around and every part needs to function as expected", and CLAUDE.md's opening paragraph is an argument that documentation asserting things that are not true is the root cause of the project's worst defect. These three are that exact failure, live in the tree.
<br>*Win:* Three counts. Broken path/profile references across `docs/`, `deploy/README.md` and `scripts/`: currently at least 5 verified (`deploy/local-gateway/`, `--profile local-llm`, `postgres-initdb.sh`, service names `aleph-api`/`aleph-migrate`, the ALEPH_UID mechanism) → 0, enforced by `check-docs-refs.sh` in the `python-qu…


**`TRIAL` · RED metrics, SLOs, and alert rules — an opt-in `metrics` compose profile**
<br>`grep -rniE 'prometheus|/metrics|alertmanager|grafana'` over `apps/`, `packages/`, `deploy/` and `.github/` returns **zero hits**. OTEL tracing exists (`packages/aleph-observability`, `deploy/compose/otel-collector-config.yaml`) and exports to Langfuse, but traces are a debugging tool, not an alerting substrate.
<br>*Why here:* Right now the only production signal is `docker compose ps` and log tailing — `deploy/README.md:100-107` says as much. With no metrics there is no way to state an SLO, which means "everything prod ready" has no evidence behind it.
<br>*Win:* From nothing to something countable: metrics endpoints 0 → 1; named SLOs 0 → 4 (API availability, p95 latency on non-SSE routes, ingest job success rate, gateway call success rate), each with a published target and a burn-rate alert.


**`TRIAL` · A real audit export: cursor-paginated, chain-verified NDJSON**
<br>`apps/api/src/aleph_api/routes/ledger.py:29-49` is the only way to read the action ledger over HTTP. It caps at `limit: Query(ge=1, le=500)`, orders `timestamp.desc()`, and offers `since`/`until` filters — no cursor, no total count, no export format. Paging a long-lived project means walking `until` backwards and hoping no two events share a timestamp.
<br>*Why here:* The hash-chained append-only ledger is one of this system's genuinely strong properties — `tests/integration/test_ledger_immutability.py` proves the UPDATE/DELETE triggers against real Postgres, not a mock, and that is rare and worth something. But an audit trail that cannot be exported is an audit trail that exists only inside the system it is auditing.
<br>*Win:* Maximum ledger events retrievable in one request: 500 → unbounded, measured by exporting a project seeded with 100,000 events and asserting the received line count equals `SELECT count(*)`.


**`TRIAL` · Load testing on the SSE fan-out, and failure injection between Aleph and the gateway**
<br>`grep -rniE 'k6|locust|artillery|vegeta|chaos|toxiproxy'` over `scripts/`, `.github/`, `deploy/` and `docs/` returns nothing outside prose. Add `tests/load/` with a k6 script that opens N concurrent workspace sessions — each holding one multiplexed `SurfaceStreamProvider` SSE connection plus an agent run — and a `--profile chaos` compose overlay putting Toxiproxy between the app and `LITELLM_BASE_…
<br>*Why here:* The specific architecture makes this non-optional rather than nice-to-have: the agent runs **in-process inside FastAPI** on a single uvicorn worker (`apps/api/Dockerfile`, no `--workers`), and the reading region holds one long-lived SSE connection per pane group.
<br>*Win:* Two numbers that do not exist today: concurrent workspace sessions before p95 non-SSE latency exceeds 1s — unknown → a published number the compose defaults are sized against. And behaviour under a 100%-429 gateway: currently unknown → asserted as "UI stays responsive, `/readyz` stays 200, banner shows the gateway stat…


### The case that this plan is wrong


**`ADOPT` · Stop treating acceptance.sh as the gate — it has already produced a false green on the single most load-bearing claim in…**
<br>`scripts/acceptance.sh:287-288` defines E5 ("aleph-belief patch contract is wired or deleted") as `grep -rl 'aleph_belief' apps packages | grep -v packages/aleph-belief | wc -l >= 1`. I ran that exact command: it returns `packages/aleph-wiki/src/aleph_wiki/belief_service.py`, rc=0, so the runner reports `E5 PASS — FIXED — was a known defect`.
<br>*Why here:* CLAUDE.md names `scripts/acceptance.sh` as "the gate to trust" precisely because it counts skips separately and can self-check. That property has already failed. Every strategic decision in `docs/backlog.md` — including "the self-improvement thesis is far closer than section A implies" — was made against a scoreboard reading 38/41 ✅.
<br>*Win:* Two numbers. (1) Acceptance rows whose named test path does not resolve: **currently 11 of 41** (B1,B2,B3,B7,C1,C3,C4,C5,C6,C7,C8) — target 0, measured by a preflight in acceptance.sh that runs `pytest --collect-only <path>` for every row and exits non-zero on any unresolved id.


**`ADOPT` · Replace the "producer with no consumer" grep with a real transitive-reachability gate, and publish the number**
<br>Aleph's stated dominant defect class is "a column, table, or service that is written correctly and read by nothing." I measured it. **2,412 source lines have zero production callers**, guarded by **1,466 lines of test** that keep them green: `aleph_belief/patch.py` (200), `reconcile.py` (285), `trust.py` (76) — imported only by `aleph_wiki/belief_service.py` (512), which is imported by nothing;
<br>*Why here:* This is 8 of the 38 ✅ acceptance parts (A4, A6, D1–D6) going green off code no request ever reaches, plus C0/C5 partially. The kernel's own docstring says the probe requirement is "the structural answer to a codebase whose dominant defect was write paths shipped with no read path" — but probes only run for capabilities in `aleph.toml`, and none of the dead modules are capabilities.
<br>*Win:* `scripts/check-reachable.sh` — BFS the AST import graph from {aleph.toml factories, main.py include_router targets, arq job map, copilot_agent subagent builders} and print unreachable `src/**.py` line count. It reports **2,412 across 11 modules today** (I verified each by grep;


**`ADOPT` · Kill the belief spine as a second knowledge substrate, or force it to ship one consumer this week — it is currently comp…**
<br>Two substrates exist. One is measured and consumed: `aleph_rks.retrieval.search_corpus` fuses pgvector cosine and `ts_rank` with RRF at k=60 over `DocumentChunk`; recall@1 0.91 hybrid vs 0.60 lexical on a 45-pair set (acceptance B6 PASS: `45 question/source pairs`); its consumers are real — `aleph_assistant/retrieval/router.py:361,697` and `aleph_evals/retrieval_eval.py:238`.
<br>*Why here:* `docs/acceptance.md` E1/E2/E3 (delete the wiki) are blocked on "a belief extractor beating 0.91 recall@1" — a bar set by the layer that already works. So the plan is: build a second substrate, prove it beats the first at the first's own job, then delete the third (the wiki). That is three-body work to reach parity.
<br>*Win:* A forcing test with a deadline. Either (a) `grep -rl 'belief_service' apps/ packages/ | grep -v belief_service.py` returns ≥1 **and** an HTTP route returns a claim by `claim_key` with derived confidence (measured: `curl /v1/projects/{id}/claims` returns non-empty against a seeded project), or (b) delete `packages/aleph…


**`ADOPT` · Do not build the A1 plugin system. Prove the boot manifest already is one — today it demonstrably is not, and nothing ch…**
<br>`apps/api/aleph.toml` declares 10 capabilities, 7 `protected = true` and 3 not, with the comment: *"Research capabilities. Not protected: the research suite is a plugin suite, and an operator may legitimately run Aleph without it."* That claim is false in code.
<br>*Why here:* Backlog A1 proposes building a `Plugin` type, registry, activate/deactivate, dependency resolution and an isolation boundary. But `Kernel` already has `register_dynamic` (kernel.py:82), `activate` (:159), `replace` (:240), `deactivate` (:317) and `reprobe` (:289), and I verified by grep that **none of them has a single call site outside `packages/aleph-kernel/tests` and `scripts/_acceptance/`**.
<br>*Win:* One integration test, `test_api_boots_without_the_research_suite`: strip the three non-protected `[[capability]]` blocks from a copy of `aleph.toml`, boot, assert `kernel.boot()` succeeds and that the 7 non-research routes still answer 200 while the research routes answer a clean 501/404. It **fails today**.


**`ADOPT` · Route agent-authored code through code-runner, not `exec()` in the FastAPI process — the guardrail story currently has n…**
<br>`packages/aleph-kernel/src/aleph_kernel/skills.py:118` runs `exec(compile(code, f"<skill:{name}>", "exec"), namespace)`. The AST gate is honest that it is not a defence: `ast_gate.py:19-24` says outright *"It is not a sandbox and not a capability check. A gated module can still do anything Python can do once its functions are called."* It buys one property — loading is not running.
<br>*Why here:* The thesis is "an agent authors plugins for itself." The failure mode when an agent writes a bad skill and it persists is currently: definition-only Python, admitted by a static gate that forbids six module names, then `exec`'d inside the process that holds the DB session factory, the JWKS, the gateway credential and the asset store.
<br>*Win:* Three tests that fail today and must pass: an authored skill that calls `open('/etc/passwd')` is refused or fails closed; an authored skill that imports the app's session factory cannot reach the database;


**`ADOPT` · Reconcile CLAUDE.md with the tree: the wiki is being invested in, not removed, and the document authorising that decisio…**
<br>CLAUDE.md says: *"Treat wiki code as legacy under removal. Do not extend it, do not fix its cosmetics, do not add tests to it. Migrate callers off it."* Measured against the last 12 commits touching `packages/aleph-wiki`: **+6,001 / −51 lines**, including a brand-new 469-line `schema.py`, a 119-line `schema_service.py`, a 590-line `lint.py`, a 209-line `frontmatter.py`, a 303-line `navigation.py`,…
<br>*Why here:* CLAUDE.md's own preamble says the previous version "asserted invariants that were false in code… and that is the single reason a broken retrieval path survived seven work packages." The same failure has recurred, in the same file, about the same subsystem — and this time the rationale document has been deleted, so nobody can even check whether the removal decision still holds.
<br>*Win:* Two checks. (1) A link check over `docs/*.md` + `CLAUDE.md`: dangling relative links **currently 6** (all to `decisions.md`) — target 0. (2) `scripts/check-wiki-frozen.sh`: `git diff --numstat <freeze-ref> HEAD -- packages/aleph-wiki` must show 0 added lines, or the rule is deleted from CLAUDE.md.


**`ADOPT` · Invest in retrieval quality — it is the only measured subsystem, and not one of the backlog's ten prioritised items touc…**
<br>`docs/backlog.md`'s "Suggested order" lists 10 items: model endpoint + harness profiles, agent-writes-a-skill, Inspector, RUN_ERROR, settings panes + UI rebuild, interpreters, RubricMiddleware, UI sweep, catalog composition + plugin system, then async subagents / OKF / E2-E4 / D1-D2.
<br>*Why here:* The owner's standard is "the research capability fully SoTA." Retrieval is the research capability. A 0.91 recall@1 with no reranking and a single-axis eval is a solid 2024 baseline, not 2026 state of the art — and the plan spends its next two quarters on harness plumbing instead.
<br>*Win:* Two numbers on the existing harness. (1) recall@1 on the 45-pair set: 0.91 → target ≥0.95, measured by the unchanged command `uv run python -m aleph_evals.retrieval_eval`, with the reranker on/off reported the way `mode: hybrid|lexical` already is.


**`ADOPT` · Make the instrument-aesthetic spec a ratcheting sweep — the 180 number reproduces exactly and will otherwise regrow**
<br>I reproduced `docs/backlog.md` section G independently across the 43 components in `apps/web/src/components` and `apps/web/src/a2ui/components`: `rounded-(sm|md|lg|xl|2xl|3xl|full)` = **55**, `shadow-(sm|md|lg|xl|2xl)` = **9**, raw Tailwind palette classes (`(text|bg|border|ring|from|to)-<scale>-<n>`) = **116**. Total **180**, matching the backlog exactly.
<br>*Why here:* The owner's standard is that the UI "cannot be half-baked with stale or dead code laying around." A hardcoded `text-slate-500` does not respond to the theme at all, so it renders identically on both grounds — which is why the interface looks right in one theme and wrong in the other. The backlog notes the 11 clean components are, without exception, the ones written during the redesign.
<br>*Win:* `scripts/check-instrument-aesthetic.sh` prints the three counts and exits non-zero above a ceiling committed in the script. Today: 55 / 9 / 116 = 180. Ceiling ratchets down with each cleanup; target 0.


**`TRIAL` · Re-verify the backlog itself — two of its "NOT BUILT" items are already built and dead in the tree**
<br>`docs/backlog.md` opens with *"Verified against the code, not remembered."* Two entries are not. **A4** ("Per-plugin settings cards — NOT BUILT… Missing: a settings contract a plugin declares, a renderer that composes declared cards") — `packages/aleph-a2ui/src/aleph_a2ui/settings_card.py` is 279 lines and exists;
<br>*Why here:* The backlog is the input to this plan, and it is being used to size work. Scoping A4 as greenfield when 279 lines already exist means either rebuilding what is there or discovering it mid-sprint; either way the estimate is wrong.
<br>*Win:* Backlog entries whose status claim is contradicted by a file in the tree: **2 today** (A4, C2), found by name-grepping each entry's named artefact. Target 0, re-run whenever the backlog is edited.


**`ASSESS` · Assess whether the kernel earns 2,120 lines and 143 tests — or whether AsyncExitStack plus the probe convention is the w…**
<br>In production the kernel does two things: order capability setup by declared dependency, and unwind LIFO on shutdown running every inverse even when one raises. That is `contextlib.AsyncExitStack` over a topologically-sorted list.
<br>*Why here:* CLAUDE.md says "the kernel is the product." If that is the bet, the dynamic half needs a production caller within a defined window or it is speculative generality with a 143-test maintenance tax — and every one of those tests is a reason not to delete it later.
<br>*Win:* A dated decision with a number attached. Production call sites of `kernel.register_dynamic|activate|replace|deactivate|reprobe`: **0 today**. Set a review date; if still 0, delete `agent_api.py` (197), the dynamic half of `kernel.py` (~130 of 368), `spawn_ledger.py` (198), and their 5 test files (~880 lines) — kernel s…


**`HOLD` · Cap the beta/preview surface — the backlog puts three unstable Deep Agents APIs on the critical path of the thesis**
<br>`docs/backlog.md`'s H adoption table marks H3 (`RubricMiddleware`) **beta**, H5 (interpreters + dynamic subagents) **beta**, H6 (async subagents) **preview** — 3 of its 8 rows. All three appear in the suggested order (positions 6, 7, 10), and H5 is proposed as the fix for E5's gateway rate limiting.
<br>*Why here:* The agent already runs in-process inside FastAPI (`lifespan.py` → `setup_copilotkit`), so a breaking change in a beta middleware is not a contained failure — it is the request path. And backlog E1 reports the agent is already failing with `RUN_ERROR` on the newest project, un-traced, against the *stable* API.
<br>*Win:* Count of thesis-critical features depending on a beta/preview upstream API: **3 today** (H3, H5, H6) — cap at 1. Enforced by a contract test per adopted beta feature that asserts the imported symbol's signature (`inspect.signature`) matches what Aleph calls, so `uv sync` to a new deepagents fails CI instead of producti…


### Generative UI


**`ADOPT` · Let agent-composed UI land on the Board, not only in the chat dock**
<br>Today the Board renders exactly the seven server-built surfaces declared in `packages/aleph-a2ui/src/aleph_a2ui/pane_registry.py` (wiki, library, artifacts, notes, hypotheses, briefs, grounding). Anything the agent composes — `render_a2ui` surfaces via the A2UI middleware, `generateSandboxedUi` markup — renders inside the CopilotKit chat transcript in `AssistantDock`, on a different transport, and…
<br>*Why here:* Block.tsx's own docstring claims this is solved — "A chat reply, a catalog-assembled surface, agent-written HTML and a third-party panel are the SAME object at different sizes… That is what removes the two-paths problem at the design level rather than patching it in code". It is not solved; there are still two renderers on two transports and they have already drifted.
<br>*Win:* `grep -c 'band="open"\|band="controlled"\|band="third-party"' apps/web/src/components/Board.tsx` goes from 0 to ≥1. Add a sweep `scripts/check-block-bands.sh` asserting every variant of `BlockBand` in Block.tsx has ≥1 producer under `apps/web/src`; it exits 1 today (3 of 4 unproduced) and 0 after.


**`ADOPT` · Make the Grounding surface reachable — the provenance pane is complete on five layers and has zero call sites**
<br>`GroundingSurface` exists end to end: `PaneKind(id="grounding", launchable=False, params=("claim_id",))` in `pane_registry.py`, a builder `_grounding_messages` at `apps/api/src/aleph_api/routes/surfaces.py:923` that walks claim → citation → chunk → char-span → source, a catalog entry at `apps/web/src/a2ui/aleph-catalog-v09.tsx:532`, and a 194-line renderer `apps/web/src/a2ui/components/GroundingSu…
<br>*Why here:* Provenance display is the product. CLAUDE.md leads with claims being "evidence-anchored", records that `chunks_for_claim` now fills `Citation.chunk_ids` on the real write path, and cites `test_chunk_offsets.py` proving `markdown[char_start:char_end] == chunk.text`. All of that work terminates in a surface no user can open.
<br>*Win:* `grep -rc 'openPane("grounding"\|openPane("Grounding"' apps/web/src` goes 0 → ≥1. Add a `"claim"` branch to `_open` and a unit test asserting `_open(target_kind="claim")` returns a `navigate` dict containing a `tab` key — it fails today for every `target_kind` not in the eight listed.


**`ADOPT` · Human-in-the-loop that actually pauses the run — `useHumanInTheLoop` / `useInterrupt`, both already installed and both a…**
<br>`grep -rlw useHumanInTheLoop apps/web/src` → 0. `grep -rlw useInterrupt apps/web/src` → 0. No `humanInTheLoop` prop on `<CopilotKitProvider>` in `apps/web/src/lib/copilot.tsx`. Both hooks are exported by the *installed* `@copilotkit/react-core@1.58.0` (`node_modules/@copilotkit/react-core/dist/v2/index.d.mts`), so no upgrade is required to start.
<br>*Why here:* CLAUDE.md's stated product is an agent that authors plugins for itself "with guardrails preventing it from removing load-bearing capability". A guardrail that fires after the write is not a guardrail. The AG-UI `Interrupt` type carries a `responseSchema` (JSON Schema for the answer), which means the approval form is *generated from the schema* — a new plugin needs no frontend work to become approv…
<br>*Win:* `grep -rlw 'useHumanInTheLoop\|useInterrupt' apps/web/src` goes 0 → ≥1 file. A Playwright test that triggers a gated tool and asserts the run is *blocked*: assert no `ActionLedgerEvent` row for the target action exists while the approval card is on screen, and exactly one appears after Approve.


**`ADOPT` · Card actions have no pending state, no error path, and fire three invalidations that match no query**
<br>Every A2UI card action funnels through `adapt()` in `apps/web/src/a2ui/aleph-catalog-v09.tsx:112-171`. Three defects, all verified: 1. `action.isPending` is computed by `useMutation` and never passed to the view — the `onAction` signature returns `void` (`_shared.tsx:5-8`). Click Approve and nothing changes until the SSE surface stream happens to push. Double-clicks fire two POSTs. 2.
<br>*Why here:* The owner's standard is "every part needs to function as expected". A button that gives no feedback for 300ms and no feedback at all on failure reads as broken, and a no-op cache invalidation is precisely the "looks right in isolation" failure CLAUDE.md is written to prevent. It also makes the ApprovalCard racy: nothing stops two approvals of the same proposal.
<br>*Win:* Three counts, all currently 0: files under `apps/web/src` referencing `useOptimistic` or an `onMutate` rollback; `onError` handlers on card mutations; `ErrorBoundary` components.


**`ADOPT` · The Block's lifecycle indicator cannot report a still-arriving surface, and three of its buttons do nothing**
<br>`Block.tsx`'s docstring names four things it says are "primary rather than metadata": trust, band, lifecycle, and the verbs. Three of the four are inert on the only page that renders them. - `Board.tsx:232-238`: `error ? "failed" : surface ? "settled" : connected ? "building" : "building"` — the last two branches are identical, and the `stale` state has no producer at all.
<br>*Why here:* "Cannot be half-baked with stale or dead code laying around" is the owner's stated bar, and this is the most-looked-at component in the product failing it in three ways at once. The trust meter is worse than dead — it is actively misleading, asserting "signed" over content the agent composed.
<br>*Win:* `grep -c '=> undefined' apps/web/src/components/Board.tsx` goes 3 → 0. A sweep asserting no ternary in `apps/web/src` has identical branches: 2 hits today (`Board.tsx:236`, `PipelineStrip.tsx:61`), 0 after. `grep -c 'trust="' apps/web/src/components/Board.tsx` where the value is a literal: 1 → 0.


**`ADOPT` · The Board is a spatial canvas with no keyboard access at all**
<br>Across 8,838 lines of `apps/web/src` there is exactly **one** `onKeyDown` (`a2ui/components/NoteEditorCard.tsx:71`) and **zero** `tabIndex` attributes. Blocks are moved and resized only by `onPointerDown`/`onPointerMove` (`Board.tsx:118-176`). There is no way to focus a block, move it, resize it, or cycle between blocks from the keyboard, and no command palette.
<br>*Why here:* An analyst adjudicating a contested claim moves between a page, its source and its grounding tree dozens of times an hour. Requiring a mouse for every one of those is a throughput cost, not just a compliance one — and a spatial canvas is precisely the layout where keyboard navigation is hardest to retrofit later.
<br>*Win:* Four counts, all reproducible with grep: `tabIndex` 0 → ≥1 per block; `onKeyDown` 1 → ≥4; `aria-live` in live (non-dead) modules 0 → ≥2; Escape/focus-trap handlers on `role="dialog"` 0 → 1.


**`ADOPT` · Delete 716 lines of dead frontend and add a sweep so it cannot come back**
<br>A module-graph walk from `main.tsx` over `apps/web/src` (58 files, 8,838 lines) finds four modules with zero importers anywhere in the repository: - `components/ActivityCard.tsx` — 349 lines, the only user of `useAgent`, so the sole consumer of the agent's streamed `todos` plan is dead - `a2ui/A2UISurfaceView.tsx` — 213 lines, all three exports (`surfaceSeq`, `A2UISurfaceView`, `A2UIStreamSurfaceV…
<br>*Why here:* This is the direct answer to "cannot be half-baked with stale or dead code laying around". It also actively misleads: `ActivityCard` is the only code that ever consumed the agent's live plan, so its death silently removed the plan view; a reader grepping for `useAgent` concludes Aleph consumes agent state when nothing does.
<br>*Win:* `scripts/check-dead-modules.sh` (a ~30-line module-graph walk from `main.tsx`, honouring side-effect imports like `import "@/lib/fetch-bind"`) prints the unreferenced set and exits 1. It exits 1 today naming 4 modules / 672 lines; it exits 0 after. Web source drops 8,838 → ~8,120 lines.


**`TRIAL` · MCP Apps — the third band, and the middleware is already sitting in node_modules**
<br>MCP Apps lets an MCP server ship its own interactive UI: the agent calls a tool that has an associated UI resource, the runtime fetches the resource and renders it in a sandboxed iframe with zero frontend code, and it persists in thread history across reconnects. Aleph does not use it — `grep -rn 'mcpApps\|mcpServers' apps/copilot-runtime/src` → 0. It is nearly free here.
<br>*Why here:* Backlog A3 wants "every plugin can come with its own catalog for A2UI and publish it", and A4 wants per-plugin settings cards — MCP Apps is the industry's answer to the same question, and it is the one band where the Node runtime earns its keep (the other three are client-side facts, per `docs/research/generative-ui-spectrum.md` §6).
<br>*Win:* `grep -c 'mcpApps' apps/copilot-runtime/src/server.ts` goes 0 → 1. A Playwright spec pointing at a local reference MCP App server asserting a sandboxed iframe appears with content from that server: 0 → 1 passing.


**`TRIAL` · Aleph uses 2 of 19 CopilotKit v2 hooks — the Inspector, the Controlled band and capability-driven UI are all already ins…**
<br>Counted against the installed `@copilotkit/react-core@1.58.0` v2 surface: 19 hooks are exported, and `apps/web/src` references three — `useAgentContext` (3 files), `useFrontendTool` (2 files), and `useAgent` (1 file, the dead `ActivityCard`). Two in live code.
<br>*Why here:* `useCapabilities` is the plugin manifest the product thesis needs, in a shape that already exists — when a plugin is activated the capability document changes, and the UI adapts instead of being recompiled. `custom` is where Aleph's plugin identifiers belong.
<br>*Win:* Live-code hook adoption 2/19 → target ≥6/19, countable with a one-line loop over the exported names. `grep -c 'showDevConsole' apps/web/src/lib/copilot.tsx` 0 → 1 (the Inspector, same day).


**`ASSESS` · Close the CopilotKit version skew before building on any of it**
<br>Three versions in play and no two agree. `apps/web/package.json` asks for `@copilotkit/react-core: ^1.58` and has 1.58.0 installed; `apps/copilot-runtime/package.json` pins `@copilotkit/runtime: 1.63.2`; npm currently publishes 1.69.0 for both. `@ag-ui/client` is pinned at 0.0.57 against a published 0.0.58.
<br>*Why here:* Items 3, 5 and 9 above all land on this stack. Building HITL or MCP Apps on a skewed pair means every failure has two candidate causes. Doing the upgrade first is what makes the next four pieces of work debuggable — and the Inspector (item 9) is the instrument that makes the upgrade itself verifiable.
<br>*Win:* Three counts to zero: (1) packages where the installed version differs from another workspace's pin for the same scope — 2 today (`react-core` 1.58.0 vs `runtime` 1.63.2); (2) `^`/`~` ranges on any `@copilotkit/*` or `@ag-ui/*` dependency — 1 today (`"@copilotkit/react-core": "^1.58"`);


**`HOLD` · Undo over agent actions — the substrate exists, the inverses do not**
<br>Every state mutation writes a hash-chained `ActionLedgerEvent` in the same transaction, and `packages/aleph-kernel/src/aleph_kernel/effects.py` opens with "every context mutation flows through one primitive, so every mutation is tracked and every tracked mutation can be undone". It is tempting to read that as an undo stack for agent actions.
<br>*Why here:* I am recommending against building this now, and the reason is structural rather than about effort. A general undo requires a registered inverse per action kind, and the inverse of "retract a source" is not "un-retract" — it is a re-derivation of every claim whose confidence moved, which is the reconciler's job and the reconciler is still being built (`packages/aleph-belief`).
<br>*Win:* The honest measurable here is a negative one, and it is worth writing down so the decision can be revisited on evidence rather than mood: count the ledger action kinds that would need a registered inverse (`grep -rho 'kind="[a-z_]*\.[a-z_]*"' packages apps | sort -u | wc -l`) and the subset for which an inverse is curr…


### Retrieval and knowledge


**`ADOPT` · Make the production retrieval index non-empty, and give it a boot probe that fails when it is**
<br>The corpus index is the substrate every SoTA retrieval technique sits on. On the live stack it has never been populated. `docker exec aleph-postgres-1 psql -U aleph -d aleph -c 'select count(*) from document_chunks'` returns **0** against 75 sources and 45 normalized_documents; `retrieval_index_records` is also 0; `agent_runs` shows **45 chunk_embed runs, all status='running', 0 succeeded**.
<br>*Why here:* Every other item on this list is a percentage improvement on a number that is currently zero in production. It is also the exact defect class CLAUDE.md names as dominant — a write path with no working consumer — but one level up: a whole subsystem that is correct in unit tests, measured in a bespoke eval harness that seeds its own rows, and inert in the only deployment.
<br>*Win:* Three counters, all currently at their failing value. (1) `select count(*) from document_chunks` goes 0 → ≥2,500 (45 docs averaging 140,185 chars ≈ 35k tokens ≈ ~70 chunks each at target_tokens=512).


**`ADOPT` · Make the research composer read the passages it cites, then measure sentence-level attribution (citation precision / rec…**
<br>Two coupled gaps. (a) `research_workflow.py:_node_compose` builds the model's entire evidence context as `listing = '\n'.join(f'c{i}: {s.title}' + (f' — {s.url}' if s.url else ''))` (line 859-862). Titles and URLs. No chunk text, no snippet — `IngestedSource.snippet` is only used in `_node_reflect` (line 823) and is `None` for every OpenAlex candidate (`_candidate_from_work` sets `snippet=None`, l…
<br>*Why here:* CLAUDE.md's product thesis is a web of belief where 'claims are first-class, evidence-anchored' and 'prose is rendered from that layer'. Today the flagship prose path is the inverse: prose generated from titles, with citation markers attached afterwards, and 786 stored citations of which none has been verified against its source.
<br>*Win:* (1) Passage bytes reaching the composer per source: 0 → ≥1,500 chars of retrieved chunk text per cited source (assert in a test on the composer's user message). (2) A new `attribution` scorer: for each `[cN]`-bearing sentence in a produced report, ask a judge-tier model whether the cited chunk entails it;


**`ADOPT` · Add a rerank stage — and because this gateway serves no cross-encoder, make it a listwise LLM reranker over a model it d…**
<br>`search_corpus` returns the RRF-fused list directly (`retrieval.py:118-142`); there is no second-stage scoring. `Capability.RERANK` exists (`aleph_core/schemas/model_profile.py:21`), `discovery.py:392` has a `CapabilityPolicy(mode='rerank', tier='light')` for it, and `apps/web/src/components/Drawers.tsx:189,201` shows it in Settings as 'Reorders retrieved chunks' — but `LiteLLMClient` has only `ch…
<br>*Why here:* Aleph's first stage retrieves top_k=8 with `fetch = max(top_k*4, 40)` candidates already in hand (`retrieval.py:83`) — the over-fetch that a reranker needs is already being paid for and then thrown away. It is also the one lever that helps most where Aleph is weakest: 140k-char un-sectioned PDF chunks, where lexical overlap and a single dense vector both under-discriminate.
<br>*Win:* nDCG@10 on the rebuilt eval (opportunity 5) is the headline; on the current 45-pair set the honest number is recall@1, which reranking should move because @1 is where ranking is actually tested (0.91 today; @1 paraphrase is 0.87).


**`ADOPT` · Contextual retrieval — stop embedding bare chunk text**
<br>`chunk_embed.py:186` embeds `texts=[c.text for c in chunks]`. Nothing else: no document title, no `section_path`, no document-level summary. The chunk goes into the dense index and the `text_tsv` GIN index as an isolated fragment. Note the eval does not do this: `retrieval_eval.py:200` builds `bodies = [f"{doc['title']}.
<br>*Why here:* Aleph's corpus is scientific PDFs averaging 140k chars, and — see opportunity 8 — only 1 of 45 normalized documents has any markdown heading, so `section_path` is NULL for essentially every chunk. A mid-paper fragment therefore carries no signal about which paper it is from or which section it is in. 'The effect was significant (p<0.01)' is unretrievable and, once retrieved, uncitable.
<br>*Win:* Run the rebuilt eval (opportunity 5) three ways over the same corpus — bare chunk, deterministic context prefix, LLM-generated context prefix — and report recall@1 / nDCG@10 for each. The deterministic arm is free;


**`ADOPT` · Replace the 45-pair toy eval with one that can actually fail**
<br>The current set is 12 documents of ~350 characters each and 45 questions, every one with exactly one gold document (`wc -l` on `datasets/retrieval/*.jsonl`; verified 0 multi-label questions). `retrieval_eval.py:207-230` seeds one chunk per document, so the chunker is not under test.
<br>*Why here:* acceptance.md says '0.91 recall@1 is the bar the belief engine has to beat' and gates the entire wiki deletion (Part E) on beating it. That is a load-bearing number resting on 45 questions over 12 short paragraphs, measured through a seeder that does not match production indexing.
<br>*Win:* Concrete targets: corpus ≥2,500 chunks (the project's own 45 ingested documents, indexed by the production job); ≥300 query/passage pairs with ≥3 graded per query; report nDCG@10, recall@50 and MRR@10, with a per-phrasing breakdown as today.


**`ADOPT` · Rebind the embedder to cohere-embed-v4 at 1024 Matryoshka dimensions**
<br>The gateway serves `cohere-embed-v4`. I probed it: default output is 1536-dim, but `{"dimensions": 1024}` returns exactly 1024 — a drop-in fit for `DocumentChunk.embedding = Vector(EMBEDDING_DIM=1024)` (`aleph_rks/models.py:34,155`) with no migration and no column re-dimensioning. The current default binding is a model the gateway does not serve at all;
<br>*Why here:* Amazon Titan Text Embeddings V2 is a 2024-era general-purpose embedder; Cohere Embed v4 is a current-generation retrieval embedder with a 128k context window and native Matryoshka truncation. Aleph's own eval already shows the dense leg is worth +0.31 recall@1 and that almost all of it comes from paraphrase questions — the leg most sensitive to embedder quality is the one carrying the most weight.…
<br>*Win:* A/B on the rebuilt eval, same corpus, same queries, only the `embedding` binding changed: report recall@1 and nDCG@10 for titan-embed-text-v2@1024 vs cohere-embed-v4@1024. `reembed_for_project` already exists and is idempotent (`retrieval.py:201-296`), so the switch is a worker run and the delta is directly attributabl…


**`ADOPT` · Turn on pgvector iterative index scans for the project-filtered dense leg**
<br>The dense query is `select(...).where(DocumentChunk.project_id == project_id).order_by(DocumentChunk.embedding.cosine_distance(q)).limit(fetch)` with `fetch = max(top_k*4, 40)` (`retrieval.py:79-96`). With an HNSW index, the filter is applied *after* the index scan, so a query in a small project can come back with far fewer than `fetch` rows — or none — while the index reports success.
<br>*Why here:* Aleph is multi-tenant by construction — 'every row carries project_id' is a stated design commitment — so *every* dense query is a filtered query. This is the failure mode where the system reports no error and simply returns a worse list, which is the shape of defect this codebase has shipped repeatedly.
<br>*Win:* A test that seeds N=20 projects with 500 chunks each, queries one project, and asserts the dense leg returns `fetch` rows (40) rather than a truncated set. It fails today and passes with `SET LOCAL hnsw.iterative_scan = 'relaxed_order'` in the retrieval transaction.


**`ADOPT` · Replace pypdf with a layout-aware PDF parser — section_path is NULL for the entire corpus**
<br>`PyPDFNormalizer.normalize` (`normalization.py:64-108`) calls `page.extract_text()` per page and joins with blank lines. No headings, no reading order, no tables, no equations; `structure` is hardcoded `{'heading_count': 0, 'table_count': 0, 'figure_count': 0}` (line 98-103).
<br>*Why here:* For a scientific corpus, the section a passage came from is most of its meaning: a number in Methods, in Results, and in Related Work are three different claims. Aleph stores `section_path` on every chunk, threads it through `ChunkHit` and `DescentChunk`, and renders it — and it is NULL for 44 of 45 documents.
<br>*Win:* Two counts, both currently at their worst value: `select count(*) from normalized_documents where (structure_jsonb->>'heading_count')::int > 0` goes 1 → ≥35 of 45, and `select count(*) from document_chunks where section_path is not null` goes (once chunks exist) 0 → >80%.


**`ADOPT` · Make claims retrievable — the Claim Spine has two indexes, no writer, and an O(n²) reconciler**
<br>`WikiClaim` declares `embedding: Vector(1024)` with an HNSW index `ix_claims_embedding_hnsw` and a GIN expression index `ix_claims_text_fts` over `to_tsvector('english', text)` (`aleph_wiki/models.py:203-216,243`). `grep -rn '\.embedding' packages/aleph-wiki packages/aleph-belief apps/api/src` returns **nothing** — no writer. Live DB: 786 claims, `count(*) where embedding is not null` = **0**.
<br>*Why here:* acceptance.md gates the wiki deletion on the belief path beating 0.91 recall@1 — but there is no belief retrieval path at all to measure. The Claim Spine is currently write-only with respect to retrieval, which means Part E can never unblock on its own terms.
<br>*Win:* (1) `select count(*) from wiki_claims where embedding is not null` goes 0 → 786, written on the same transaction as the claim. (2) A `search_claims` entry point with the same hybrid+RRF shape as `search_corpus`, benchmarked on the rebuilt eval as a third arm alongside chunk retrieval — this produces the number acceptan…


**`TRIAL` · Multi-query expansion fused by RRF, targeted at the paraphrase gap**
<br>`search_corpus` issues exactly one query with the user's raw text (`retrieval.py:103,113`). No expansion, no decomposition, no synonym generation. The assistant router (`router.py:184`) passes the question through verbatim.
<br>*Why here:* Aleph's own eval already isolates the residual error: at recall@1, verbatim questions score 1.00 and paraphrase questions score 0.87 (acceptance.md §B). Vocabulary mismatch is the remaining failure mode by the project's own measurement, and generating 3-4 alternate phrasings and RRF-fusing their result lists is the cheapest direct attack on it.
<br>*Win:* recall@1 and nDCG@10 on the *paraphrase* slice of the rebuilt eval specifically, since that is where the mechanism should act — the eval already breaks down by phrasing (`retrieval_eval.py:250-253,306-307`), so the slice exists.


**`TRIAL` · BM25 lexical scoring instead of Postgres ts_rank**
<br>The lexical leg ranks with `func.ts_rank(DocumentChunk.text_tsv, tsquery)` (`retrieval.py:113`). `ts_rank` is a weighted term-density score with no inverse document frequency and no document-length normalisation. Combined with `or_tsquery` (`tsquery.py:51-59`), which rewrites the parsed conjunction to a disjunction, a natural-language question matches any chunk containing any one stem — so on a re…
<br>*Why here:* The whole architecture rests on the lexical leg being an *independent, competent* ranker — RRF's value comes from agreement between two rankers that disagree in useful ways, and a leg that cannot tell a rare domain term from a common one contributes noise rather than signal.
<br>*Win:* On the rebuilt eval, run the lexical leg alone three ways — `ts_rank`, `ts_rank_cd`, BM25 — and report recall@50 and nDCG@10 for each, plus p95 query latency at corpus scale. The decision rule should be stated in advance: adopt BM25 only if it beats `ts_rank` by more than the noise band on lexical-only recall@50, becau…


**`ASSESS` · GraphRAG-style community summarisation over the claim graph**
<br>The pieces are notionally present — `claim_edges` with `kind ∈ supports|contradicts|derived_from|specializes|supersedes` (`aleph_wiki/models.py:408-430`) is exactly a typed knowledge graph — but the table has 0 rows on the live stack and the only writer emits `supersedes`.
<br>*Why here:* Global sensemaking is a genuinely different query class from the one hybrid chunk retrieval serves, and it is the class a research workbench gets asked most often ('what does this literature disagree about', 'what is the consensus').
<br>*Win:* Before building anything: instrument the assistant to classify each turn as local (fact-seeking) or global (sensemaking), and count. If under ~15% of real turns are global, the honest answer is that this is not where the value is.


**`HOLD` · ColBERT-style late interaction, and ColPali-style vision late interaction over PDF pages**
<br>Late interaction stores a vector per token (or per patch) and scores by MaxSim, keeping the term-level detail a single pooled vector discards. Aleph stores exactly one 1024-dim vector per chunk (`DocumentChunk.embedding`, `models.py:155`) and the installed pgvector 0.8.6 has no multi-vector primitive — a real implementation means a second index type or a second store, which is a deployment change…
<br>*Why here:* The vision variant is the interesting one, because it is the alternative answer to opportunity 8: instead of parsing a PDF into text and losing the layout, embed the rendered page image with a late-interaction vision model and retrieve pages directly. For a corpus that is 40 scientific PDFs averaging 140k chars with one usable heading between them, that is a materially different bet.
<br>*Win:* None obtainable today, which is the reason for the verdict. The trigger to revisit is concrete and checkable: when the configured gateway advertises a multi-vector or vision-embedding model (`aleph_models.discovery` already reads the catalogue and already leaves capabilities unbound rather than guessing), re-open this…


**`HOLD` · HyDE (hypothetical document embeddings) as a query transform**
<br>Generate a fake answer to the query with an LLM, embed *that*, and search with it — on the theory that a hypothetical answer sits closer in embedding space to the real passage than the question does. Aleph does none of this; the raw question is embedded directly (`router.py:335-345`).
<br>*Why here:* It is the obvious candidate for the paraphrase gap, which is why it needs an explicit no rather than silence. It would add an LLM generation to the critical path of every retrieval turn — and Aleph's agent runs in-process inside FastAPI, so latency on the request path is a real constraint, not a preference.
<br>*Win:* The honest measurable is a negative one: after opportunities 3, 4 and 6 land, check whether the paraphrase slice of the rebuilt eval still shows a gap. If recall@1 on paraphrase reaches parity with verbatim, HyDE has nothing left to buy and this stays closed.


### Agent harness


**`ADOPT` · Feed the gateway's real context window into compaction — today the agent compacts at a hardcoded 170k regardless of the…**
<br>`create_deep_agent` installs `create_summarization_middleware(model, backend)` in the default stack (`.venv/.../deepagents/graph.py:740`). Its thresholds come from `compute_summarization_defaults` (`.venv/.../deepagents/middleware/summarization.py:172-209`), which reads `model.profile['max_input_tokens']`.
<br>*Why here:* `aleph_models.discovery` already reads `max_input_tokens` from the gateway's `/model/info` (packages/aleph-models/src/aleph_models/discovery.py:87, 177) and stores it on every binding (`binding_for`, line 449). Nothing on the agent path reads it — grep shows the only consumer is the Settings API surfacing it to the UI (apps/api/src/aleph_api/routes/model_profile.py:77).
<br>*Win:* Provider `context_length_exceeded` / `ContextOverflowError` responses per 100 agent turns against a sub-170k binding: currently unbounded, target 0. Testable without a live model: bind a profile whose `max_input_tokens` is 32000, drive a thread past 32k approximate tokens, assert `_summarization_event` fired at least o…


**`ADOPT` · Prompt caching is silently a no-op on Aleph's agent path — ~10k tokens of identical prefix are re-billed at full rate ev…**
<br>deepagents appends `AnthropicPromptCachingMiddleware(unsupported_model_behavior='ignore')` unconditionally (`.venv/.../deepagents/graph.py:609`, and the equivalent for the top-level agent). That middleware returns immediately unless the model is a `ChatAnthropic` instance — `.venv/.../langchain_anthropic/middleware/prompt_caching.py:104: if not isinstance(request.model, ChatAnthropic)`.
<br>*Why here:* I measured the static prefix: Aleph's own SYSTEM_PROMPT is 1,644 approximate tokens, deepagents' BASE is 569, the filesystem middleware prompt 2,347, the subagent middleware prompt 2,279, skills 469, and Aleph's four bundled SKILL.md metadata blocks 297 — 7,605 tokens before any tool schema, plus 1,483 tokens of docstring across the 11 `@tool` functions in copilot_agent.py and deepagents' own eigh…
<br>*Win:* `SELECT sum(cached_tokens), sum(cache_savings_usd) FROM model_calls WHERE purpose LIKE 'assistant.%'` is exactly 0 today. Target: cached_tokens / input_tokens > 0.6 measured over a session of 5+ turns. Secondary: median time-to-first-token on turn N>1.


**`ADOPT` · Resolve the model per request, not once at boot — and stop `set_model_profile` telling the user something untrue**
<br>`build_assistant_deep_agent` (copilot_agent.py:1529) constructs one orchestrator `ChatOpenAI` and six subagent models at startup. `_resolve_agent_model` (line 1439) reads `_runtime['agent_bindings']`, which lifespan populates once from the **default named template**, not from any project (apps/api/src/aleph_api/lifespan.py:88-106).
<br>*Why here:* The `set_model_profile` tool writes the project row and then tells the analyst 'New LLM/agent calls use that profile's models' (copilot_agent.py:1287-1291). For the agent's own turns and every subagent turn, that sentence is false — those models were frozen at process start.
<br>*Win:* A test that (a) switches project A's profile to `aleph-production`, (b) runs one turn, (c) asserts the resulting `ModelCall.model` differs from the boot default. It cannot pass today.


**`ADOPT` · Systemic tool-output spill: cap what a tool result may put in context, store the rest, hand back a preview plus a locato…**
<br>A post-execute policy that measures the model-facing tool result, and when it exceeds a byte cap writes the full text to the agent backend and replaces the inline result with a head/tail preview plus a retrieval path the agent can `read_file`.
<br>*Why here:* Aleph's tools are retrieval tools — their whole job is to return document text. `deep_read` returns a composed answer over expanded wikilinks; `search_corpus` returns chunks. That is precisely the shape whose size is unpredictable and dominated by content the model reads once.
<br>*Win:* p99 `len(ToolMessage.content)` over a session, asserted ≤ the configured cap in a test that calls `deep_read` against a large corpus. Today that number is unbounded and unmeasured. Secondary: tokens per turn attributable to tool results, from the `ModelCall.input_tokens` delta across a turn.


**`ADOPT` · Build the Inspector on the trajectory Aleph already persists and currently reads with nothing**
<br>An agent Inspector pane (backlog C3) fed from two sources that both exist: the durable `AsyncPostgresSaver` checkpoint, and Deep Agents' `stream.subagents` handles for the live view (backlog H7). Add a read route over `graph.aget_state` / `checkpointer.alist` plus per-turn `AgentRun` / `AgentEvent` rows for the conversational agent.
<br>*Why here:* Two verified gaps meet here. First, the trajectory is already written and read by nothing: `build_agent_checkpointer` mounts `AsyncPostgresSaver` (copilot_agent.py:1410-1426) and grep finds no call to `aget_state`, `get_state` or `alist` anywhere in `apps/api` or `apps/workers` — the checkpoint is used only by the graph to resume itself.
<br>*Win:* `GET /v1/projects/{id}/agent-runs/{thread_id}/trajectory` returns ≥1 tool-call record for a turn that made a tool call — a check that returns 0 today and can fail. Downstream: median minutes to localise an agent failure, currently 'read container logs'.


**`ADOPT` · The eval harness discovers zero datasets and exits 0 — and there is no agent-behaviour eval of any kind**
<br>`packages/aleph-evals` has a dataset-discovering runner (`_discover_specs`, runner.py:62) and six scorers — citation, coverage, cost, permission, retrieval, synthesis. It scans `datasets/<inc>_<area>/manifest.yaml`. I ran `uv run python -m aleph_evals --datasets all --gate soft`: the output is `{"selected_datasets": [], "any_failures": false}`.
<br>*Why here:* CLAUDE.md's own standard is 'prefer criteria that can FAIL' and it already calls out `audit/run.sh` for this exact sin. The eval runner is currently a greener version of the same problem: six scorers with no case can never report a regression, while the CLI reports success.
<br>*Win:* Three numbers that do not exist today: (1) `selected_datasets` length > 0 from the runner; (2) tool-choice accuracy over a labelled set of N prompts with an expected first tool, reported as a percentage; (3) end-to-end task success rate over a small suite of research tasks. Each can regress, which is the point.


**`ADOPT` · Wire the SpawnLedger — Aleph has written depth/fan-out/budget brakes and enforces none of them**
<br>`packages/aleph-kernel/src/aleph_kernel/spawn_ledger.py` (198 lines) implements exactly the right thing: `max_depth`, `max_children`, and a budget that is *deducted from the parent's remaining* so 'a subtree cannot outspend its root', with reservation at spawn time so a parent cannot promise the same budget twice. It has zero callers outside its own tests — verified by grep across the whole tree.
<br>*Why here:* Backlog E5 reports the gateway 'weirdly rate limited' and names subagent fan-out as the likely cause — and the brake for that is sitting in the tree, finished, unwired. This is also the strongest version of the 'ship a consumer with every producer' rule: cost is recorded to six decimal places with pricing provenance, and no code path anywhere can act on it.
<br>*Win:* A turn given a $0.05 budget refuses its 4th subagent spawn with a stated reason — a test that cannot pass today. Operationally: max `ModelCall` count attributable to a single thread over 24h, and count of turns exceeding a configured spawn budget (target 0, currently unmeasurable).


**`ADOPT` · Make the agent able to author a skill — using `aleph_kernel.skills` and its AST gate as admission control instead of lea…**
<br>Two skills implementations exist. The live one is deepagents' `SkillsMiddleware` over a read-only `FilesystemBackend` (copilot_agent.py:1596, `_SKILLS_DIR` at 1463) serving four bundled SKILL.md files. The other, `packages/aleph-kernel/src/aleph_kernel/skills.py` (197 lines) plus `ast_gate.py` (203 lines), gates source before executing it, execs into a fresh namespace so a skill cannot shadow a mo…
<br>*Why here:* This is the product thesis (CLAUDE.md: 'an agent that authors plugins for itself... with guardrails preventing it from removing load-bearing capability') and it is currently unexpressible — the skills backend is a read-only host filesystem, so nothing the agent learns survives the session.
<br>*Win:* Three checks that all fail today: (1) a skill the agent authored in thread A is listed by `SkillsMiddleware` in thread B after an API restart; (2) a skill whose `kernel.py` performs work at module top level is rejected with a line number rather than admitted;


**`ADOPT` · Loop and repeat guards on the tool-call stream — the cheapest item on this list**
<br>Two small policies with no model-facing surface. (a) A repeat detector: count consecutive tool calls with identical canonicalized arguments and, at thresholds, inject an advisory nudge telling the model to re-read the last result and change approach. (b) A per-tool declared timeout, enforced as a structured `TOOL_TIMEOUT` result rather than a hung turn.
<br>*Why here:* Aleph's expensive tools are the ones most likely to be repeated — `deep_read` re-run with a near-identical query, `search_wiki` re-run after an empty result. The empty-search confabulation bug already fixed in `retrieval/router.py` was exactly the shape that invites a retry loop, and a loop against a metered path (`search_consensus` is quota-metered per month, subagents/researcher.py:184) burns a…
<br>*Win:* Count of agent runs containing ≥3 consecutive identical tool calls, computed from the trajectory (item 5 makes this queryable): target 0 after the nudge lands. Count of turns exceeding a per-tool deadline, currently unmeasurable because no deadline exists.


**`ADOPT` · Instrument the agent graph for OTEL — `diagnose_platform` reads traces the agent never emits**
<br>`aleph-observability` instruments FastAPI, httpx, SQLAlchemy and Redis (packages/aleph-observability/pyproject.toml:10-13) and nothing else. There is no LangChain/LangGraph instrumentation and no Langfuse callback handler on the agent path — `_gateway_chat_model` attaches exactly one callback, the cost handler (copilot_agent.py:1506).
<br>*Why here:* Aleph ships a `diagnose_platform` tool (copilot_agent.py:1318-1364) whose whole purpose is to read the platform's own Langfuse traces and report what is broken — while the conversational agent contributes none of its own spans to that store. So the agent can diagnose the workers and cannot diagnose itself.
<br>*Win:* Spans emitted per agent turn: 0 today, target ≥1 per tool call and ≥1 per subagent delegation, asserted by an in-memory span exporter in a test. Secondary: whether `diagnose_platform` can name the failing step of an errored chat run — currently it cannot.


**`TRIAL` · Deterministic record/replay of gateway traffic, so agent behaviour is testable without a live model**
<br>Record real gateway request/response pairs once into JSON cassettes, replay them in tests. `grep -rn 'vcr|cassette|respx|record_mode'` over the tree returns nothing outside `.venv` — Aleph has no replay layer of any kind. Every agent test today either mocks at the Python object level (apps/api/tests/unit/test_subagents.py asserts a constructed model's `base_url`, not behaviour) or needs `LITELLM_B…
<br>*Why here:* The 18 agent-adjacent unit tests in `apps/api/tests/unit/` pin construction, not conduct: `test_agent_gateway_base_url.py` pins URL shaping, `test_agent_checkpointer.py` pins that a saver is passed. Not one exercises a turn. That is why backlog E1's `RUN_ERROR` has to be reproduced by hand in a browser.
<br>*Win:* Number of tests exercising a complete agent turn that pass with no gateway configured: 0 today, target ≥5 covering tool call, subagent delegation, approval gate, and a compaction trigger. Secondary: CI wall-clock stays flat because replayed turns cost no tokens.


**`TRIAL` · A general MCP client as the tool-source registry — a cheaper plugin system than the bespoke one A1 describes**
<br>Aleph is an MCP *server* (`packages/aleph-a2ui/src/aleph_a2ui/mcp_server.py` publishes the A2UI catalog) and an MCP *client* for exactly one hardcoded connector (`packages/aleph-scholar/src/aleph_scholar/consensus.py:39, 204`). There is no way to register an MCP server as a tool source for the agent. The `mcp` SDK is already a workspace dependency;
<br>*Why here:* Backlog A1 wants runtime activate/deactivate of plugins and orders it last on purpose, because 'a plugin system designed before [items 2, 5 and 6] would be designed against guesses'. An MCP tool-source registry is the version of A1 that needs no invented protocol: a plugin becomes a server you register, per-project enable/disable is a row, and the trust tiering A4 wants maps onto per-server tool a…
<br>*Win:* Number of tool sources addable without a code change and a redeploy: 0 → N. A check that can fail: register a stdio MCP server in config, assert its tools appear in the compiled agent's tool list, disable it for one project and assert they disappear for that project only.


**`HOLD` · Async subagents (backlog H6) for the research loop**
<br>`deepagents.middleware.async_subagents.AsyncSubAgentMiddleware` is present in the installed 0.6.6 (verified importable) and returns a job id immediately so the supervisor keeps talking while work proceeds, with check / mid-flight update / cancel.
<br>*Why here:* The backlog places this at item 10 on the grounds that 'Aleph's research loop is exactly this shape and currently blocks'. Reading the code, it does not. `_start_research_impl` (copilot_agent.py:825-868) self-calls `POST /v1/projects/{id}/synthesize`, which dispatches an arq job, and returns immediately with 'It runs in the background (~1 minute)...
<br>*Win:* None that I can state honestly. The candidate metric — supervisor wall-clock blocked per research dispatch — is already near zero, and I could not find a second in-agent call path that blocks on long work.
---

## Part 6 — How this runs

### The eleven contested files

Six clusters, ~57 workstreams, and the bulk of the work lands in eleven files.
Every one is edited by more than one cluster, and no cluster's `depends_on`
crosses a cluster boundary — so all of these are invisible to both sides.

| File | Lines | Clusters editing it |
|---|--:|---|
| `apps/api/src/aleph_api/copilot_agent.py` | 1,662 | **4** |
| `apps/api/src/aleph_api/routes/surfaces.py` | 1,028 | 3 |
| `packages/aleph-runtime/.../capabilities.py` | 729 | 3 |
| `packages/aleph-a2ui/.../catalog.json` | 2,093 | 2 (+2 generated copies, +a sweep that fails on drift) |
| `apps/web/src/components/Drawers.tsx` | 742 | 2 — one **deletes** it while the other's criteria still reference it |
| `scripts/acceptance.sh` | 365 | 2, both claiming ownership, neither owning its twenty dangling references |
| `CLAUDE.md`, `docs/acceptance.md` | — | 3 each |

**Rule:** shared trunk, weekly merge, and no cluster carries more than one week
of divergence on any of these. Without it, the plan's real output is six branches
that individually pass and collectively do not merge.

### Cross-cluster dependencies the clusters did not declare

- **The JS test harness gates three clusters.** It is built in one workstream and
  depended on by none.
- **`tests/e2e/` does not exist**; four clusters write criteria into it, and the
  workstream that restores it is nobody's dependency.
- **Retrieval index population gates four clusters' measurements.** With
  `document_chunks = 0`, the agent's search fix, cost attribution, four research
  workstreams and `/readyz` semantics all measure against an empty index.
- **Per-plugin settings cards need four workstreams across two clusters** — a
  plugins table with `config_schema`, catalog composition, a settings pane on the
  SSE transport, and the renderer. As scheduled, the renderer ships for schemas
  no table stores.
- **Two clusters register a `plugins` pane into the same 165-line if-chain** in
  `routes/surfaces.py`, in the same window.
- **Two clusters need different chat-model fakes** and neither budgets the
  other's.

### Sequence

**First** — Part 0. Make the gate honest and make it run.

**Then, in dependency order:**

1. **Unbreak retrieval** (WS-RS1) — small, and it unblocks four clusters' ability
   to measure anything. Rebind the embedder, add a boot probe that fails loudly,
   sweep the 45 stuck runs, and decide in writing what happens to 786 ungrounded
   claims: re-extract or delete.
2. **Close the unsupervised skill-write path** (WS-K1) and **fix the two kernel
   bugs** (WS-A1a) — small, and they gate the self-improvement work.
3. **Prompt caching, context window, spill and loop guards** (harness horizon,
   `adopt`) — the cheapest items on the list and the likely cause of the gateway
   rate limiting.
4. **The endpoint and per-model profiles** (WS-MEP-*) — the stated hard
   requirement.
5. **The trajectory record, then the Inspector** (WS-C3a → WS-C3b) — built on
   what Aleph already persists and currently discards.
6. **The UI harness, dead-code deletion, then settings as panes** (WS-UI-1 →
   WS-UI-2 → WS-B1a → WS-A4 → WS-B1).
7. **Retrieval quality and the real eval** (WS-RS4 → WS-RS5 → WS-RS6).
8. **Everything else**, gated on the numbers in Part 1 moving.

### Review and iteration are steps, not attitudes

Every workstream above carries both, because the owner asked for them explicitly
and because a plan without them silently assumes first drafts are correct.

- **Review** prefers *mutation testing*: break the thing, confirm the check goes
  red, restore. A check never observed failing is a check that proves nothing —
  which is exactly how acceptance Part D came to certify a module the product
  does not load.
- **Iterate** names what a second pass improves once v1 works, so the difference
  between "shipped" and "good" is scheduled rather than hoped for.
- **Brainstorm** is Part 5, and it is not a one-off. Re-run the horizon scan at
  each of the eight numbers, because what is state of the art in retrieval and
  agent harnesses moves faster than this plan will be executed.

---

## Part 7 — Sources to review before executing

A new session picking this up should read these first. Each is mapped to the
workstreams that depend on it, so the reading is scoped rather than general.

### LangChain / Deep Agents

| Doc | Workstreams |
|---|---|
| [Skills](https://docs.langchain.com/oss/python/deepagents/skills) — `SKILL.md` frontmatter, three-level progressive disclosure, `SkillsMiddleware`, and the backends (`State` / `Store` / `Filesystem`) | `WS-H1`, `WS-K1`, `WS-A1b`, `WS-A2` |
| [Profiles](https://docs.langchain.com/oss/python/deepagents/profiles) — `HarnessProfile`, `register_harness_profile`, YAML config, per-provider defaults | `WS-MEP-7`, `WS-MEP-6` |
| [Rubric](https://docs.langchain.com/oss/python/deepagents/rubric) — `RubricMiddleware`, LLM-as-judge, iterate-until-satisfied. **Beta**, needs ≥0.6.5 | `WS-H3` |
| [Interpreters](https://docs.langchain.com/oss/python/deepagents/interpreters) — in-memory workspace, programmatic tool calling. **Beta**, needs `deepagents[quickjs]` | `WS-H5` |
| [Dynamic subagents](https://docs.langchain.com/oss/python/deepagents/dynamic-subagents) — dispatching subagents from interpreter code | `WS-H5` |
| [Async subagents](https://docs.langchain.com/oss/python/deepagents/async-subagents) — job ids, check/update/cancel, **ASGI transport when `url` is omitted**. Preview | `WS-H6` |
| [Event streaming](https://docs.langchain.com/oss/python/deepagents/event-streaming) — `stream.subagents`, `astream_events(version="v3")`, `asyncio.gather` | `WS-C3a`, `WS-C3b` |
| [RAG](https://docs.langchain.com/oss/python/deepagents/rag) — four patterns; the one that fits is **retrieve-offload-delegate**: fetch chunks, write them to the filesystem backend instead of the orchestrator's context, let subagents read them in parallel | `WS-RS1`, `WS-RS6`, `WS-RS7`, `WS-H5` |
| [OpenWiki](https://docs.langchain.com/oss/openwiki/overview) — agent-facing wiki as durable context, and **OKF v0.1** (front matter, indexes, linked concepts) | `WS-H8` |

### CopilotKit

| Doc | Workstreams |
|---|---|
| [Intelligence — self-improvement](https://www.copilotkit.ai/copilotkit-intelligence#self-improvement) — the pillar the first assessment missed: in-context learning from feedback, captured interactions, automatic skill development | Context for `WS-H1`/`WS-A2`; the decline is recorded in Part 7 |
| [OSS vs Enterprise](https://docs.copilotkit.ai/concepts/oss-vs-enterprise) — what the licensed backend actually provides, and the Helm/Kubernetes self-hosting path | The evidence behind declining C1 |
| [Generative UI spectrum](https://www.copilotkit.ai/generative-ui-spectrum) — the four bands; Aleph uses three | `WS-C2`, `WS-A4`, `WS-UI-3` |

### Specifications

- [Agent Skills specification](https://agentskills.io/specification) — the format both
  `aleph_kernel/skills.py` and deepagents' `SkillsMiddleware` implement, and the
  reason they are two implementations of one thing (`WS-A1b`).
- [Agent Protocol](https://github.com/langchain-ai/agent-protocol) — what async
  subagents speak (`WS-H6`).

### MCP servers available in-session

Do not work from memory on any of the above. These were used to build this plan
and are the current source:

- `copilotkit-mcp` — `search-docs`, `explore-docs`, `search-ag-ui-docs`, `search-code`
- `docs-langchain` — `search_docs`, and `query_docs_filesystem` for reading whole
  pages by path (`cat /oss/python/deepagents/skills.mdx`)
- `reference-langchain` — `get_symbol`, `search_api` for API signatures
- `context7` — `resolve-library-id` then `query-docs`, for anything else

**Beta and preview surface.** `WS-H3` and `WS-H5` are beta; `WS-H6` is preview.
The horizon scan flagged that three thesis-critical features currently sit on
unstable upstream APIs, and recommended capping that at one. The decision taken
here is to adopt them anyway — the owner's instruction was *"don't fight the
beta, just use the best stuff"* — but each should carry a contract test
asserting the imported symbol's signature matches what Aleph calls, so a
`uv sync` to a new `deepagents` fails CI rather than production.

---

## Part 8 — Explicitly not doing

Each of these belongs in `docs/decisions.md` as a dated entry, so it stops being
re-litigated every planning round.

- **The kernel stays in Python.** Close D5. Fifty-seven workstreams already
  assume it; leaving it "open" means the assumption is never owned.
- **The wiki is not deleted in this cycle.** Acceptance E1/E2/E3 stay skipped.
  One workstream produces the deciding measurement; nothing schedules the
  deletion. Say the exit condition and stop there.
- **OIDC *deployment* is not a goal this cycle** — but its two known holes are
  closed, and this line used to say the opposite.

  **Correction (21 Aug):** an earlier draft said flatly *"OIDC is not shipped"*
  while `WS-D3` and `WS-D4` are both scheduled OIDC workstreams — the same
  mistake as the async-subagents line above, found by the same check. `WS-D3`'s
  own rationale argues against parking it: *"parking it behind D4's genuine
  transport rewrite makes a cheap fix look expensive and keeps oidc mode
  unusable for the chat path."* It is one prop on one component plus a test, and it also closes an open proxy on port 4000 — which is a
  `local`-mode problem too, not only an OIDC one. `WS-D4` is a genuine transport
  decision and is scheduled as one, because in `oidc` mode every live surface is
  dark, and that is not partial degradation, it is the workspace not working.

  What remains out of scope: standing up an identity provider, and measuring
  anything in `oidc` mode. Measurement stays in `local`.

### Three deletions that were promised and unowned

An earlier draft of this section promised three deletions with **no workstream
behind any of them**, and one of them contradicted scheduled work. They are now
one workstream, because they are the same job — removing a second, unread
concept — and because a promise with no owner is how the last round of
"review-held rules" became this backlog.

#### WS-X1 · Delete the three unread concepts — **DONE 2026-08-21**

All three are removed from the tree and the database. `docs/decisions.md` D7
records why.

| | Was | Now |
|---|---|---|
| `access_scope` | 70 write sites, 0 query filters, on 41 tables | 0 references; column dropped from all 41 |
| `audit/` | a second acceptance gate disagreeing with the first | *pending* — its 7 browser specs are harvested by `WS-P4` first |
| `aleph-datasets` | a package whose only importers were the migration runner and a consistency test | package deleted, 3 empty tables dropped, workspace count 21 → 20 |

**OIDC went with them** (`docs/decisions.md` D6). The half-built code-flow is
gone: `aleph_security/jwt.py`, the JWKS kernel capability, the middleware
branch, four settings, `oidc-client-ts`, and the frontend's issuer/client/audience
configuration. `apps/web/src/lib/auth.ts` is now 55 lines and returns a sentinel.

**Two workstreams changed as a result:**

- `WS-D4` (SSE token transport) is **withdrawn**. It existed because every live
  surface was dark in `oidc` mode. There is no `oidc` mode.
- `WS-D3` **survives and is more important than it looked**. Its OIDC framing
  was never the real problem: `apps/copilot-runtime/src/server.ts` builds
  `new HttpAgent({ url: AGENT_URL })` with no headers on a **published port**,
  so anything that can reach port 4000 can drive the agent. That is a live
  problem in the only mode Aleph runs.

Six tests were re-expressed rather than deleted. They had used `oidc` mode to
get a deterministic 401 without a database; they now assert the invariant that
actually matters — an unauthenticated request must never return 2xx — which is
what pinned the original vulnerability and still does.

**Verification:** `grep -rn 'access_scope' apps packages --include='*.py' | grep -v alembic/versions`
returns 0 (31 unfiltered, every one of them in an immutable migration that must keep naming the
column it dropped, so the unfiltered form is red forever) · `select count(*) from information_schema.columns where
column_name='access_scope'` returns 0 · `alembic check` reports no drift ·
1,776 unit + 362 integration passing · all 24 sweeps green. (779/36/five were the counts when
this was written; the suite has roughly doubled twice since, so a stale absolute number here
reads as a shrinking test base.)

### And one open question worth taking seriously

The `thesis-risk` scan argues **not to build the plugin system at all** — that
the boot manifest already is one, and the honest move is to prove that with a
failing test rather than to write a second abstraction beside it. It also argues
that agent-authored code must run in `code-runner` — cap-dropped, read-only
rootfs, network-partitioned — and **never** through `exec()` in the FastAPI
process, which is what the kernel's current skill loader does.

The second point is not optional; it is a security boundary and the plan adopts
it. The first is a genuine fork in the road, and it should be decided
deliberately in `docs/decisions.md` before WS-A1b starts, not discovered two
weeks in.
