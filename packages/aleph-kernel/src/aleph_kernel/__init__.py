"""Aleph kernel — spatiotemporal composability for a self-improving harness.

An implementation of the model in *A Programming Paradigm for Spatiotemporal
Composability* (Shi, Zhang, Cui — Peking University / DeepSeek-AI), §5.1
Algorithms 1-6, in Python. Written from the paper rather than ported from
`cordis`, its TypeScript reference implementation, so that divergences are
deliberate. Where they exist they are documented at the site.

Two things the kernel provides that the reference implementation does not:

* **The support set is computed** (Definition 67, Lemma 68/70). `blast_radius`
  answers "what stops working if this goes away" as a pure function, before
  anything is torn down. This is the guardrail.
* **A capability cannot activate without proving it works.** `probe` is a
  required field; activation runs it against the live system and rolls back on
  failure. This is the structural answer to a codebase whose dominant defect was
  write paths shipped with no read path.
"""

from aleph_kernel.context import Context, Store
from aleph_kernel.effects import EffectScope, Inverse
from aleph_kernel.errors import (
    CyclicDependency,
    DependentsWouldBreak,
    InactiveAccess,
    KernelError,
    MissingProvider,
    ProbeFailed,
    ProtectedCapability,
    UndeclaredAccess,
)
from aleph_kernel.kernel import Kernel, PluginId, State
from aleph_kernel.spec import CapabilitySpec, Probe, ProbeResult, Setup, ok, problem
from aleph_kernel.support import BlastRadius, dependent_closure, support_set, topological_order

__all__ = [
    "BlastRadius",
    "CapabilitySpec",
    "Context",
    "CyclicDependency",
    "DependentsWouldBreak",
    "EffectScope",
    "InactiveAccess",
    "Inverse",
    "Kernel",
    "KernelError",
    "MissingProvider",
    "PluginId",
    "Probe",
    "ProbeFailed",
    "ProbeResult",
    "ProtectedCapability",
    "Setup",
    "State",
    "Store",
    "UndeclaredAccess",
    "dependent_closure",
    "ok",
    "problem",
    "support_set",
    "topological_order",
]
