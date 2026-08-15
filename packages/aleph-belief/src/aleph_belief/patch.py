"""The belief patch — the only way a decision enters the web of belief.

Ported from graphify (MIT, Copyright (c) 2026 Safi Shamsi) —
``src/ontology-patch.ts`` ``OntologyPatch`` / ``OntologyPatchValidationResult``.

The inversion this encodes: **derived state is regenerated, never hand-edited.**
The belief graph is a pure function of (sources x extraction x patch log), so it
can be rebuilt, replayed, and audited. Every human approval and every LLM
curation verdict is an immutable ``BeliefPatch`` rather than an in-place
mutation, which is what makes an LLM curator safe to run — it proposes, it does
not overwrite.

``profile_hash`` and ``graph_hash`` are the staleness guard. A patch records the
state it was computed against, so a decision made against an older graph is
detected at apply time instead of silently applying to a graph that has since
moved. Aleph's prior approval path had no such guard.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aleph_belief.trust import TrustTier

__all__ = [
    "PATCH_SCHEMA",
    "BeliefPatch",
    "PatchIssue",
    "PatchOperation",
    "PatchSeverity",
    "PatchStatus",
    "PatchValidation",
    "validate_envelope",
]

PATCH_SCHEMA = "aleph_belief_patch_v1"


class PatchOperation(StrEnum):
    """Operation vocabulary, carried over from graphify's ontology patch.

    Names are kept identical to the source where the semantics are identical, so
    the lineage stays traceable; ``deprecate``/``supersede`` are renamed off
    ``*_entity`` because Aleph's node is a belief, not an ontology entity.
    """

    ACCEPT_MATCH = "accept_match"
    REJECT_MATCH = "reject_match"
    CREATE_CANONICAL = "create_canonical"
    MERGE_ALIAS = "merge_alias"
    SET_STATUS = "set_status"
    ADD_RELATION = "add_relation"
    REJECT_RELATION = "reject_relation"
    DEPRECATE_BELIEF = "deprecate_belief"
    SUPERSEDE_BELIEF = "supersede_belief"


class PatchStatus(StrEnum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    REJECTED = "rejected"


class PatchSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


#: Operations that assert something new about the world and therefore require at
#: least one evidence ref. ``reject_*`` operations are judgments about the
#: graph's own proposals and are allowed to stand on reasoning alone.
_REQUIRES_EVIDENCE: frozenset[PatchOperation] = frozenset(
    {
        PatchOperation.ACCEPT_MATCH,
        PatchOperation.CREATE_CANONICAL,
        PatchOperation.MERGE_ALIAS,
        PatchOperation.ADD_RELATION,
        PatchOperation.SUPERSEDE_BELIEF,
    }
)


class PatchIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    severity: PatchSeverity
    message: str


class PatchValidation(BaseModel):
    """Outcome of validating a patch. ``valid`` is false iff any issue is an error."""

    model_config = ConfigDict(frozen=True)

    patch_id: UUID | None
    valid: bool
    issues: tuple[PatchIssue, ...] = ()

    @classmethod
    def from_issues(cls, patch_id: UUID | None, issues: list[PatchIssue]) -> Self:
        valid = not any(i.severity is PatchSeverity.ERROR for i in issues)
        return cls(patch_id=patch_id, valid=valid, issues=tuple(issues))


class BeliefPatch(BaseModel):
    """An immutable, validated decision about the belief graph.

    ``target`` is operation-shaped and deliberately open: each operation names
    the belief/relation ids it acts on. It is validated against the graph at
    apply time, not here — see ``validate_envelope`` for what this module can
    and cannot check on its own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_name: str = Field(default=PATCH_SCHEMA, alias="schema")
    id: UUID
    project_id: UUID
    operation: PatchOperation
    status: PatchStatus = PatchStatus.PROPOSED

    #: State the decision was computed against. Mismatch at apply time means the
    #: patch is stale and must be recomputed rather than applied.
    profile_hash: str
    graph_hash: str

    target: dict[str, Any]
    evidence_refs: tuple[str, ...] = ()
    #: Trust tier of the writer. An ``ASSERTED`` patch cannot overwrite a belief
    #: the corpus ``EARNED`` — enforced at apply time via
    #: ``trust.violates_trust_tier``.
    trust: TrustTier
    reason: str
    author: str
    created_at: datetime

    @model_validator(mode="after")
    def _check_schema_name(self) -> Self:
        if self.schema_name != PATCH_SCHEMA:
            msg = f"unknown patch schema {self.schema_name!r}, expected {PATCH_SCHEMA!r}"
            raise ValueError(msg)
        return self


def validate_envelope(patch: BeliefPatch) -> PatchValidation:
    """Validate everything decidable from the patch alone.

    This is the structural half of validation and is a pure function: it checks
    the envelope's internal consistency (evidence present where the operation
    asserts something, hashes non-empty, reason given, trust rankable).

    It deliberately does **not** check graph preconditions — that the target ids
    exist, that a relation's endpoints are legal under the profile, or that the
    hashes still match the live graph. Those require graph state and belong to
    the apply path. A patch passing here is well-formed, not yet applicable.
    """
    issues: list[PatchIssue] = []

    if not patch.profile_hash.strip():
        issues.append(PatchIssue(severity=PatchSeverity.ERROR, message="profile_hash is empty"))
    if not patch.graph_hash.strip():
        issues.append(PatchIssue(severity=PatchSeverity.ERROR, message="graph_hash is empty"))
    if not patch.reason.strip():
        issues.append(
            PatchIssue(
                severity=PatchSeverity.ERROR,
                message="reason is empty — a decision must record why it was made",
            )
        )
    if not patch.target:
        issues.append(
            PatchIssue(
                severity=PatchSeverity.ERROR,
                message=f"target is empty for operation {patch.operation}",
            )
        )
    if patch.operation in _REQUIRES_EVIDENCE and not patch.evidence_refs:
        issues.append(
            PatchIssue(
                severity=PatchSeverity.ERROR,
                message=(
                    f"operation {patch.operation} asserts a belief and requires "
                    "at least one evidence ref"
                ),
            )
        )
    if patch.trust is TrustTier.UNVERIFIED:
        issues.append(
            PatchIssue(
                severity=PatchSeverity.WARNING,
                message="unverified trust tier — this patch cannot overwrite any existing belief",
            )
        )

    return PatchValidation.from_issues(patch.id, issues)
