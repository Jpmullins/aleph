# Increment 5 — Reviewer Agents + Approval Workflow + Hypotheses + AgentMemory

**Parent spec:** `docs/superpowers/specs/2026-05-26-aleph-design.md`
**Depends on:** Inc 0–4
**Status:** Subsystem design spec.
**Written:** 2026-05-27.

## 5.1 Scope

Increment 5 ships the governance and analyst-authored layers. After Inc 5:

- Every wiki revision is auto-validated by **MechanicalReviewer** (extends Inc 3's `citation_verification` to broken/stale wikilinks, hash mismatches, duplicate sources, citation freshness, alias consistency, schema validation).
- **EditorialReviewer** (Deep Agents) operates on schedule + threshold to flag contradictions, weak sources, narrative gaps, coverage gaps.
- All reviewer findings flow into the existing `BriefsSurface` as `FindingCard`s and `ApprovalCard`s.
- Rejection feedback flows from analyst UI back into the wiki agent's next compile.
- **Hypotheses** are first-class: `Hypothesis` / `HypothesisVersion` / `HypothesisEvidence` with confidence states and version history. `HypothesisCard` and `HypothesesSurface` (catalog-defined in Inc 4) gain real backing data.
- **AgentMemory** lands: per-project, per-agent structured scratchpad.

### In scope

- `ReviewRun` + `ReviewFinding` models
- `ApprovalRequest` (wraps `ApprovalDecision` from Inc 3 in richer workflow)
- `MechanicalReviewer` agent (LangGraph) — auto-runs on every wiki revision
- `EditorialReviewer` agent (Deep Agents) — scheduled + threshold-triggered
- `Hypothesis` + `HypothesisVersion` + `HypothesisEvidence`
- `AgentMemory`
- Rejection feedback UX wiring (in addition to API from Inc 1)
- Diff view (`DiffCard` from Inc 4 now binds to real revision-pair data)
- Hypothesis assistant integration: assistant can propose hypothesis updates → approval
- Tests, docs, eval datasets for reviewer recall + precision

### Out of scope

- Datasets → Inc 6
- Builder, artifacts → Inc 7
- Eval suite expansion → Inc 8 (Inc 5 ships its own eval datasets; cross-cutting eval framework is Inc 8)

### Dependencies

- Inc 0–4 fully, including:
  - Inc 1: `WikiPage`/`WikiRevision`/`WikiClaim`/`Citation`/`HandEditMark`/`RejectionFeedback`/`Alias`/`WikiIndex`
  - Inc 3: `ApprovalDecision`, `SynthesisProposal`, AIQ pipeline, `citation_verification` node
  - Inc 4: A2UI catalog with `FindingCard` / `ApprovalCard` / `HypothesisCard` / `DiffCard` / `HypothesesSurface` / `BriefsSurface` schemas

### Downstream

- Inc 6: charts/maps/graphs can embed in `HypothesisEvidence` views
- Inc 7: Builder can export approved synthesis + reviewer findings as part of report appendix
- Inc 8: reviewer recall/precision become first-class eval metrics

---

## 5.2 Repository changes

```
packages/
├── aleph-reviewer/                     # new package
│   └── src/aleph_reviewer/
│       ├── __init__.py
│       ├── models.py                   # ReviewRun, ReviewFinding, ApprovalRequest
│       ├── review_service.py
│       ├── approval_service.py
│       ├── mechanical/
│       │   ├── __init__.py
│       │   ├── workflow.py             # LangGraph; per-revision pass
│       │   ├── checks/
│       │   │   ├── citation_match.py   # wraps AIQ citation_verification
│       │   │   ├── broken_links.py
│       │   │   ├── stale_sources.py
│       │   │   ├── hash_mismatch.py
│       │   │   ├── duplicate_sources.py
│       │   │   ├── alias_consistency.py
│       │   │   └── schema_validation.py
│       │   └── prompts/                # for the few LLM-judged checks
│       └── editorial/
│           ├── __init__.py
│           ├── workflow.py             # Deep Agents harness
│           ├── subagents/
│           │   ├── contradiction.py
│           │   ├── weak_source.py
│           │   ├── narrative_gap.py
│           │   ├── coverage_gap.py
│           │   └── factual_freshness.py
│           └── prompts/
├── aleph-hypotheses/                   # new package
│   └── src/aleph_hypotheses/
│       ├── __init__.py
│       ├── models.py                   # Hypothesis, HypothesisVersion, HypothesisEvidence
│       ├── hypothesis_service.py
│       └── confidence.py               # confidence-state transitions + rules
└── aleph-core/src/aleph_core/
    └── agent_memory.py                 # AgentMemory model (extends Inc 0 core)

apps/api/src/aleph_api/routes/
├── reviews.py                          # ReviewRun list, ReviewFinding fetch, manual trigger
├── approvals.py                        # ApprovalRequest CRUD (extends Inc 3 endpoints)
├── hypotheses.py                       # CRUD + evidence + version history
└── feedback.py                         # extended for analyst-facing rejection from Briefs

apps/workers/src/aleph_workers/jobs/
├── mechanical_review.py                # per-revision job
└── editorial_review.py                 # scheduled + threshold-triggered

apps/web/src/
├── components/
│   ├── ReviewFindingDetail.tsx         # used inside BriefsSurface detail pane
│   ├── HypothesisEditor.tsx            # used inside HypothesisCard / HypothesesSurface
│   ├── EvidenceBrowser.tsx
│   └── RejectionDialog.tsx             # captures reason on reject
└── a2ui/components/
    ├── FindingCard.tsx                 # extended; now binds to real ReviewFinding rows
    └── HypothesisCard.tsx              # extended; now binds to real Hypothesis rows
```

---

## 5.3 Domain model

### 5.3.1 Reviewer

```python
# packages/aleph-reviewer/src/aleph_reviewer/models.py

class ReviewRun(CommonColumns, Base):
    __tablename__ = "review_runs"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # mechanical | editorial
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    # revision_commit | scheduled | manual | threshold
    target_revision_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    # for mechanical: the revision under review
    target_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="revision")
    # revision | page | project
    agent_run_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    # running | completed | failed
    finding_count: Mapped[int] = mapped_column(nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class ReviewFinding(CommonColumns, Base):
    __tablename__ = "review_findings"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    review_run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    finding_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # citation_match_failure | broken_wikilink | stale_source | hash_mismatch |
    # duplicate_source | alias_inconsistency | schema_invalid | contradiction |
    # weak_source | narrative_gap | coverage_gap | factual_staleness
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    # info | low | medium | high | critical
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    target_page_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    target_revision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    target_section_anchor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    target_claim_id: Mapped[UUID | None] = mapped_column(nullable=True)
    target_source_id: Mapped[UUID | None] = mapped_column(nullable=True)
    evidence_refs_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # refs to chunks, source pages, prior revisions, conflicting claims
    proposed_patch_jsonb: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # editorial reviewer may propose a concrete patch
    auto_resolvable: Mapped[bool] = mapped_column(nullable=False, default=False)
    # true for mechanical findings the system can fix without human approval
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    # open | approved | rejected | superseded | auto_resolved
    approval_request_id: Mapped[UUID | None] = mapped_column(nullable=True)

class ApprovalRequest(CommonColumns, Base):
    """Wraps an ApprovalDecision (Inc 3) with workflow state and target context."""
    __tablename__ = "approval_requests"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # synthesis_proposal | review_finding | wiki_revision | hypothesis_update
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    proposed_patch_jsonb: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    evidence_refs_jsonb: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    requested_by_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # aleph_agent | aiq_agent | user
    requested_by_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # pending | approved | rejected | expired | superseded
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # FK approval_decisions.id (Inc 3)
```

### 5.3.2 Hypotheses

```python
# packages/aleph-hypotheses/src/aleph_hypotheses/models.py

class Hypothesis(CommonColumns, Base):
    __tablename__ = "hypotheses"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    short_id: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    # H0001 — referenced as [[Hypothesis:H0001]] in wiki/notes
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(String(24), nullable=False, default="under_investigation")
    # under_investigation | weakly_supported | well_supported | contested | refuted | abandoned
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # active | resolved | archived
    current_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    last_evidence_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class HypothesisVersion(Base):
    """Immutable. Each update creates a new version."""
    __tablename__ = "hypothesis_versions"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    hypothesis_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(String(24), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # short narrative explaining the confidence change
    author_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    author_id: Mapped[UUID] = mapped_column(nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ledger_event_id: Mapped[UUID] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("hypothesis_id", "version_no"),)
# Immutable triggers, same as wiki_revisions

class HypothesisEvidence(CommonColumns, Base):
    """Edge from a Hypothesis to supporting/contradicting evidence."""
    __tablename__ = "hypothesis_evidence"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    hypothesis_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    hypothesis_version_id: Mapped[UUID] = mapped_column(nullable=False)
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    # supports | contradicts | contextualizes
    evidence_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # claim | source_page | chunk | finding | other_hypothesis
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    weight: Mapped[float] = mapped_column(nullable=False, default=1.0)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
```

### 5.3.3 AgentMemory

```python
# packages/aleph-core/src/aleph_core/agent_memory.py

class AgentMemory(CommonColumns, Base):
    """Per-project, per-agent structured scratchpad. Indexed, queryable.
    Distinct from AssistantThread/Message (those are user-facing conversation history).
    Distinct from AgentEvent (those are progress events on an AgentRun).
    """
    __tablename__ = "agent_memories"
    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    agent_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    # agent-defined sub-scope, e.g. "concepts_seen", "rejected_concepts"
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value_jsonb: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("project_id", "agent_kind", "namespace", "key"),
        Index("ix_agent_memory_lookup", "project_id", "agent_kind", "namespace"),
    )
```

### Migration

`<timestamp>_inc5_reviewers_hypotheses.py` creates:
- `review_runs`, `review_findings`, `approval_requests`
- `hypotheses`, `hypothesis_versions`, `hypothesis_evidence`
- `agent_memories`
- Immutability triggers on `hypothesis_versions`
- Seeds nothing (rows created by operation)

---

## 5.4 MechanicalReviewer

`packages/aleph-reviewer/src/aleph_reviewer/mechanical/workflow.py` is a LangGraph workflow that runs on every wiki revision commit.

### Trigger

The wiki service emits an event on `commit_revision` success. An Arq job listener picks it up and enqueues `mechanical_review_job`.

### Nodes (parallel where possible)

| Node | Check | Cost |
|---|---|---|
| `citation_match` | Wraps AIQ's `verify_citations`. For each claim, verify `[c…]` markers map to real Citation rows pointing to existing chunks/source pages. | LLM (judge) — light |
| `broken_links` | For each `WikiLink` with `dst_page_id IS NULL`, attempt repair via Alias. If still null AND occurrence > 1, raise finding `broken_wikilink`. | deterministic |
| `stale_sources` | For each Source referenced by the revision: if `retrieval_timestamp` is older than `freshness_threshold` (per source kind), raise `stale_source`. | deterministic |
| `hash_mismatch` | For each `SourceAsset.sha256` referenced in citations: verify the stored asset matches; on disagree raise `hash_mismatch` (asset replaced unexpectedly). | deterministic |
| `duplicate_sources` | Detect Sources in the same project with identical content-hash but different IDs (e.g. user uploaded twice). Raise `duplicate_source` with merge suggestion. | deterministic |
| `alias_consistency` | For each new Alias rendered by this commit: if multiple canonical_names disagree on the same surface_form, raise `alias_inconsistency`. | deterministic |
| `schema_validation` | The page body parses as valid Markdown; structured Pydantic checks on claim/citation shape. | deterministic |

All 7 nodes run in parallel via `langgraph.Send`. Results joined; findings written.

### Auto-resolve

Mechanical findings with `auto_resolvable=True` can be applied without human approval:
- `broken_wikilink` repaired via alias if a confident match exists → `auto_resolved`
- `hash_mismatch` is **never** auto-resolved (security-significant)
- `alias_inconsistency` is auto-resolved if one canonical_name dominates by >5× usage; else raised as `medium`

All auto-resolutions are ledgered with `actor_kind="aleph_agent"`.

### Findings → Briefs

Non-auto-resolvable findings appear in `BriefsSurface` as `FindingCard`s. If a finding has a `proposed_patch_jsonb`, it also creates an `ApprovalRequest` so the analyst can approve the patch directly.

### Cost

Mechanical pass is cheap (mostly deterministic + 1 judge call per revision). Capability used: `judge` (which maps to `claude-opus-4-7` in prod, `claude-sonnet-4-6` in dev).

---

## 5.5 EditorialReviewer

`packages/aleph-reviewer/src/aleph_reviewer/editorial/workflow.py` uses the Deep Agents harness for the editorial pass. Expensive; runs on schedule or threshold, not on every revision.

### Triggers

- **Scheduled:** every N revisions (default 5), or every M hours (default 24). Configurable per project.
- **Threshold:** when an Aleph-Coverage eval against `aleph-production` profile drops below a project-configured floor, an EditorialReviewer run is triggered automatically (configurable).
- **Manual:** owner triggers via UI.

### Sub-agents

The Deep Agents harness spawns subagents per dimension:

- **`contradiction`** — pairwise scan over WikiClaims and citations. Flag claim pairs that the judge model rates as contradictory with high confidence.
- **`weak_source`** — analyze citations whose source has low credibility signals (no peer review, single self-citation, etc.). Raise `weak_source` with suggested better sources.
- **`narrative_gap`** — for each topic page, check whether the body links to known related pages. Missing high-value wikilinks raise `narrative_gap`.
- **`coverage_gap`** — sample chunks from sources NOT cited by any wiki page; cluster by topic; raise `coverage_gap` with a suggested topic to synthesize.
- **`factual_freshness`** — for time-sensitive claims (judge call to identify), check whether newer sources exist that would update or contradict.

Each sub-agent emits a draft finding with a proposed patch where possible. The orchestrator dedupes (same finding from multiple subagents merges) and ranks. Top-N findings per run land as `ReviewFinding` rows.

### HITL via ApprovalCard

Every editorial finding goes through `ApprovalRequest`. **EditorialReviewer never modifies the wiki directly.** It only proposes via Briefs.

### Cost control

Editorial runs are expensive. Cost cap per run: configurable per project (default $5). If cap is hit mid-run, the workflow finalizes findings collected so far with `partial=true` flag.

### AgentMemory usage

EditorialReviewer uses `AgentMemory` to track:
- `concepts_with_open_findings` — to avoid re-flagging the same issue
- `last_full_pass_at` per dimension
- `rejected_finding_signatures` — analyst-rejected findings stored as signatures so the next run skips them

---

## 5.6 ApprovalRequest workflow

When created:
- `ReviewFinding` with non-auto-resolvable severity → `ApprovalRequest` row + `BriefsSurface` update via SSE
- `SynthesisProposal` from Inc 3 — extended to also produce an `ApprovalRequest` (Inc 3 used `ApprovalDecision` directly; Inc 5 promotes through the request wrapper)

When approved (via `approve` action on `ApprovalCard`):
- `ApprovalDecision` row written (Inc 3 table)
- Target effect applied:
  - `review_finding` → finding status → `approved`; if `proposed_patch_jsonb` present, apply via `WikiService.apply_patch(patch, commit_message="approved finding F#")`
  - `synthesis_proposal` → page status → `approved` (Inc 3 behavior preserved)
  - `wiki_revision` → revision marked approved
  - `hypothesis_update` → new HypothesisVersion committed

When rejected:
- `RejectionFeedback` written with the reason (Inc 1 table)
- `ApprovalRequest.status="rejected"`
- For `synthesis_proposal`: page soft-deleted, RejectionFeedback fed back to wiki agent for next compile
- For `review_finding`: signature hashed and stored in EditorialReviewer's `AgentMemory.rejected_finding_signatures` so future runs skip it
- For `hypothesis_update`: prior version remains current

---

## 5.7 Hypotheses

### Creation

- Analyst via `create_hypothesis` action on `HypothesesSurface` (via `FormCard` in the surface header)
- Assistant via natural conversation: when the assistant detects the analyst is forming a hypothesis ("I think X might be Y"), it offers via inline `FormCard` to capture it formally

### Evidence linking

`HypothesisEvidence` rows added via:
- Drag-and-drop from a WikiClaim, SourcePage, or DocumentChunk reference into a `HypothesisCard`'s evidence panel
- Assistant proposal: when answering a question, if the assistant cites a claim that supports/contradicts an open hypothesis, it offers to attach (via inline `FormCard`)

### Confidence transitions

Confidence is a **structured field** with explicit rules:
- `under_investigation` (initial) → `weakly_supported` (≥2 supporting evidence with weight ≥0.5)
- `weakly_supported` → `well_supported` (≥3 supporting; ≤1 contradicting at weight ≤0.3)
- any state → `contested` (≥1 contradicting at weight ≥0.7 AND ≥1 supporting at weight ≥0.5)
- any state → `refuted` (≥3 contradicting at weight ≥0.7; no supporting at weight ≥0.5)
- analyst-explicit: analyst can override the rule-derived state to any value with a rationale (logged)

Transitions are computed by `aleph_hypotheses.confidence.transition()` and applied via `HypothesisService.update()` which creates a new `HypothesisVersion`.

### Assistant integration

The assistant's composer (Inc 2) prompt is extended to:
- Surface mentions of relevant hypotheses (`[[Hypothesis:H0001]]` in answer body) when a user query touches one
- Offer to update a hypothesis when new evidence is found (via inline FormCard)

`HypothesisService.propose_update()` — assistant proposes; for confidence changes to anything other than `under_investigation→weakly_supported`, an `ApprovalRequest` is required (configurable per project; default = require approval for confidence promotions to `well_supported`, demotions to `refuted`, and any transition to `contested`).

---

## 5.8 Rejection feedback UX

Inc 1 created the `RejectionFeedback` model and the wiki agent's read-on-compile behavior. Inc 5 wires the user-facing path:

- `RejectionDialog.tsx` component: when an analyst clicks Reject on an `ApprovalCard`, a modal asks for a one-line reason before submitting
- The reason flows through: `reject` action → `ApprovalService.reject` → writes `RejectionFeedback` row + `ApprovalDecision` row in one transaction
- For synthesis proposals: rejection schedules a wiki agent re-compile with the feedback context
- For mechanical findings: rejection records the signature so the same finding doesn't re-fire on the next mechanical pass
- For editorial findings: same — signature stored in EditorialReviewer's `AgentMemory`

`feedback_service.list_for_concept(project_id, concept_name)` returns the active rejections for a concept; this is the data the wiki agent reads at compile start (Inc 1 behavior).

---

## 5.9 HTTP API

All under `/v1/projects/{project_id}/`.

### Reviews

- `GET /reviews/runs` — paginated; filter by `kind`, `status`
- `GET /reviews/runs/{id}` — detail with findings
- `POST /reviews/mechanical/trigger` — owner/editor; force a mechanical pass on a revision
- `POST /reviews/editorial/trigger` — owner; force an editorial pass; body specifies `dimensions[]` (subset) and `cost_cap_usd`
- `GET /findings` — paginated; filter by `kind`, `severity`, `status`, `target_page_id`
- `GET /findings/{id}` — detail
- `PATCH /findings/{id}/status` — owner/editor; manual status flip (e.g. mark `superseded`)

### Approvals

- `GET /approval-requests` — paginated; filter by `target_kind`, `status`
- `GET /approval-requests/{id}` — detail with the embedded diff card (Inc 4 catalog component)
- `POST /approval-requests/{id}/approve` — wraps existing Inc 3 endpoint with richer effect application
- `POST /approval-requests/{id}/reject` — body `{reason}`

### Hypotheses

- `POST /hypotheses` — body `{title, statement, initial_evidence?}`; creates Hypothesis + initial HypothesisVersion (`v1`) + optional initial evidence
- `GET /hypotheses` — list; filters
- `GET /hypotheses/{id}` — detail with current version + all versions summary
- `GET /hypotheses/{id}/versions` — full version history
- `POST /hypotheses/{id}/update` — body `{statement?, rationale, evidence_changes[]?}`; computes new confidence per rules + either commits new version directly (if rules permit) or creates an `ApprovalRequest`
- `POST /hypotheses/{id}/evidence` — add a HypothesisEvidence row
- `DELETE /hypotheses/{id}/evidence/{evidence_id}` — soft-delete (kept in version history)

---

## 5.10 Frontend additions

A2UI components (already in Inc 4's catalog) gain real backing:

- `FindingCard` — renders a `ReviewFinding` with severity color, evidence chips that hover-preview, and Approve/Reject if there's a proposed patch
- `ApprovalCard` — Inc 3 already used this for synthesis; Inc 5 adds review-finding and hypothesis-update target kinds
- `HypothesisCard` — renders a `Hypothesis` with current confidence badge, statement, evidence list (supports/contradicts/contextualizes columns), version history button, edit button
- `HypothesesSurface` — list of project hypotheses + create-new affordance + filters by confidence

`RejectionDialog` is a non-A2UI React component (it's a transient modal; could be a `FormCard` but tradeoff favors a fast native modal). Captures one-line reason.

`EvidenceBrowser` — a side rail inside `HypothesisCard` that lets the analyst search wiki claims / sources / chunks to attach as evidence.

---

## 5.11 Tests

### Unit

- `aleph-reviewer/tests/test_mechanical_workflow.py` — each check fires on the right input
- `aleph-reviewer/tests/test_editorial_workflow.py` — each subagent runs; cost cap enforced; partial findings persisted
- `aleph-reviewer/tests/test_auto_resolve.py` — auto_resolvable findings are applied; non-auto are not
- `aleph-hypotheses/tests/test_confidence_rules.py` — every transition rule from §5.7 fires on the right evidence configuration
- `aleph-hypotheses/tests/test_hypothesis_service.py` — create, update, version-history, evidence attach/detach
- `aleph-reviewer/tests/test_approval_workflow.py` — approve/reject for each target_kind triggers the right effect

### Integration (`tests/e2e/`)

- `test_revision_to_mechanical.py` — Wiki commit → MechanicalReviewer runs → findings appear in BriefsSurface within N seconds
- `test_synthesis_rejected_feedback_loop.py` — Reject a synthesis proposal with a reason → next /synthesize with same topic includes the reason → resulting page is meaningfully different (verifiable via prompt-context inspection in trace)
- `test_editorial_pass_e2e.py` — Trigger editorial pass; verify subagents ran (traces); findings written; ApprovalRequests created; cost stayed within cap
- `test_hypothesis_lifecycle.py` — Create hypothesis → attach 3 supporting evidence → confidence transitions to weakly_supported → approval-gated promotion to well_supported → contradicting evidence → contested → resolved by analyst rationale
- `test_handedit_blocks_finding_patch.py` — Finding proposes a patch that overlaps a HandEditMark → patch is rejected at apply time, finding remains open with note "manual resolution required"
- `test_agent_memory_per_project.py` — Agent memory rows for Project A invisible from Project B agent runs
- `test_permission_leakage_inc5.py` — findings/approvals/hypotheses for Project X invisible to Project Y members
- `test_rejected_finding_signature_skip.py` — Reject a finding → trigger another editorial pass → same finding signature is skipped (verified via AgentMemory inspection)

### Eval (`packages/aleph-evals/datasets/inc5_reviewers/`)

- `mechanical_citation_recall.jsonl` — revisions with deliberately broken citations; gate: 95% recall
- `mechanical_broken_link_recall.jsonl` — broken wikilinks; gate: 90% recall
- `editorial_contradiction_recall.jsonl` — known contradiction pairs; gate: 70% recall (these are hard)
- `editorial_coverage_gap_precision.jsonl` — verify reported gaps are real (precision >70%)
- `hypothesis_confidence_correctness.jsonl` — given evidence sets, computed confidence matches expected

Profile-aware in CI. EditorialReviewer eval thresholds are lower under `aleph-dev` (Haiku/Sonnet) than `aleph-production` (Opus); both must meet their floor.

---

## 5.12 Documentation

- `docs/agents/mechanical-reviewer.md`
- `docs/agents/editorial-reviewer.md`
- `docs/review/finding-schema.md`
- `docs/review/approval-workflow.md`
- `docs/review/rejection-feedback-ux.md`
- `docs/domain/hypotheses.md`
- `docs/domain/agent-memory.md`
- `docs/ui/hypotheses-surface.md`
- `docs/ui/briefs-surface-extended.md`
- `docs/implementation-log.md` — Inc 5 entry

---

## 5.13 Acceptance criteria

1. **Mechanical on every revision.** Every wiki commit triggers a MechanicalReviewer run. Findings appear in Briefs.
2. **Auto-resolve works.** Auto-resolvable findings are applied with `actor_kind="aleph_agent"` ledger events.
3. **Editorial pass works.** Manual trigger runs Deep Agents subagents; findings + ApprovalRequests created; cost capped.
4. **Approval flow.** Approve / reject from BriefsSurface produces the right effect for every target_kind (synthesis, finding, revision, hypothesis_update). Ledgered.
5. **Rejection feedback loop.** Reject with reason → next compile for the same concept uses the reason (provable via trace). For findings, signature is remembered.
6. **Hypotheses live.** Create, update (rules + approval), version history, evidence add/remove all functional.
7. **AgentMemory live.** Per-project per-agent scratchpad isolated.
8. **HandEdit respected by finding patches.** A patch that would clobber a HandEditMark is refused.
9. **Permission leakage zero.** All Inc 5 entities project-scoped; 404 cross-project.
10. **Eval gates pass.** All Inc 5 eval datasets at their respective floors under both profiles.
11. **Docs complete.**
12. **No placeholders.**
13. **Implementation log written.**

---

## 5.14 Handoff to Increment 6

Inc 6 lands Datasets and visualization cards. Existing A2UI catalog has `ChartCard` / `TableCard` / `MapCard` / `GraphCard` schemas (Inc 4); Inc 6 makes them bind to real `DatasetVersion`s. Inc 6 also activates the artificialanalysis.ai connector's `dataset_rows` path (deferred from Inc 3).

No schema changes to Inc 5 entities anticipated.

See `docs/superpowers/specs/2026-05-27-inc-6-datasets-visualization-design.md`.
