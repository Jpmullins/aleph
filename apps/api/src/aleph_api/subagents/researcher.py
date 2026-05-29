"""The `researcher` subagent — dispatches the AIQ research arc (Wave 3 T3).

Delegating "research X" to this subagent keeps the dispatch logic (and the
fire-and-return chatter) in an isolated context, so the orchestrator's thread
just sees one short line back. The subagent reuses the exact dispatch impl
(`_start_research_impl`, rule #3 — self-calls the tested `/synthesize` route,
never raw DB) and the cost-attributed subagent model (`subagent_model`, rule #5
— its LLM calls write `ModelCall` + `CostLedgerEvent` tagged
`assistant.subagent.researcher`).
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import (
    RunnableConfig,  # noqa: TC002 — runtime import: @tool introspects this annotation to build the args schema
)
from langchain_core.tools import tool


def build_researcher_subagent(*, settings: Any) -> dict[str, Any]:
    """Build the researcher subagent dict (a deepagents `SubAgent`).

    The imports from `aleph_api.copilot_agent` are function-local to avoid a
    circular import (copilot_agent does not import this module at top level; the
    orchestrator builder calls this function at startup).
    """
    from aleph_api.copilot_agent import (  # noqa: PLC0415 — function-local to break the copilot_agent ↔ subagents import cycle
        _start_research_impl,  # pyright: ignore[reportPrivateUsage] — shared dispatch body deliberately reused (DRY); module-private to the api
        subagent_model,
    )

    @tool
    async def start_research(query: str, config: RunnableConfig, depth: str = "shallow") -> str:
        """Kick off background research (AIQ) on a topic; returns immediately.

        `depth` is 'shallow' (fast) or 'deep' (thorough).
        """
        return await _start_research_impl(query, config, depth)

    return {
        "name": "researcher",
        "description": (
            "Kicks off background research on a topic (web search via AIQ) that grows the "
            "wiki. Delegate when the analyst asks to research/look into/synthesize a topic the "
            "wiki doesn't yet cover. Returns immediately; results land in the Wiki/Briefs tabs."
        ),
        "system_prompt": (
            "You are Aleph's research dispatcher. Call start_research with a focused query "
            "(depth 'shallow' unless the analyst asked for a thorough/deep dive), then return ONE "
            "short line naming the topic and stating results will appear in the Wiki/Briefs tabs. "
            "Do NOT wait for completion."
        ),
        "tools": [start_research],
        "model": subagent_model(settings, "researcher"),
    }
