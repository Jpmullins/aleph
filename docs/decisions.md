# Decisions

Dated decisions and the reasoning behind them. Cited from `CLAUDE.md`,
`architecture.md` and `belief-engine.md`.

**This file was missing.** It was cited nine times and did not exist, so the
reasoning behind several standing rules was simply gone — including the one that
said the wiki was being deleted, which stopped a rule from being questioned
because nobody could see why it was made. Rebuilt 2026-08-21.

Decisions are written in plain language. Where a term needs decoding, it is
decoded here rather than assumed.

---

## D1 · The wiki is one of two knowledge plugins — **SUPERSEDES the removal decision**

**Decided 2026-08-21. Supersedes the original D1, which said the wiki was being
removed.**

### What the old decision said

The wiki was going to be deleted. The reasoning: it was an *LLM-maintained
encyclopedia* — the agent read sources and wrote prose pages, and those pages
were what search looked at. That is a lossy middleman. You search a summary of
the evidence rather than the evidence, and if the summary is wrong or stale, so
is every answer. The **Claim Spine** — durable claims with verbatim quotes
anchored to exact character positions in the source — was going to replace it.

### Why that decision is now stale

Two things happened.

**The replacement never arrived.** The Claim Spine has 786 claims, **zero**
edges between them, **zero** verbatim quotes, and no callers — `BeliefService`
is imported by nothing outside its own module. It has never run in production.
A replacement that does not exist cannot replace anything.

**And the wiki became good.** It now has a governance schema, sixteen health
checks, derived category hubs and an index, automatic classification, and link
resolution (`docs/wiki-schema.md`). It is the working knowledge layer.

### What is true instead

**There are two knowledge plugins, and both stay.**

| Plugin | What it holds | What it is for |
|---|---|---|
| **The wiki** | Synthesised pages, claims, citations, category hubs, a governed tag vocabulary | What the project *concluded*. Curated, cross-linked, reviewable — a thing a person reads. |
| **RAG over the raw collection** | Every ingested source, chunked and indexed | What the project *collected*. Searched directly, so an answer can be grounded in the actual passage rather than in somebody's summary of it. |

**RAG** — retrieval-augmented generation — means: before the model answers, find
the passages that actually bear on the question and put them in front of it. The
model answers from the sources rather than from memory.

The old decision framed these as competitors, where one had to win. They are
not. They answer different questions:

- *"What do we think about X, and on what evidence?"* → the wiki.
- *"What did source 47 actually say, in its own words?"* → the RAG.

Both are plugins. Both are fully accessible to the agent and to the analyst.
Neither is the "primary retrieval surface", because that framing is what forced
the false choice.

### What this changes immediately

- The rule *"treat wiki code as legacy under removal; do not extend it, do not
  fix its cosmetics, do not add tests to it"* is **deleted**. It was violated
  comprehensively and correctly.
- Acceptance Part E ("the wiki can be deleted") is **withdrawn**, not blocked.
  There is no deletion to unblock.
- The raw collection needs to become a real RAG. Today it is not one:
  `document_chunks` has **zero rows** against 75 ingested sources, because the
  embedding model name in the profile does not match what the gateway serves.
  Nothing can be retrieved because nothing was ever indexed. See `WS-RS1`.
- The Claim Spine is not cancelled, but it is no longer a *replacement*. It is
  the evidence layer underneath the wiki: what makes a page's assertions
  traceable to an exact sentence in a source. See `WS-RS8`.

### Reference

