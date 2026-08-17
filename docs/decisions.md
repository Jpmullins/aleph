# Decisions

Why the system is shaped the way it is. Append new decisions; do not rewrite old ones — a decision
that was later reversed is more useful with its reversal attached than deleted.

---

## D1 — The compiled wiki is removed as the knowledge substrate

**Date:** 2026-08-14 · **Status:** accepted

### Context

Aleph was built on the bet that an LLM-maintained wiki should be the primary retrieval surface, with
embeddings restricted to intra-source descent and explicitly forbidden as first-line RAG. A review of
the codebase (47 agents across three fleets, plus direct verification) examined whether that bet paid.

### What we found

**The bet was never actually placed.** Three mechanisms each independently prevented the architecture
from being the thing it was described as:

1. `wiki_index.index_tsv` was built from `title + summary + aliases` and **never the page body**, so
   the retrieval index covered ~2KB summaries of documents it had spent ~20 LLM calls to compile.
2. The query gate was `plainto_tsquery`, which **ANDs every term** — so a natural-language question
   had to have all its tokens present in that summary. Most matched nothing and fell through to a
   fallback that returned arbitrary recent pages.
3. The accretion behaviour that is the entire reason a compiled wiki beats per-document RAG was
   promised in the compose prompt and **absent from the payload sent to the model**.

So there has never been evidence for or against the wiki hypothesis. It was not wrong; it was untested.

**The write primitive was the deeper problem.** Every write path — ingest stubs, curation,
cross-linking, merge — regenerated the *complete* markdown body and committed it. That single choice
produced the clobber behaviour, overview erosion, the section-splice workaround, the claim-carry
workaround, the absence of any rebuild-from-source path, and roughly 80% of ingest cost.

**And "wiki" was a costume.** Every mechanism specific to wiki-ness was absent, unreachable, or
inverted — no backlinks, no history or diff UI, no categories, no talk pages, no human write path at
all (no create, edit, rename, move, split, or delete endpoint existed). Every mechanism specific to
*cache*-ness was present and worked. The vocabulary made the mechanisms feel handled: "aliases" felt
like redirects, so nobody tested resolution; "revisions" felt like history, so nobody asked who could
see it.

### Decision

Remove the wiki as the knowledge substrate. Replace it with the **Claim Spine**
(`docs/belief-engine.md`): claims as durable, evidence-anchored nodes with typed edges and derived
confidence. Prose — HTML artifacts, reports — becomes a *render* of that layer.

### Consequences

- `packages/aleph-wiki` is legacy under removal. `curator_service.py` (929 lines), `index_service.py`,
  `alias_service.py`, `citation_verification.py`, and `feedback_service.py` are deleted outright.
- The LLM page-selector hop and `Capability.PAGE_SELECTION` are deleted — one LLM call per query
  removed from the read path.
- Retrieval moves to the hybrid retriever **that already exists** in `aleph-rks/retrieval.py`
  (`0.6·cosine + 0.4·ts_rank`), unchained from its single-source predicate, with
  `plainto_tsquery` → `websearch_to_tsquery`.
- Kept unchanged because they are genuinely good and independently verified: `compile_page_html`,
  `compute_freshness`, `inject_cross_links`, `rewrite_wikilink_target`, `apply_merge`.

### What does *not* change

The parts of Aleph that were built well are untouched: tri-state DOI verification, the hash-chained
ledger, container isolation for the code runner, disciplined migrations, and `aleph-scholar` — which
already does bidirectional citation-graph expansion and is the strongest package in the repo.

---

## D2 — Confidence is derived, never asserted

**Date:** 2026-08-14 · **Status:** accepted

A model must not be asked "how confident are you in this claim?" Confidence is a **function of the
evidence structure**: stance-weighted support and contradiction over anchored evidence.

This is not new work. `packages/aleph-hypotheses/src/aleph_hypotheses/confidence.py` already computes
`net = Σ weight·sign(stance)` → `well_supported | weakly_supported | contested | refuted`, as a pure
tested function with zero LLM calls. `html_compiler.py` already renders per-claim cards with CSS
classes matching those exact strings. **The two were never connected to each other.** Wiring them is
the work; the engine and the renderer both exist.

