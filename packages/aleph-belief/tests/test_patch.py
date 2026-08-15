"""Envelope validation — everything decidable without the graph."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aleph_belief.patch import (
    PATCH_SCHEMA,
    BeliefPatch,
    PatchOperation,
    PatchSeverity,
    PatchStatus,
    validate_envelope,
)
from aleph_belief.trust import TrustTier


def make_patch(**overrides: Any) -> BeliefPatch:
    base: dict[str, Any] = {
        "id": uuid4(),
        "project_id": uuid4(),
        "operation": PatchOperation.ACCEPT_MATCH,
        "profile_hash": "sha256:profile",
        "graph_hash": "sha256:graph",
        "target": {"source_belief_id": str(uuid4()), "target_belief_id": str(uuid4())},
        "evidence_refs": ("chunk:abc#0-120",),
        "trust": TrustTier.EARNED,
        "reason": "identical normalized text and shared evidence",
        "author": "matcher:lexical",
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return BeliefPatch.model_validate(base)


def errors(patch: BeliefPatch) -> list[str]:
    return [i.message for i in validate_envelope(patch).issues if i.severity is PatchSeverity.ERROR]


def test_wellformed_patch_validates() -> None:
    result = validate_envelope(make_patch())
    assert result.valid
    assert result.issues == ()


def test_patch_is_immutable() -> None:
    patch = make_patch()
    with pytest.raises(ValidationError):
        patch.status = PatchStatus.APPLIED  # type: ignore[misc]


def test_defaults_to_proposed() -> None:
    """A patch is a proposal until something applies it."""
    assert make_patch().status is PatchStatus.PROPOSED


def test_unknown_schema_rejected() -> None:
    with pytest.raises(ValidationError):
        make_patch(schema="graphify_ontology_patch_v1")


def test_staleness_guard_hashes_required() -> None:
    assert "profile_hash is empty" in errors(make_patch(profile_hash="   "))
    assert "graph_hash is empty" in errors(make_patch(graph_hash=""))


def test_reason_required() -> None:
    assert any("reason is empty" in m for m in errors(make_patch(reason="  ")))


def test_empty_target_rejected() -> None:
    assert any("target is empty" in m for m in errors(make_patch(target={})))


@pytest.mark.parametrize(
    "operation",
    [
        PatchOperation.ACCEPT_MATCH,
        PatchOperation.CREATE_CANONICAL,
        PatchOperation.MERGE_ALIAS,
        PatchOperation.ADD_RELATION,
        PatchOperation.SUPERSEDE_BELIEF,
    ],
)
def test_asserting_operations_require_evidence(operation: PatchOperation) -> None:
    msgs = errors(make_patch(operation=operation, evidence_refs=()))
    assert any("requires at least one evidence ref" in m for m in msgs)


@pytest.mark.parametrize(
    "operation",
    [PatchOperation.REJECT_MATCH, PatchOperation.REJECT_RELATION],
)
def test_rejecting_operations_may_stand_on_reasoning(operation: PatchOperation) -> None:
    """Declining a proposal is a judgment about the graph, not a claim about the world."""
    assert validate_envelope(make_patch(operation=operation, evidence_refs=())).valid


def test_unverified_trust_warns_but_does_not_invalidate() -> None:
    result = validate_envelope(make_patch(trust=TrustTier.UNVERIFIED))
    assert result.valid
    assert [i.severity for i in result.issues] == [PatchSeverity.WARNING]


def test_schema_constant_is_aleph_owned() -> None:
    """We adapted graphify's contract; we did not inherit its identifier."""
    assert PATCH_SCHEMA == "aleph_belief_patch_v1"