Deep Agents documents four RAG patterns
(<https://docs.langchain.com/oss/python/deepagents/rag>). The one that fits
Aleph is **retrieve, offload, and delegate**: the agent fetches matching chunks,
writes them to the filesystem backend instead of holding them in its own
context, and subagents read and summarise them in parallel. That keeps the
orchestrator's context clean, which is the same problem the tool-output spill
work addresses.

---

## D5 · The kernel stays in Python — **CLOSED**

**Decided 2026-08-21.** `CLAUDE.md` said the kernel's language was an open
question and that Python should not be assumed. Every one of the 59 planned
workstreams assumes Python, so the decision was being closed by momentum rather
than by choice. Closing it explicitly.

Python, because the kernel's job is to compose capabilities that are already
Python and already bound to Postgres — the database layer, the ledger, the
belief layer. A kernel in another language would spend its life marshalling
across a boundary it created.

This is revisitable, but it now requires an argument rather than an absence.

---

## D6 · OIDC deployment is out of scope; two specific holes still get fixed

**Decided 2026-08-21.**

**OIDC** (OpenID Connect) is the standard way an app hands sign-in off to an
identity provider — "log in with Google/Okta/Entra" — instead of managing
passwords itself. Aleph has two auth modes: `local`, which trusts a configured
developer identity and is the only mode deployed; and `oidc`, which is
half-built.

Standing up an identity provider is not a goal, and nothing is measured in
`oidc` mode. But two known holes get fixed anyway, because both are cheap or
because both hurt `local` mode too:

- The Node bridge between browser and agent forwards no credential, which also
  leaves port 4000 an open proxy — a `local`-mode problem. (`WS-D3`)
- Server-sent event streams cannot carry a credential, and every live surface in
  Aleph is one. In `oidc` mode the entire workspace is dark, which is not partial
  degradation. (`WS-D4`)

---

## D7 · Three unread concepts are deleted

**Decided 2026-08-21.** Each exists, is maintained, and is read by nothing. See
`WS-X1`.

**`access_scope`** is a column on every table, meant to record who a row is
visible to — project-wide, private, and so on. It has **70 places that write a
value and zero that ever read one**. No query filters on it. It looks like an
access control and is not one; the real control is the role check on each API
route. An authorization concept that is written everywhere and enforced nowhere
is worse than none, because it reads as protection that is not there. Deleted.

**`audit/`** is a second acceptance gate, parallel to `scripts/acceptance.sh`.
The two disagree about which checks exist, in both directions, and `audit/`
still asserts a defect that was fixed. Two gates that disagree are worse than
one gate. Deleted — after its seven real browser test files are harvested.

**`aleph-datasets`** is a workspace package defining `Dataset`, `DatasetVersion`
and `Observation` tables, for recording structured experimental data. Nothing
uses it. Its only importers are the database migration runner and a test that
checks migrations match models. No route, no worker, no agent tool touches it.
It was built for a capability that was never wired up. Deleted; if structured
datasets are wanted later, they should be designed against a real use.

---

## D2 · Reference implementations are read, not depended on

**Standing.** `deepseek-harness`, `cordis`, `prime-agent` and `graphify` are MIT
and are blueprints to reimplement and improve on. None of them — and no
`@deepseek-ai/*` package — is a runtime dependency. Ported code carries a
`NOTICE` recording upstream, licence and per-file lineage.

---

## D3 · Merge candidates are proposed, never applied silently

**Standing.** Deterministic passes generate scored merge candidates with named
reasons. A merge is an approval, not an inference.

---

## D4 · Every state mutation writes a ledger event in the same transaction

**Standing.** Hash-chained and append-only. If the write and its ledger entry
can come apart, the ledger records what was attempted rather than what happened.

---

## D8 · Subagent attribution comes from the config, not from the experimental event stream

**Decided 2026-08-21. Closes the open question in `plan.md` `WS-C3a` / backlog H7.**

### The question

The Inspector needs to say *which* agent did a thing — orchestrator, retriever,
researcher, and so on — and not merely that a thing happened. Deep Agents ships
a live subagent stream that looks like the obvious source, and the backlog
assumed it.

### What was actually checked

Two facts in the installed libraries, both verified rather than remembered:

1. **`ag_ui_langgraph` hardcodes `version="v2"`.** `agent.py:589` builds its
   stream kwargs with `version="v2"` and passes `subgraphs=...`.
2. **`astream_events(version="v3")` refuses those kwargs.** LangGraph's
   `_reject_v3_invariant_kwargs` (`pregel/main.py:378-390`) raises `TypeError`
   on `stream_mode` or `subgraphs`, because v3 owns both: "stream_mode is built
   from the transformer mux, subgraphs is forced True so nested namespaces flow
   through scoped muxes."

So moving to the newer stream is not a flag — it means owning the event
translation that `ag_ui_langgraph` currently does, on an API whose kwargs
contract has already changed once.

### The decision

**Attribution comes from `config["configurable"]`, on the tool-call path.**
`AlephAgentMiddleware` reads the run id and the subagent name there and writes
them onto every `agent_events` row. That channel is not a workaround: it is the
one deepagents forwards to subagents, and it is the same channel the agent's
tools already read their project scope from.

### Why this is the better answer regardless

The event stream reports *what the framework emitted*; the config reports *what
Aleph asked for*. Aleph needs the second. A subagent that never streams an event
still has a run id, and a timeline built from the framework's own event vocabulary
would change shape whenever that vocabulary did.

There is also a specific trap this avoids. `model_calls.agent_run_id` existed as
a column and was **unconditionally NULL for the whole life of the feature**,
because its reader looked in `metadata` while nothing ever wrote it there.
`configurable` is the channel that is actually forwarded;
`tests/integration/test_chat_turn_is_recorded.py::test_the_run_id_travels_in_configurable_not_metadata`
pins the distinction so it cannot be re-made quietly.

### Revisit when

`ag_ui_langgraph` moves to `version="v3"` itself, or Aleph has a reason to own
the event translation for something other than attribution. `stream.subagents`
is not adopted; `deepagents.SubagentRunStream` stays unused.

---

## D9 · What happens to the rows written while attribution was broken

**Decided 2026-08-21.** `docs/plan.md`'s sequence step 1 asks for this in
writing: *"decide in writing what happens to 786 ungrounded claims: re-extract
or delete."* Two different kinds of legacy row, and they get opposite answers,
for the same reason.

### Ungrounded citations — **re-extract, do not delete**

831 citations carry no verbatim quote and no character span, because
`BeliefService` has never run: the wiki ingest path built `CitationDraft` with
`chunk_ids=[]` and no quote. They are not wrong, they are *thin* — each one
names a real source for a real claim, and the source is still in the corpus.

So they are **re-derivable**. `WS-RS8` writes the extractor and
`BeliefService.rebuild` already accepts it as an injected callable and is tested
for determinism, idempotence, and not destroying human corrections. Running it
over the existing sources regenerates the same claims WITH quotes and spans.

Deleting them would throw away the only record of which sources support which
claims, in exchange for nothing: the number would go to zero because the
denominator went to zero. That is the shape of measurement this project keeps
catching itself in.

**So number 3 stays red until `WS-RS8` runs.** That is the honest reading: the
belief layer has not run, and the number says so.

### Uncosted model calls — **retain, and measure forward**

159 `model_calls` rows have `pricing_source='unknown'` or a NULL
`agent_run_id`. Unlike the citations these are **not re-derivable and not
wrong**: they are true records of calls that really were made during a period
when the agent path had no price list (`WS-MEP-1`) and no run to attribute to
(`WS-C3a` / `WS-D2`). The spend happened. Nobody can now say what it cost or
which turn it belonged to, because nothing recorded it at the time.

Deleting or backfilling them would make the ledger say something nobody knows to
be true. The ledger is append-only and hash-chained precisely so that it records
what happened rather than what would be convenient — a retrospective repair here
would be the first exception, and there is never only one.

**They stay. Number 5 is measured forward**, over rows written after the fix,
and the historical count is printed beside it so it is retained rather than
hidden. When the last of them ages out of any window anyone cares about, it will
be because time passed, not because somebody edited history.

### The rule this generalises to

A legacy row that is **thin** and re-derivable from data still held is
regenerated. A legacy row that is a **record of something that happened** is
kept, even when it is embarrassing, and the measurement moves rather than the
data.


---

## D10 · A cosine-distance floor does not make retrieval abstain — 2026-08-22

**Decision: do not ship a relevance threshold on the dense leg. Expose the
absolute scores instead, and leave abstention to WS-RS6.**

`WS-RS5`'s new `unanswerable` category measured something nobody had measured:
asked a question its corpus cannot answer, Aleph's retrieval returns passages
anyway, **8 times out of 8**. The abstain rate is 0.00. There is no relevance
floor anywhere in `search_corpus` — RRF fuses two rankings, and a rank-based
score says nothing about whether the top item is any good. The best of a bad set
and a perfect match score identically.

The obvious fix is a threshold on cosine distance. It was measured against the
live corpus (53 sources, 3,479 chunks, `bedrock-titan-embed-text`), taking the
nearest chunk for each query:

| query | nearest-chunk cosine distance |
| --- | --- |
| "What is reward hacking in RLHF?" | **0.814** |
| "How does neural architecture search reduce the cost of model design?" | **0.803** |
| "What causes write amplification in solid state drives?" | **0.430** |
| "the offside rule in association football" | 0.840 |
| "how sourdough starter is maintained" | 0.909 |
| "the tuning of a baroque harpsichord" | 0.894 |

**The distributions overlap.** A floor at 0.82 abstains on two of the three real
questions. The widest gap that separates nothing — 0.814 answerable against
0.840 off-corpus — is 0.026, which is not a discriminator, it is noise with a
threshold drawn through it.

So a threshold would trade a defect nobody has complained about (answering an
unanswerable question) for one they would complain about immediately (refusing
to answer a real one), and it would look principled while doing it.

**What ships instead.** `ChunkHit` now carries `cosine_distance` and
`lexical_rank`. Both legs already computed them — the dense leg ordered by
`cosine_distance` and selected five columns not including it, the lexical leg
did the same with `ts_rank` — so the only signal capable of telling a real match
from the nearest irrelevant passage was being discarded at the moment it was
calculated. Anything that wants to reason about relevance now can, including
RS6's reranker, which is the thing that can actually do this: a cross-encoder
scores the pair rather than the distance between two independently-embedded
points, and that is where the separation lives.

`WS-RS5` criterion 6 (abstain rate ≥ 0.80) therefore stays **unmet**, and the
eval reports `abstain 0.00` rather than omitting the row. A metric that is
silently absent reads as a metric that passed.

---

## D11 · A long job returns a ticket; there is no `AsyncSubAgent` — 2026-08-22

**The choice.** When the assistant is asked for work measured in minutes —
reindex the corpus, sweep every page for review — it starts a **background
task** and hands back a ticket id, then answers. It does not hold the turn open.
The ticket is an `agent_runs` row; progress is `agent_events`; the three verbs
are `start_background_task`, `check_background_task`, `cancel_background_task`.

**What was rejected, and why.** The obvious shape is an `AsyncSubAgent`: a
subagent the orchestrator spawns without awaiting, whose result arrives later on
the same graph. deepagents can express it, and it keeps everything inside the
agent framework, which is genuinely attractive.

It was rejected for three reasons, in order of weight.

1. **A detached coroutine has no record.** The whole point of this workstream is
   that a user can ask "is that still running?" twenty minutes and one page
   reload later. An in-process task's state lives in the process; a ticket's
   lives in Postgres. The reload is not an edge case — it is the normal way
   somebody checks on long work.

2. **Cancellation has to be a checkpoint, not a signal.** Cancelling an
   `AsyncSubAgent` means cancelling a task, which lands wherever the coroutine
   happened to be — possibly mid-write. A ticket sets a flag; the handler reads
   it between units of work and stops cleanly at a known point. The difference
   is whether "stop the sweep" stops it at page 40 or lets it finish all 200 and
   then relabels the row `cancelled`.

3. **The work does not belong in the API process.** A reindex fans out one job
   per document. Running that inside the request process couples the assistant's
   responsiveness to the size of the corpus, which is exactly the coupling being
   removed.

**What this costs.** Two processes now hold two halves of one vocabulary — the
API validates against `BACKGROUND_TASK_KINDS`, the worker dispatches on
`BACKGROUND_TASK_HANDLERS`, and apps may not import apps. Nothing but
`apps/workers/tests/test_background_task_kinds.py` stops them drifting, and it
fails in both directions: a kind with no handler is a ticket that can only fail,
and a handler nobody can request is dead code that reads as capability.

**What was nearly lost to it.** The first pass shipped the routes, the record
layer, the worker supervisor and the cancellation machinery with **no consumer**
— no agent tool, no UI, nothing. A `grep` for `background-tasks` found the route
file, its own test, and one docstring. The mechanism was complete and
unreachable, which is the dominant defect class in this codebase and the reason
`CLAUDE.md` says to ship a consumer with every producer.

Two more things were true and untested at that point, both found by adversarial
review rather than by the suite:

- The cancellation checkpoint existed in the two shipping handlers and **no test
  touched it** — deleting `if await task.cancelled(): break` from both left every
  test green. The only mid-flight cancel test ran a fixture handler that supplied
  its own checkpoint. The plan's own risk note said it: *a flag nobody checks
  looks identical to a flag that works until you test it.*
- `parent_agent_run_id` was written straight into `agent_events`, which carries
  no `project_id` and no foreign key — so a caller with EDITOR on project A could
  name a run owned by project B and put a dispatch row in B's timeline.
  Reachable from a prompt. It is now refused, with the same answer for "no such
  run" so the field cannot be used to probe for ids.

---

## D12 · An unreadable answer is not an abstention — 2026-08-22

**The choice.** When the reranker's reply cannot be understood, retrieval keeps
fusion order. Only a well-formed, empty `relevant` list empties the result.

**Why it needed deciding.** `parse_scores` returned `[]` for both, and
`apply_ranking` reads `[]` as "the model judged none of these relevant" — the
abstention signal, which empties the result deliberately (D10: nothing else in
Aleph can tell an answerable question from an unanswerable one). So a reranker
nobody could parse was indistinguishable from a reranker exercising perfect
judgement, and the destructive reading won by default.

**Measured.** With `gemma-4-e2b` bound to `Capability.RERANK` — a small model
that frequently answers `{"results": [{"index": 3, "relevance": 0.9}]}` instead
of the requested shape — the 45-question eval went **nDCG@10 0.970 → 0.133** and
recall@20 **1.00 → 0.13**. Retrieval was not returning worse answers. It was
returning nothing, and reporting a high abstention rate while it did. After the
fix: **0.978**.

**Three states, not two.** `Judgement` now carries a `malformed` reason:

- a well-formed empty list → abstain, empty the result;
- a list from which every entry had to be dropped → malformed, keep fusion order;
- no `relevant` key, or not a list, or not JSON at all → malformed, same.

A partially usable list is a judgement: one good entry among rubbish is honoured
and the rubbish is dropped, because dropping a malformed entry is repair the
model did not authorise and honouring a good one is not.

**The same rule, one level up.** A reranker may not take down the search it is
decorating. `search_corpus` now catches any failure of `reranker.rank` and
returns fused order with the exception's own words on the span. This is not
hypothetical: the reference gateway answers `POST /v1/rerank` with **500**
"Unsupported provider: bedrock_mantle" — not the 4xx `RerankUnsupported` was
written for — so binding the capability turned every corpus search into an
unhandled `HTTPStatusError`, and `AdaptiveReranker`'s LLM fallback, built for
exactly this deployment, never ran on it.

**Degraded, always out loud.** Every one of these paths writes the reason to
`retrieval.rerank.skipped` and to the log. A silent fallback to fused order is
indistinguishable from a reranker that ran and agreed with fusion, which is the
confusion the attribute exists to prevent — and it is the same rule the embedder
already follows: a dead embedder degrades to lexical-only and says so.