---

## D3 — Deterministic first, LLM as adjudicator

**Date:** 2026-08-14 · **Status:** accepted

The LLM stops being the matcher and becomes the adjudicator on a pre-filtered shortlist.

**Deterministic, always:** normalization; merge-candidate generation with scoring; verbatim citation
grounding; span/offset verification; community detection for entrenchment; structural contradiction
flagging; staleness.

**LLM, on a shortlist only:** extraction (prose → candidate claims); adjudicating merge candidates;
semantic contradiction judgment; relation typing; synthesis.

```
score ≥ high   → auto-apply         (no LLM)
score ≤ low    → auto-reject        (no LLM)
between        → LLM adjudicates    (the entire LLM budget lives here)
```

The LLM never scans the corpus; it adjudicates a shortlist. This changes cost from O(documents) to
O(ambiguous pairs), and it makes the system testable: deterministic parts get fixture tests, and evals
only need to cover the ambiguous band.

Every LLM decision emerges as a **patch** — proposed, validated, applied — not a mutation. An LLM
curation run is therefore auditable, reversible, replayable, and safe to re-run against a newer graph.

---

## D4 — Borrowed designs and their provenance

**Date:** 2026-08-14 · **Status:** accepted

Four reference systems were read in depth. Licenses were verified before anything was taken.

