"""The `analyst` subagent — Analysis of Competing Hypotheses (Wave 3 T6).

Delegating "create/list/weigh hypotheses" to this subagent keeps the
hypothesis-reasoning chatter in an isolated context, so the orchestrator's
thread just sees one short result (or a HypothesisCard render instruction)
back. Three tools, each wrapping the exact shared impl from `copilot_agent`
(DRY) so the create/add-evidence paths still write their Action Ledger events
(rule #4) and never touch the DB credentials directly. Its LLM calls use the
cost-attributed subagent model (`subagent_model`, rule #5 — they write
`ModelCall` + `CostLedgerEvent` tagged `assistant.subagent.analyst`).
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import (
    RunnableConfig,
)
from langchain_core.tools import tool


def build_analyst_subagent(*, settings: Any) -> dict[str, Any]:
    """Build the analyst subagent dict (a deepagents `SubAgent`).

    The imports from `aleph_api.copilot_agent` are function-local to avoid a
    circular import (copilot_agent does not import this module at top level; the
    orchestrator builder calls this function at startup).
    """
    from aleph_api.agent_middleware import AlephAgentMiddleware
    from aleph_api.copilot_agent import (
        _add_hypothesis_evidence_impl,  # pyright: ignore[reportPrivateUsage] — shared body deliberately reused (DRY); module-private to the api
        _create_hypothesis_impl,  # pyright: ignore[reportPrivateUsage] — shared body deliberately reused (DRY); module-private to the api
        _list_hypotheses_impl,  # pyright: ignore[reportPrivateUsage] — shared body deliberately reused (DRY); module-private to the api
        _runtime,
        subagent_model,
    )

    @tool
    async def list_hypotheses(config: RunnableConfig) -> str:
        """List the current project's hypotheses with their confidence.

        Use this to recall what competing explanations the analyst is already
        tracking before proposing or creating a new one.
        """
        return await _list_hypotheses_impl(config)

    @tool
    async def create_hypothesis(title: str, statement: str, config: RunnableConfig) -> str:
        """Create a new hypothesis (a competing explanation) for the project.

        Confirm the statement with the analyst before calling this. `title` is a
        short label; `statement` is the falsifiable claim. After creating, render
        a HypothesisCard so the analyst can track and weigh evidence against it.
        """
        return await _create_hypothesis_impl(title, statement, config)

    @tool
    async def add_hypothesis_evidence(
        hypothesis_id: str,
        stance: str,
        evidence_kind: str,
        target_id: str,
        config: RunnableConfig,
        note: str = "",
        weight: float = 1.0,
    ) -> str:
        """Attach a piece of evidence to a hypothesis.

        `stance` is one of supports / contradicts / contextualizes.
        `evidence_kind` is one of claim / source_page / chunk / finding /
        other_hypothesis. `target_id` is the UUID of the referenced entity.
        Adding evidence may shift the hypothesis's confidence; re-render its
        HypothesisCard afterward.
        """
        return await _add_hypothesis_evidence_impl(
            hypothesis_id, stance, evidence_kind, target_id, config, note, weight
        )

    return {
        "name": "analyst",
        "description": (
            "Structures analytic hypotheses and weighs evidence (Analysis of "
            "Competing Hypotheses). Delegate when the analyst is reasoning about "
            "competing explanations, hypotheses, or evidence."
        ),
        "system_prompt": (
            "You are Aleph's analyst. List or create hypotheses and add evidence "
            "as asked; for a created/updated hypothesis return a HypothesisCard "
            "render instruction (hypothesis_id, title, confidence, evidence_count). "
            "Confirm the statement with the analyst before creating. Return concise "
            "results, not raw dumps."
        ),
        "tools": [list_hypotheses, create_hypothesis, add_hypothesis_evidence],
        # The same tool guard the orchestrator carries. deepagents lets a
        # subagent spec override the parent's middleware rather than extend it,
        # so "the orchestrator has it" is not "the subagents have it" —
        # scripts/check-agent-middleware.sh asserts all six do.
        "middleware": [AlephAgentMiddleware(session_maker=_runtime.get("session_maker"))],
        "model": subagent_model(settings, "analyst"),
    }
