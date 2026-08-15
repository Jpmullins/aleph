"""Aleph web of belief — the patch contract and trust lattice.

Beliefs are revised by proposing immutable, validated patches rather than by
mutating derived state in place. See :mod:`aleph_belief.patch` for the contract
and :mod:`aleph_belief.trust` for the provenance lattice that decides which
writer may overwrite which belief.

Both are ported from graphify (MIT, Copyright (c) 2026 Safi Shamsi); see
``NOTICE`` in this package for attribution.
"""

from aleph_belief.patch import (
    PATCH_SCHEMA,
    BeliefPatch,
    PatchIssue,
    PatchOperation,
    PatchSeverity,
    PatchStatus,
    PatchValidation,
    validate_envelope,
)
from aleph_belief.trust import TrustTier, outranks, violates_trust_tier

__all__ = [
    "PATCH_SCHEMA",
    "BeliefPatch",
    "PatchIssue",
    "PatchOperation",
    "PatchSeverity",
    "PatchStatus",
    "PatchValidation",
    "TrustTier",
    "outranks",
    "validate_envelope",
    "violates_trust_tier",
]
