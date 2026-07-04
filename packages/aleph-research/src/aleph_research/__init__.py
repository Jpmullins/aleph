"""Aleph native research loop (WP-3).

Plan/search/ingest/reflect/compose over the typed connector suite +
scholar path, landing reports through the wiki ``SynthesisWorkflow``.
"""

from aleph_research.dispatch import DEPTH_AGENT_KINDS, StartedResearch, dispatch_research
from aleph_research.research_workflow import (
    Candidate,
    IngestedSource,
    ResearchLimits,
    ResearchState,
    ResearchWorkflow,
    Subquery,
)
from aleph_research.tools import (
    RESEARCH_CONNECTOR_FACTORIES,
    RESEARCH_CONNECTOR_KINDS,
    BoundTool,
    resolve_bound_tools,
    resolve_enabled_kinds,
)

__all__ = [
    "DEPTH_AGENT_KINDS",
    "RESEARCH_CONNECTOR_FACTORIES",
    "RESEARCH_CONNECTOR_KINDS",
    "BoundTool",
    "Candidate",
    "IngestedSource",
    "ResearchLimits",
    "ResearchState",
    "ResearchWorkflow",
    "StartedResearch",
    "Subquery",
    "dispatch_research",
    "resolve_bound_tools",
    "resolve_enabled_kinds",
]
