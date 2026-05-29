"""Purpose-built subagents for the assistant Deep Agent (Wave 3).

Each subagent wraps a heavy capability and isolates its large output from the
orchestrator's context, returning only a distilled result via the harness
`task` tool. See `retriever.py` for the exemplar (deep wiki reads).
"""

from aleph_api.subagents.retriever import build_retriever_subagent

__all__ = ["build_retriever_subagent"]
