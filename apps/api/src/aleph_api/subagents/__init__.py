"""Purpose-built subagents for the assistant Deep Agent.

Each subagent wraps a heavy capability and isolates its large output from the
orchestrator's context, returning only a distilled result. See `retriever.py`
for the exemplar (deep wiki reads).

**Two ways to reach one, and they are gated in different places.**

*Synchronously*, through the harness `task` tool: the supervisor blocks until
the subagent finishes. This is the default and needs no registry.

*Asynchronously*, through `AsyncSubAgentMiddleware`'s `start_async_task`, which
returns a ticket and lets the supervisor keep talking — `docs/decisions.md` D17.
That path names a subagent by string (`graph_id` / `assistant_id`), so there has
to be something that says which strings are real. `DELEGATABLE` is that.

**`DELEGATABLE` is a name registry, NOT an approval gate**, and the distinction
matters because getting it wrong in either direction is a real bug. Async
delegation happens through an ordinary supervisor tool call, so it is subject to
`interrupt_on` like any other — the human-in-the-loop gate belongs there. What
this list prevents is a supervisor inventing a graph id and getting a run that
can never resolve, and a route interpolating an arbitrary string into a job
name. Contrast `interpreter.PTC_ALLOWLIST`, which IS a safety gate, because PTC
bypasses `interrupt_on` entirely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from aleph_api.subagents.researcher import build_researcher_subagent
from aleph_api.subagents.retriever import build_retriever_subagent
from aleph_api.subagents.reviewer import build_reviewer_subagent
from aleph_api.subagents.viz_builder import build_viz_builder_subagent
from aleph_api.subagents.wiki_builder import build_wiki_builder_subagent

if TYPE_CHECKING:
    from collections.abc import Callable

#: Subagent name -> its builder. The keys are the `graph_id` values an
#: `AsyncSubAgent` spec may name and the route will accept.
DELEGATABLE: Final[dict[str, Callable[..., dict[str, Any]]]] = {
    "retriever": build_retriever_subagent,
    "researcher": build_researcher_subagent,
    "wiki_builder": build_wiki_builder_subagent,
    "viz_builder": build_viz_builder_subagent,
    "reviewer": build_reviewer_subagent,
}

#: The accepted `assistant_id` values, for the route's validation and its error.
DELEGATABLE_SUBAGENTS: Final[frozenset[str]] = frozenset(DELEGATABLE)


def build_subagent(name: str, *, settings: Any) -> dict[str, Any]:
    """Build one delegatable subagent by name, or refuse.

    The worker uses this to reconstruct the subagent a delegated run names. It
    raises rather than returning None: a run that named an unknown subagent
    cannot proceed, and failing here puts the reason in the run's `error_text`
    instead of leaving it pending forever.
    """
    builder = DELEGATABLE.get(name)
    if builder is None:
        msg = f"unknown subagent {name!r}; delegatable: {', '.join(sorted(DELEGATABLE))}"
        raise KeyError(msg)
    return builder(settings=settings)


__all__ = [
    "DELEGATABLE",
    "DELEGATABLE_SUBAGENTS",
    "build_researcher_subagent",
    "build_retriever_subagent",
    "build_reviewer_subagent",
    "build_subagent",
    "build_viz_builder_subagent",
    "build_wiki_builder_subagent",
]
