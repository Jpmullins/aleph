"""The Agent Protocol status vocabulary, and the map onto Aleph's own.

Aleph hosts the Agent Protocol so `deepagents`' `AsyncSubAgentMiddleware` can
drive delegated work against Aleph's own queue instead of a LangGraph
deployment. `docs/decisions.md` D17 records why: the middleware talks to "any
server that implements the Agent Protocol", and the surface it actually uses is
five routes. Hosting them buys the five supervisor tools, the `async_tasks`
state channel that survives context compaction, and the prompt rules that stop a
supervisor polling itself back into blocking — none of which Aleph then writes.

**Two vocabularies, and they are not the same size.** The middleware compares
`run["status"]` against exactly six strings; Aleph's `agent_runs.status` column
holds five, and two of them mean the same thing. Neither set is a superset of
the other, so the map is explicit and total: an unmapped Aleph status returns
`running` rather than raising, because a delegated task whose status cannot be
read is still in flight and reporting `error` would tell the supervisor to give
up on work that is proceeding.

`succeeded` and `completed` both appear in the live table and both mean the run
finished well. That duplication is pre-existing and is NOT resolved here —
mapping both is honest; silently picking one would drop the other's runs.
"""

from __future__ import annotations

from typing import Final, Literal

#: What `AsyncSubAgentMiddleware` compares against. Anything else is unreadable
#: to it: `_build_check_result` keys the whole result off `run["status"]`, and an
#: unrecognised value falls through every branch, so the supervisor is told the
#: task exists and never that it finished.
ProtocolStatus = Literal[
    "pending", "running", "success", "error", "interrupted", "timeout", "cancelled"
]

PROTOCOL_STATUSES: Final[frozenset[str]] = frozenset(
    {"pending", "running", "success", "error", "interrupted", "timeout", "cancelled"}
)

#: Terminal on the protocol side. The middleware caches these and stops asking
#: the server, so a status that lands here is the last word on that run.
TERMINAL_PROTOCOL_STATUSES: Final[frozenset[str]] = frozenset({"success", "error", "cancelled"})

#: Aleph `agent_runs.status` -> Agent Protocol status.
_MAP: Final[dict[str, ProtocolStatus]] = {
    "pending": "pending",
    "running": "running",
    "succeeded": "success",
    "completed": "success",
    "failed": "error",
    "cancelled": "cancelled",
}


def to_protocol_status(aleph_status: str) -> ProtocolStatus:
    """Map an Aleph run status onto the protocol's.

    Falls back to `running`, not `error`. An unknown status means Aleph grew a
    state this map has not been taught; the run is still live as far as anyone
    knows, and telling the supervisor `error` would abandon work in flight.
    """
    return _MAP.get(aleph_status, "running")


def is_terminal(protocol_status: str) -> bool:
    return protocol_status in TERMINAL_PROTOCOL_STATUSES
