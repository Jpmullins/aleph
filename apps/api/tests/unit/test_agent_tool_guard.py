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
    """Every tool the orchestrator carries survives raising, by NAME.

    What this does and does not prove, because the docstring used to overstate
    it. It drives `AlephAgentMiddleware` over a synthetic handler once per
    entry in `_ORCHESTRATOR_TOOLS`, passing each tool's name — so it catches a
    tool whose name the middleware mishandles, and it catches the list going
    empty. It does NOT build a graph and does NOT prove the middleware is
    mounted: removing `AlephAgentMiddleware` from `create_deep_agent` entirely
    leaves this green (measured 2026-08-23).

    The "is it actually wired" half belongs to `check-agent-middleware.sh`,
    which AST-walks the `create_deep_agent(middleware=[...])` call and does go
    red, and to `test_every_subagent_spec_really_carries_the_guard` below,
    which asserts an INSTANCE on each built subagent spec.
    """
    from aleph_api.copilot_agent import _ORCHESTRATOR_TOOLS

    assert _ORCHESTRATOR_TOOLS, "the orchestrator's tool list is empty"
    for tool in _ORCHESTRATOR_TOOLS:
        result = await _run_with(RuntimeError("forced"), name=tool.name)
        assert isinstance(result, ToolMessage), f"{tool.name} was not survivable"
        assert result.status == "error"


def _settings() -> Any:
    """A Settings a subagent builder can be constructed against, no network."""
    from aleph_api.settings import Settings

    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://aleph:x@localhost:5432/aleph",
        redis_url="redis://localhost:6379/0",
        langfuse_host="http://localhost:3000",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        otel_exporter_otlp_endpoint="http://localhost:4317",
        litellm_base_url="http://localhost:18999",
        insights_litellm_api_key="test-key",
        aleph_agent_token_secret="unit-test-secret-0123456789abcdef0123456789abcdef",
        aleph_credential_master_key="c" * 64,
    )


def _built_subagent_specs() -> dict[str, dict[str, Any]]:
    """Every `build_*_subagent` in the package, discovered and BUILT.

    Discovered rather than listed. A seventh subagent must be covered the day it
    lands, and a list of six here would be a test that quietly stops testing the
    thing it is named after — which is how the guard came to be missing from the
    subagents in the first place.
    """
    import importlib
    import pkgutil
    import re

    import aleph_api.subagents as package

    settings = _settings()
    specs: dict[str, dict[str, Any]] = {}
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for attr in dir(module):
            if not re.fullmatch(r"build_\w+_subagent", attr):
                continue
            specs[attr] = getattr(module, attr)(settings=settings)
    return specs


def test_every_subagent_spec_really_carries_the_guard() -> None:
    """The BUILT spec, not the source text — the sweep only reads the text.

    `scripts/check-agent-middleware.sh` decides a subagent is guarded by looking
    for the string `AlephAgentMiddleware` anywhere in its file. Measured: empty
    the retriever's middleware list to `[]` and leave the import line and the
    comment above it alone, and the sweep still prints "orchestrator + 6
    subagents guarded" and exits 0. Every tool that subagent carries is then one
    exception away from ending the turn, with a green gate over it.

    This asserts the object. The middleware list a builder actually returns has
    to contain an `AlephAgentMiddleware` instance, so no arrangement of imports,
    comments or docstrings can satisfy it.
    """
    specs = _built_subagent_specs()
    assert specs, "no build_*_subagent functions were discovered — update this test"

    unguarded = [
        name
        for name, spec in specs.items()
        if not any(
            isinstance(entry, AlephAgentMiddleware) for entry in spec.get("middleware") or []
        )
    ]
    assert not unguarded, (
        f"{unguarded} return a spec whose middleware list holds no AlephAgentMiddleware. "
        "deepagents REPLACES the parent's middleware when a spec declares its own, so "
        "these subagents' tools are unguarded and a raising tool ends the turn."
    )
