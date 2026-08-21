"""The `retriever` subagent — exemplar heavy subagent (Wave 3 T2).

Wraps the full wiki-first retrieval pipeline (`WikiFirstRetrievalRouter`) so the
large composed body lives in this subagent's isolated context and only a
distilled, cited answer returns to the orchestrator's chat thread. The subagent
reuses the exact retrieval impl (`_read_wiki_impl`, rule #3 — goes through the
router/services, never raw DB) and the cost-attributed subagent model
(`subagent_model`, rule #5 — its LLM calls write `ModelCall` +
`CostLedgerEvent` tagged `assistant.subagent.retriever`).
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import (
    RunnableConfig,
)
from langchain_core.tools import tool


def build_retriever_subagent(*, settings: Any) -> dict[str, Any]:
    """Build the retriever subagent dict (a deepagents `SubAgent`).

    The imports from `aleph_api.copilot_agent` are function-local to avoid a
    circular import (copilot_agent does not import this module at top level; the
    orchestrator builder calls this function at startup).
    """
    from aleph_api.agent_middleware import AlephAgentMiddleware
    from aleph_api.copilot_agent import (
        _read_wiki_impl,  # pyright: ignore[reportPrivateUsage] — shared retrieval body deliberately reused (DRY); module-private to the api
        subagent_model,
    )

    @tool
    async def deep_read(query: str, config: RunnableConfig) -> str:
        """Run the full wiki-first retrieval pipeline and return a cited answer."""
        return await _read_wiki_impl(query, config)

    return {
        "name": "retriever",
        "description": (
            "Reads the project's wiki in depth (page selection, wikilink expansion, "
            "answer composition, intra-source descent) and returns a cited, composed "
            "answer. Delegate any substantive question that needs grounding in the wiki."
        ),
        "system_prompt": (
            "You are Aleph's retrieval specialist. Call deep_read once with a focused "
            "query, then return ONLY a concise cited answer using [[Page Title]] markers "
            "plus a one-line coverage note. Never dump raw page bodies."
        ),
        "tools": [deep_read],
        # The same tool guard the orchestrator carries. deepagents lets a
        # subagent spec override the parent's middleware rather than extend it,
        # so "the orchestrator has it" is not "the subagents have it" —
        # scripts/check-agent-middleware.sh asserts all six do.
        "middleware": [AlephAgentMiddleware()],
        "model": subagent_model(settings, "retriever"),
    }
