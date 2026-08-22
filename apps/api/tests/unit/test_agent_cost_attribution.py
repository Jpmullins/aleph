"""No model call made during a chat turn may be written unattributed.

Found by measurement, not by reading. `scripts/_acceptance/agent_turn_probe.py`
drove three real turns and the ledger came back with 13 attributed
`assistant.turn` rows sitting next to 9 orphans — priced correctly, belonging to
nothing:

    assistant.corpus_search.query_embed | gateway | attributed=f | 3
    assistant.page_selection            | gateway | attributed=f | 3
    assistant.compose                   | gateway | attributed=f | 3

All nine came from a single tool. `search_knowledge` calls
`WikiFirstRetrievalRouter.retrieve(..., agent_run_id=None)`, and the router
makes three further model calls of its own. The agent middleware never sees
them: it wraps the ORCHESTRATOR's model call, and these happen one layer down,
inside a tool.

`None` here is not a null, it is a claim — that this call belongs to no run —
and it is false for every call made during a turn. It is also invisible: the row
is written, the cost is right, the total is right, and only a per-run breakdown
ever shows the gap. That is the same shape as the original defect, where
`model_calls.agent_run_id` was NULL for the whole life of the column.
"""

from __future__ import annotations

import ast
import pathlib

AGENT = pathlib.Path(__file__).resolve().parents[3] / "api/src/aleph_api/copilot_agent.py"


def test_no_tool_passes_a_null_run_id() -> None:
    """An AST check, because a grep cannot tell a call from a docstring.

    Scoped to `copilot_agent.py` and only to it. Elsewhere `agent_run_id=None`
    is correct and stays: `routes/assistant.py` serves the non-agent chat path
    and `routes/smoketest.py` is a diagnostic — neither happens during a turn,
    so neither has a run to name. A sweep over the whole tree would be red for
    two honest reasons and get suppressed.
    """
    tree = ast.parse(AGENT.read_text(), filename=str(AGENT))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "agent_run_id":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value is None:
                func = ast.unparse(node.func)
                offenders.append(f"{func} at line {node.lineno}")
    assert not offenders, (
        "these calls claim to belong to no agent run, during a turn that has one: "
        + ", ".join(offenders)
    )


def test_the_search_tool_threads_the_run_id() -> None:
    """The positive half: it passes the run id, from the documented reader.

    Asserted separately from the sweep above because deleting the argument
    entirely would satisfy the sweep — `retrieve` has a default — while
    reintroducing the identical defect.
    """
    tree = ast.parse(AGENT.read_text(), filename=str(AGENT))
    threaded = [
        ast.unparse(kw.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "agent_run_id"
    ]
    assert "run_id_from_config(config)" in threaded, threaded
