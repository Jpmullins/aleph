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

---

## D13 · Plugin trust is a display attribute, not an approval gate — 2026-08-22

`WS-A4` c6 asked for two things: the three trust tiers observable at the API,
and an `authored`-tier save answering `requires_approval: true`. The first is
built. **The second is withdrawn**, on two facts verified rather than assumed:

1. **`plugin.settings.save` is already gated at OWNER** (`a2ui_handlers.py`),
   on top of the route's EDITOR gate. `requires_approval: true` would ask a
   project owner to approve their own change.
2. **No agent tool can dispatch an arbitrary card action.** An AST pass over
   `copilot_agent.py` finds exactly two call sites of
   `_dispatch_card_action_impl`, both naming their action as a string literal
   — `compose_dossier` and `spotlight`. There is no agent path to
   `plugin.settings.save` at all.

So the tier of the *plugin* is not the authority of the *actor*. An approval
gate keyed on provenance gates the wrong thing: it would stop an owner editing
a plugin they authored, and it would not stop anything an agent can do,
because an agent cannot reach the action.

**Trust stays visible and load-bearing where it belongs** — the API reports it
so a person can see what a capability came from before they configure it, and
`preview_removal` reads the same declaration graph the refusal does.

**Pinned, because a withdrawal recorded only in prose is what this repository
distrusts.** `tests/integration/test_plugin_settings_contract.py`:

- `test_a_save_does_not_branch_on_the_declared_trust[core|verified|authored]`
  asserts against the WHOLE serialised response — not a key someone must
  remember to check — that no tier answers `requires_approval`, and that the
  value really lands at every tier.
- `test_the_agent_cannot_reach_the_settings_save_action` is an AST pass, not a
  grep. The property is not "the string is absent" (still true of a tool that
  takes `action_kind` as a parameter) but "every dispatch CALLED BY NAME passes
  a literal, so the reachable set is decidable, and this one is not in it". It
  carries an anti-vacuity assertion so renaming the seam fails rather than
  passing silently, and it scans all of `apps/api/src`, so a tool in a new
  module is not invisible to it.

  **Its limit, stated rather than implied.** "Called by name" is exact: the
  walk matches `ast.Name` funcs, so rebinding the function object evades it —
  `_alias = _dispatch_card_action_impl` and `globals()["…"]` both survive,
  measured. A wrapper taking `kind` as a parameter and a registry-dict lookup,
  the two shapes this would plausibly grow into, are caught. The premise of a
  withdrawal has to say what it does not cover.

**Reopen this if** an agent tool ever dispatches a card action by variable, or
if `plugin.settings.save` is loosened below OWNER. Either change makes the
provenance of the plugin start to matter again, and both are visible to the
tests above.

---

## D14 · `derived_from` edges are not written, and that is the decision — 2026-08-22

`aleph_wiki.derivation.record_derivations` is a complete, tested writer with no
production caller, and it stays that way. `claim_edges` holds two rows, both
`supersedes`. **Three agents have now declined to wire it**, each independently
and for the same reason, and that reasoning has lived only in commit messages.

A `derived_from` edge asserts that belief B rests on belief A. **Aleph never
learns that.** Every claim is extracted from a source CHUNK with a verbatim
quote and a character span, and `BeliefService.upsert_claim` is the only
writer. The research composer cites chunks; the synthesis workflow cites
chunks; the curator writes no claims at all. There is no point in the pipeline
where one claim's dependence on another is *known* rather than guessable.

**Writing the edge from a model's guess would be worse than leaving it empty.**
`aleph_reviewer.retraction.retraction_impact`'s second hop propagates a
retraction along these edges, so guessed dependencies mean retracting
conclusions that do not rest on the withdrawn paper — a false blast radius,
which is the one failure mode the belief layer exists to prevent.

**What was NOT defensible was the silence.** `describe_impact` wrote
`0 derived from those (deepest hop 0)` whether the walk found no dependants or
whether there was no graph to walk, and on this instance it has only ever been
the second. Those two readings are opposite and the sentence could not tell
them apart. `RetractionImpact` now carries `derivation_graph_is_empty`,
measured with one `LIMIT 1` and defaulting to the pessimistic reading, and the
output says:

