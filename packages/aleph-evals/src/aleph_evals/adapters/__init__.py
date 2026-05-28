"""AIQ benchmark adapters."""

from aleph_evals.adapters.deepresearch_bench import (
    DeepResearchBenchAdapter,
)
from aleph_evals.adapters.freshqa import FreshQAAdapter

__all__ = ["DeepResearchBenchAdapter", "FreshQAAdapter"]
