"""Purpose-built subagents for the assistant Deep Agent (Wave 3).

Each subagent wraps a heavy capability and isolates its large output from the
orchestrator's context, returning only a distilled result via the harness
`task` tool. See `retriever.py` for the exemplar (deep wiki reads).
"""

from aleph_api.subagents.researcher import build_researcher_subagent
from aleph_api.subagents.retriever import build_retriever_subagent
from aleph_api.subagents.reviewer import build_reviewer_subagent
from aleph_api.subagents.viz_builder import build_viz_builder_subagent
from aleph_api.subagents.wiki_builder import build_wiki_builder_subagent

__all__ = [
    "build_researcher_subagent",
    "build_retriever_subagent",
    "build_reviewer_subagent",
    "build_viz_builder_subagent",
    "build_wiki_builder_subagent",
]