| source | license | what was taken |
|---|---|---|
| [graphify](https://github.com/rhanka/graphify) | MIT | the patch contract (propose → validate → apply), trust tiers, verbatim grounding, deterministic entity matching, `citationKey` union-not-clobber merge |
| claude-science **skills** | Apache-2.0 | usable verbatim with attribution — the `literature-review` skill in particular |
| claude-science **harness** | proprietary | **design reference only — no code copied.** It is a reconstruction of a compiled binary |
| [cordis](https://github.com/cordiverse/cordis) + [its paper](https://github.com/cordiverse/paper) | MIT | the spatiotemporal composability model — revertible effects, reactive coeffects, scoped context |
| [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | MIT | capability seams; honest diagnostic misses over degraded fallbacks |

Two findings that changed the design more than expected:

**claude-science ships no vector index at all.** Grepped for `sqlite-vec|vec0|hnsw|text-embedding|fts5`
across the bundle: zero hits. Retrieval is BM25 plus a token-set Jaccard leg, fused by **reciprocal
rank fusion at k=60**. A shipped research assistant does this without embeddings.

**Staleness should be relational, not temporal.** In claude-science a belief is stale because *its
subject moved* — the artifact it is about is now at a different version — not because a clock ticked.
Age is only the fallback when no subject relation exists. This is strictly better than a decay score,
and it is cheap.

---

## D6 — What success looks like

**Date:** 2026-08-14 · **Status:** accepted

Salvaged from the original goal document (`start.md`, since deleted for being wiki-centric) and
updated for the harness reframe. These are acceptance conditions, not aspirations — each one should be
checkable.

**Knowledge.** The system can create a project from a title, description, constraints and allowed
connectors; collect raw material into the RKS; normalize it into machine- and human-readable form;
build a project-scoped body of belief from it; maintain evidence references and cross-claim relations;
update incrementally as new material arrives; track claim-level provenance; **identify unsupported,
weakly supported, stale, or conflicting claims**; let users approve or reject proposed changes; and
preserve the full history of ingestion, revisions, reviewer findings, artifacts, approvals and agent
actions.

**Interaction.** The user can enter a project immediately after creation and *see real work
progressing*; understand what agents are doing, what completed, what is blocked, what needs approval;
chat while background work continues; navigate sources, artifacts, hypotheses, notes and the review
queue; ask for more research, new hypotheses, visualizations and artifacts; interact through structured
cards rather than chat alone; and export reports, datasets and source bundles with lineage.

**Harness** (new under D5). The agent can author a plugin for itself, load it, and have it refused if
it fails validation; activate and deactivate capability as needed; and be **prevented from deactivating
load-bearing capability**. A faulty self-authored plugin must be recoverable without a restart.

**Engineering.** Typed interfaces. Database migrations. Auth and project scoping. Structured logging.
Tracing. Test coverage on critical paths. **No fake implementations merged to main** — which, per the
2026-08 review, must now explicitly include *no write path merged without its read path*.

---

## D5 — Aleph is a self-improving harness; the kernel is the product

**Date:** 2026-08-14 · **Status:** accepted (scope) · kernel language **open**

### The reframe

Aleph is no longer "a research assistant." It is a **general-purpose, self-improving multi-agent
harness**, and the research capability becomes its first plugin suite.

The product thesis, in the owner's words: *"a better version of prime agent… it can create plugins for
itself and activate/deactivate as needed (guardrails should prevent deleting important features)."*

### Why this makes the kernel non-optional

Under the old framing a plugin kernel was an infrastructure nicety, and "just restart the process" was
a reasonable answer to hot-reload. Under this framing it is not. Paper §1.2.2 describes the exact
system being built:

> *"A future harness may generate and deploy modifications to its own components while continuously
> serving requests… Without temporal composability, each self-modification forces a full restart that
> discards all process-local accumulated state… even worse, a faulty self-modification can disable the
> very process needed to recover."*

So **temporal composability is load-bearing**, and §1.2.3's "coarse-grained workaround" (restart the
process, restart the container) is explicitly rejected.

Equally, a **fixed set of host-provided extension points is the anti-pattern**, named in §1.2.1 via
VSCode: only 7 of the top 100 extensions declare dependencies on one another, because the API exposes
fixed surface-level extension points and no structured way for extensions to depend on each other. An
earlier draft of this design proposed exactly that (`aleph.connectors`, `aleph.skills`, `aleph.tools`)
and was wrong. Services are arbitrary keys; plugins depend on plugins.

### The guardrail requirement

"Guardrails should prevent deleting important features" is a first-class requirement, and the model
supplies most of the machinery:

- **Dependent closure** — Alg. 3 (`notify`) and Alg. 5 (`unload` waits for notified dependents to
  reach INACTIVE) mean the kernel can compute the blast radius of a proposed deactivation *before* it
  happens, and refuse.
- **Trust tiers** — `packages/aleph-belief/src/aleph_belief/trust.py` already implements
  `unverified < asserted < earned < signed`. Agent-authored plugins are `asserted`; core plugins are
  `signed`; an `asserted` plugin cannot supersede a `signed` one. The lattice ported for beliefs
  applies unchanged to plugins.
- **Capability declaration** — Alg. 6 raises `UNDECLARED_ACCESS`, so a plugin cannot reach what it did
  not declare. That is the substrate for safely running agent-authored code.

Self-improvement without rollback is not self-improvement. Probation and revert are part of the
contract, not a later addition.

### Build, don't adopt

`deepseek-harness` is a working general-purpose harness on cordis and is MIT — but it will **not** be a
runtime dependency. The owner does not want to inherit another lab's roadmap, telemetry decisions, or
breaking changes in the core runtime. It is a **blueprint to reimplement and improve on**.

Note also that upstream `cordis` is *not* a DeepSeek project (`cordiverse/cordis`, MIT © 2021-present
Shigma, 537 of 550 commits by one author) — DeepSeek adopted it, vendored it, co-authored the paper,
and ships separate `@deepseek-ai/cordis-plugin-*` forks. But cordis has bus-factor 1 and its README
warns the API is unstable. So the position is: **implement the paper, not the package.** The paper is
the durable artifact — it carries a metatheory with proofs. cordis and deepseek-harness are two
reference implementations to learn from.

`prime-agent` is the first-class reference for the self-improvement loop specifically — recursive
skilling, the supervisor-owned spawn ledger as "family authority", and the recursion boundary.

### Open

Kernel language and structure. The transactional constraint is real and narrow: every state mutation
writes an `ActionLedgerEvent` in the **same transaction**, so anything touching belief state shares a
DB session and cannot be split by a process boundary. That constrains where the belief plugin lives; it
does not by itself decide the kernel. Resolve before Stage 2.

One insight is load-bearing regardless of the answer: cordis's `isolate` — the two-layer resolution
`k → ρ(k) → σ(ρ(k))` — **is project scoping**. Aleph's "every row carries `project_id`" is today a
discipline every author must remember; under this model it becomes a kernel primitive.