> `0 derived from those — NOT because nothing depends on them: this project has
> no 'derived_from' edge at all, so the second hop had no graph to walk`

**This supersedes `WS-RS9` criterion 4** — *"`select count(*) from claim_edges
where kind='derived_from'` > 0 after a research run"* — which is withdrawn. The
replacement is the one above: the absence is reported AS an absence, in the two
places a reader would otherwise read it as a measured zero.

**Reopen this if** a pipeline stage ever learns a real dependency — a composer
that cites a CLAIM rather than a chunk would be one. The writer is already
there; only the caller is missing, deliberately.

## D15 · A plugin is a capability. A skill is a document. They were fused, and the fused half was dead — 2026-08-25

**The choice.** `plugins.source_kind` loses the `"skill"` value. A durable plugin
is a **kernel capability**: code with a `setup`, an inverse, a live `probe`, and
accurate `provides`/`requires`. An agent-authored **skill** is an instruction
document and stays where it already works — the deepagents `StoreBackend` at
`/skills/authored/` (`copilot_agent.py:2627`, sourced at :2681). Aleph does not
build a second skill system, and `WS-H1` already built the first one.

**Why it needed deciding.** It did not need deciding. The plan had it right and
the implementation collapsed it, so this record exists to say so and to stop the
collapse being re-derived.

`WS-A2`/`WS-A7` is the kernel surface: an agent authors a capability, previews
what turning it off would break, turns it off. `WS-H1` is a separate workstream
whose own words are *"a short instruction document, optionally with helper
code"*, delivered through the deepagents skill backend. Two mechanisms, kept
apart — the same split the reference harnesses make between a skill catalog and
model-written runtime code (D2: read, not depended on).

What shipped was one `plugins` table with `source_kind ∈ ("skill", "capability")`
holding `instructions` **and** `code`. That dragged instruction documents into
the plugin table and created a second skill mechanism next to the working one.

**Measured — the fused half is dead code, not merely redundant.**

`source_kind` is written to the row (`plugin_service.py:195`) and into the
append-only ledger (:219). The model's own docstring claims the column exists
because *"they differ in what reconstitution does with them"*. **Nothing branches
on it.** `install` calls `skill_from_source(...)` unconditionally
(`plugin_service.py:156`) and so does `reconstitute` (:270). There is no
capability path. Every "plugin" Aleph can author is a skill.

Every reader of a plugin row's `instructions`:

| site | what it does |
|---|---|
| `routes/plugins.py:206` | writes it |
| `skills.py:220,227` | the capability's own probe — non-empty, and named helpers exist |
| `plugin_service.py:100` | first line → a description string for the settings card |
| `plugin_service.py:270` | `reconstitute`, which has **no production callers** |

**Nothing puts a plugin's instructions in front of a model.** The instructions
the assistant actually reads come from the deepagents store. So the table carried
durability, mounting, a `PluginId`, blast-radius machinery and a restart story
for prose that no model ever sees.

**And the count that made it visible.** 3,313 installed rows, **18 distinct
names**, every one an integration-test fixture (`literature-review`,
`good-one`, `broken-one`, `thing`, `plain-thing`). 3,312 of the 3,313 point at
projects that no longer exist. The single survivor was a probe row from an audit
an hour earlier. **Aleph has never had a real plugin.**

**What this decision does NOT say.** The kernel is not the problem and is not
being changed. Core capabilities from `aleph.toml` and agent-authored ones share
one registry; core ones simply have no `PluginId`, so deactivating them is
unexpressible rather than refused. That is the composability model working. The
divergence is entirely in the durable authoring path, which persisted the
kernel's existing *skill* constructor instead of a capability.

**The correction.**

1. `source_kind` is `"capability"` only. Dropping `"skill"` deletes a branch with
   no live reader rather than migrating anything.
2. Agent-authored skills stay in the deepagents store. `author_plugin` stops
   accepting an instruction document as the payload.
3. The domain suites become real capabilities — the thing "everything is a
   plugin" was always supposed to mean. Today **no** domain package is one:
   `aleph.toml` mounts seven infrastructure capabilities plus `scholar`,
   `realtime` and `agent_store`, and the wiki, the RAG, research, belief,
   reviewer, hypotheses, artifacts, notes and connectors are ordinary imports.
4. Reconstitution stops being blocked on prose. Capabilities are what needs
   mounting; a document does not.

**Cost of having got it wrong.** A security gate was designed, audited and
hardened around agent-authored code executed in-process, when the objects it was
protecting were instruction documents nothing read. The gate work stands — it was
a real hole and `exec()` in the API process is still how a capability's helpers
load — but it was scoped by a wrong idea of what was being stored.

**Supersedes.** The plugin-cluster description in `CLAUDE.md` and the
`SOURCE_KINDS` docstring in `aleph_db/models/plugin.py`, both of which assert the
fused model.

## D16 · Analysis of Competing Hypotheses is deleted — 2026-08-25

**The choice.** `aleph-hypotheses` is removed: three tables, a service, a REST
router, an A2UI surface and card, a pane, three agent tools, a card action, a
dedicated subagent, and the `ach` skill document that told the model to drive
them. 19 workspace packages, down from 20.

**Why.** The owner's assessment: *"it doesn't do anything."* The measurement
agrees — the pane rendered, the tools existed, and nothing used them. It is the
same defect class this project keeps finding, one layer up: a whole feature
written correctly and read by nobody.

**What moved instead of dying, and this is the part that mattered.** The package
held two unrelated things. The ACH half was dead. The other half —
`next_confidence_from_evidence` and `weight_for_tier`, the state machine that
decides a claim's confidence from its evidence — is called by
`aleph_wiki.BeliefService` on **every claim write**. Deleting the package
wholesale would have taken the wiki's confidence engine with it.

It now lives in `aleph_belief.confidence`, beside `aleph_belief.trust`, because
that is what it reads: `weight_for_tier` turns a `TrustTier` into the number the
machine scores on, so the lattice and the machine that scores it were one subject
split across two packages.

**`aleph-core` was the obvious home and is the wrong one.** It is the declared
leaf — it imports nothing — and the state machine imports `TrustTier`. The
alphabet (the six confidence *values*) stays in `aleph-core` where the A2UI
catalog and the HTML compiler can name it without pulling in the lattice; the
*transitions* live in `aleph-belief`. That split already existed and was
documented; this decision only corrects which package owns the second half.

**The subagent went with it.** `analyst`'s entire toolset was the three
hypothesis tools, so a subagent that could do nothing would have remained. Five
subagents now, not six.

**The skill went too.** The bundled `ach` skill document is an instruction document — the
kind of thing D15 says belongs in the skill store rather than the plugin table —
but its payload was "call `create_hypothesis`, then open the Hypotheses tab". A
skill instructing the model to call tools that no longer exist is worse than no
skill. Analysis of Competing Hypotheses is a real analytic method and could
return as a pure reasoning skill that needs no tables; that would be a new skill,
not this one.

**Irreversible in practice.** The migration recreates the tables on downgrade so
a rollback can proceed, and restores `hypothesis_versions`' append-only trigger
AND its function with them — a version table whose immutability guard is missing
looks fine and silently permits the edit the guard exists to refuse. The rows are
gone either way.

## D17 · Aleph hosts the Agent Protocol; deepagents supplies the delegation — 2026-08-25

**Supersedes** the agent-layer section of the plugin-architecture design published
2026-08-25, which was built on two claims that are false. Both came from a
research subagent's report that I did not check against the docs.

### What was claimed, and what is true

**Claim 1: "deepagents' async subagents are remote-only; Aleph does not run
LangGraph Platform, so they do not fit."**

False. `AsyncSubAgent.url` is **optional**. Omitted, the SDK uses **ASGI
transport — in-process**, which the docs call "the recommended default… zero
network latency… no additional auth configuration." The remote case is the
opt-in one.

ASGI transport does require the process to *be* an Agent Protocol server, which
Aleph is not. But that is not the end of it, because the docs also say async
subagents "communicate with **any server that implements the Agent Protocol**…
or self-host any Agent Protocol-compatible server."

**And the surface is five routes.** `AsyncSubAgentMiddleware` calls exactly five
SDK methods — `threads.create`, `threads.get`, `runs.create`, `runs.get`,
`runs.cancel` — which resolve to:

```
POST /threads
GET  /threads/{thread_id}
POST /threads/{thread_id}/runs
GET  /threads/{thread_id}/runs/{run_id}
POST /threads/{thread_id}/runs/{run_id}/cancel
```

**So Aleph hosts those five routes on the queue and the `agent_runs` table it
already has, and points `AsyncSubAgent(url=<its own base url>)` at itself.**

What that buys, none of which Aleph then writes: the five supervisor tools
(`start_async_task`, `check_async_task`, `update_async_task`,
`cancel_async_task`, `list_async_tasks`); the dedicated `async_tasks` state
channel, which exists specifically so task ids **survive context compaction**
when they would be lost from tool messages; the system-prompt rules that stop the
supervisor polling immediately after launch and treating history statuses as
current; and `update`'s interrupt-multitask semantics for mid-flight steering.

The previous design said "Aleph builds it, copying the shape." That was
reimplementing a middleware Aleph can simply use. `docs/decisions.md` D11 —
"a long job returns a ticket" — is unchanged and is exactly what this is; only
the conclusion that Aleph must hand-roll the ticket surface is withdrawn.

**Claim 2: "deepagents freezes its subagent roster at construction, so Aleph must
own the `task` tool."**

The frozen-roster fact is true and the conclusion does not follow, because
**dynamic subagents are not about adding subagents at runtime.** They are about
*dispatching configured subagents from interpreter code*: with subagents and
interpreter middleware both present, the interpreter exposes a built-in `task()`
global taking `{description, subagentType, responseSchema}`, and the agent writes
JavaScript that fans work out with `Promise.all`, routes by classification, or
runs adversarial verification — deterministically, instead of one model-chosen
tool call at a time. It is on by default when both are present.

Aleph already runs `CodeInterpreterMiddleware`. It gets this by changing one
argument.

### The one that is genuinely Aleph's decision

`interpreter.py` sets `subagents=False`, and the reason recorded there is
correct: `task()` from inside the REPL starts a model loop, dispatched with no
parent-level approval because the `eval` was approved once — PTC bypasses
`interrupt_on`. That is the same hazard `PTC_ALLOWLIST` exists for.

**The answer is the same shape as the answer already in the file: an allowlist.**
`subagents=True` with a `SUBAGENT_PTC_ALLOWLIST` of subagents that are safe to
dispatch without a per-call gate — read-and-analyse roles — and a
`SUBAGENT_WITHHELD` mapping recording why each of the others is not there. A
subagent that writes, spends, or installs is not dispatchable from the REPL; one
that reads and reports is. Blanket `False` bought safety by giving up the
feature; the allowlist keeps both, and is falsifiable the same way the tool one is.

### Three further mechanisms the earlier design either missed or hand-rolled

- **Runtime tool registration is supported.** `wrap_model_call` adds tools to the
  request and `wrap_tool_call` executes them — the documented path for "tools
  discovered at runtime (e.g. from an MCP server, or a remote registry)." That is
  exactly how a plugin contributes an agent tool, and it needs no fork.
- **Runtime context propagates to subagents automatically.** "When you invoke a
  parent agent with runtime context, that context automatically propagates to all
  subagents." So the plugin resolver reaches a subagent's tools through
  `ToolRuntime.context` rather than through a global.
- **Harness and provider profiles are the right home for model quirks.**
  `register_provider_profile(..., ProviderProfile(init_kwargs=...))` and
  `init_kwargs_factory` are where per-model construction kwargs belong — which is
  where the `temperature` workaround for `claude-opus-4-7` should live rather than
  as a learned drop inside `LiteLLMClient`. The client-side fix stays as the
  backstop for models nobody has profiled; the profile is the declaration.
  `HarnessProfile.extra_middleware` also accepts a **callable**, so a plugin can
  contribute middleware lazily.

### What this does not change

The core inversion stands: the kernel is a loader, everything above it is a
plugin, and the core depends on nothing above it. What changes is the agent
layer — Aleph writes an Agent Protocol host and an allowlist, not a delegation
framework.
