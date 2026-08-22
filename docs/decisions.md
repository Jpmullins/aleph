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

