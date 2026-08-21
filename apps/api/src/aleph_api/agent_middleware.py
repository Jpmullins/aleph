"""Aleph's own agent middleware: a tool failure is a message, not a dead run.

The assistant has 27 tools. Before this, any one of them throwing — a permission
check, a database hiccup, a missing dictionary key, a 404 for a page that does
not exist — killed the whole conversation on the spot. That is not how agents
are supposed to work: a normal agent reads the error, says "that did not work,
let me try another way", and keeps going. Ours could not, because it never got
to see the error.

The cause is LangChain's default, and it is a deliberate one: `ToolNode` wired
with no `handle_tool_errors` re-raises anything that is not a schema-validation
error. That is correct for a library and wrong for this agent.

The defect is structural rather than local. Six of the eleven orchestrator tools
contain no `try:` at all, and *every* tool — guarded or not — calls
`_project_id_from_config` OUTSIDE its try block, which reaches
`require_project_access` and can raise `PermissionDenied`. So the tools that
looked defended were defended against the wrong thing.

**What this must not become.** Swallowing `PermissionDenied` and handing the
model a friendly sentence must not turn into a way for the agent to keep probing
a project it has no access to. Authorization failures are still refusals — the
model is told it may not, not told to try again — and they are logged
distinctly, so a run that spends its turn bouncing off a permission boundary is
visible rather than merely quiet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from aleph_core.errors import AlephError, NotFound, PermissionDenied

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ToolCallRequest
    from langgraph.types import Command

_log = structlog.get_logger(__name__)

#: Per-exception guidance, so the message the model reads suggests a next move
#: rather than only reporting a stop. A tool error the model cannot act on is a
#: slower way to end the turn.
_ADVICE: dict[type[BaseException], str] = {
    PermissionDenied: (
        "You do not have access to that project. Do not retry this call and do "
        "not try a different id — ask the analyst which project they mean."
    ),
    NotFound: (
        "That id or slug does not exist. Call search_wiki first and use the "
        "page_id it returns rather than constructing one."
    ),
}


def _advice_for(exc: BaseException) -> str:
    for kind, advice in _ADVICE.items():
        if isinstance(exc, kind):
            return advice
    return "Try a different approach, or tell the analyst what you were unable to do."


def describe_tool_failure(tool_name: str, exc: BaseException) -> str:
    """One line the model can act on, with the tool named.

    The tool name is in the text as well as in the `ToolMessage` envelope
    because the model reads the text; a message that says only "an error
    occurred" costs a turn to diagnose.
    """
    reason = str(exc).strip() or exc.__class__.__name__
    return f"{tool_name} failed: {exc.__class__.__name__}: {reason}. {_advice_for(exc)}"


class AlephAgentMiddleware(AgentMiddleware):
    """Wraps every tool call so an exception becomes a readable tool result."""

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_call = request.tool_call
        name = str(tool_call.get("name", "a tool"))
        call_id = str(tool_call.get("id") or "")
        try:
            return await handler(request)
        except PermissionDenied as exc:
            # Logged at its own level and under its own event, because "the
            # agent kept hitting a project it may not touch" is a security
            # signal and must not be filed with database hiccups.
            _log.warning(
                "agent.tool.permission_denied",
                tool=name,
                tool_call_id=call_id,
                error=str(exc),
            )
            return ToolMessage(
                content=describe_tool_failure(name, exc),
                tool_call_id=call_id,
                name=name,
                status="error",
            )
        except Exception as exc:
            level = "warning" if isinstance(exc, AlephError) else "exception"
            getattr(_log, level)(
                "agent.tool.failed",
                tool=name,
                tool_call_id=call_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return ToolMessage(
                content=describe_tool_failure(name, exc),
                tool_call_id=call_id,
                name=name,
                status="error",
            )
