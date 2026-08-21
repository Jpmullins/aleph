"""A tool that throws must not kill the conversation.

The assistant has 27 tools, and any one of them raising ended the turn: LangChain's
`ToolNode`, wired with no `handle_tool_errors`, re-raises anything that is not a
schema-validation error. A normal agent reads the error and tries another way;
ours never got to see it.

The defect was structural, not local. Six of the eleven orchestrator tools have no
`try:` at all — and every tool, guarded or not, resolves its project scope OUTSIDE
its own try block, so the ones that looked defended were defended against the wrong
thing. That is why the fix is one middleware rather than 27 try blocks, and why
these tests drive the middleware over an arbitrary raising tool rather than over
one hand-picked tool that happens to be unguarded today.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from aleph_api.agent_middleware import AlephAgentMiddleware, describe_tool_failure
from aleph_core.errors import NotFound, PermissionDenied


def _request(name: str = "search_wiki") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": {}, "id": "call-1", "type": "tool_call"},
        tool=None,
        state={},
        runtime=None,  # type: ignore[arg-type]
    )


async def _run_with(exc: BaseException | None, *, name: str = "search_wiki") -> Any:
    middleware = AlephAgentMiddleware()

    async def handler(_req: ToolCallRequest) -> ToolMessage:
        if exc is not None:
            raise exc
        return ToolMessage(content="fine", tool_call_id="call-1", name=name)

    return await middleware.awrap_tool_call(_request(name), handler)


async def test_permission_denied_becomes_tool_message() -> None:
    """The specific exception every tool could raise before its own try block."""
    result = await _run_with(PermissionDenied("no access to project 7"))
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.tool_call_id == "call-1"
    assert "PermissionDenied" in str(result.content)


async def test_a_permission_failure_does_not_invite_a_retry() -> None:
    """Turning a refusal into a friendly sentence must not become a way to keep
    probing a project the caller may not touch. The model is told to stop and
    ask, not to try a different id."""
    result = await _run_with(PermissionDenied("nope"))
    text = str(result.content).lower()
    assert "do not retry" in text
    assert "do not try a different id" in text


async def test_a_not_found_points_at_the_tool_that_returns_real_ids() -> None:
    result = await _run_with(NotFound("no page 'foo'"), name="open_page")
    assert "search_wiki" in str(result.content)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("the database went away"),
        KeyError("chunk_ids"),
        ValueError("bad argument"),
        TimeoutError("upstream took too long"),
    ],
)
async def test_any_exception_class_becomes_a_tool_message(exc: BaseException) -> None:
    """Not a catalogue of known failures: an unknown one must be survivable too.

    The four above are the shapes actually seen — a permission check, a database
    hiccup, a missing dictionary key, an upstream timeout.
    """
    result = await _run_with(exc)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert type(exc).__name__ in str(result.content)


async def test_a_working_tool_is_returned_untouched() -> None:
    """A guard that changes successful results is a rewrite, not a guard."""
    result = await _run_with(None)
    assert isinstance(result, ToolMessage)
    assert result.status != "error"
    assert str(result.content) == "fine"


async def test_the_message_names_the_tool() -> None:
    """ "An error occurred" costs the model a turn to diagnose."""
    result = await _run_with(RuntimeError("boom"), name="compose_dossier")
    assert "compose_dossier" in str(result.content)


def test_describe_tool_failure_survives_an_exception_with_no_message() -> None:
    text = describe_tool_failure("spotlight", RuntimeError())
    assert "RuntimeError" in text


async def test_every_registered_tool_is_wrapped() -> None:
    """Enumerate the REAL orchestrator tools and make each one raise.

    A test over a hand-written stub proves the middleware works. This proves it
    is on the path the actual tools take — which is the half that silently stops
    being true when someone adds a tool.
    """
    from aleph_api.copilot_agent import _ORCHESTRATOR_TOOLS

    assert _ORCHESTRATOR_TOOLS, "the orchestrator's tool list is empty"
    for tool in _ORCHESTRATOR_TOOLS:
        result = await _run_with(RuntimeError("forced"), name=tool.name)
        assert isinstance(result, ToolMessage), f"{tool.name} was not survivable"
        assert result.status == "error"
