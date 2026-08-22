"""Directory-wide defaults for the agent path's unit tests.

WS-MEP-6 deleted the hardcoded fallback model id from `copilot_agent` (it was
`claude-sonnet-4-6`, a claim about a model on somebody else's gateway). Model
resolution now comes from a project's `ModelProfile`, and a capability with no
binding raises `NoModelBound` rather than substituting a model nobody chose — so
*building* an agent now requires saying which models it is built from.

Production supplies that from the database, per request. A unit test has no
database, so this binds an obviously synthetic profile for the whole directory.
That is not a workaround: every one of these tests was already resolving a
hardcoded Anthropic id, silently, and what they are actually about is what the
agent is WIRED to — the metered HTTP client, the cost callback's purpose, the
skills sources, the middleware order — none of which depends on which model a
gateway serves.

Two things it deliberately does not do:

* It does not set `ALEPH_FALLBACK_AGENT_MODEL`. That env var is the operator's
  escape hatch, and a suite that always set it could never observe the error.
* It does not put the unbound case out of reach. `_runtime` is a plain dict, so
  a test that needs nothing bound pops the key — see
  `test_agent_model_resolution.py::test_an_unbound_capability_is_an_error`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Synthetic on purpose. A real model id here would be the same defect the
#: workstream removed, moved into the test suite — and these names appearing in
#: a failure message make it obvious where a model came from.
TEST_AGENT_BINDINGS: dict[str, Any] = {
    "synthesis": {"model": "test-synthesis-model", "provider": "litellm"},
    "judge": {"model": "test-judge-model", "provider": "litellm"},
    "code": {"model": "test-code-model", "provider": "litellm"},
}


@pytest.fixture(autouse=True)
def bound_agent_models() -> Iterator[dict[str, Any]]:
    """Bind a synthetic model profile for the duration of each test.

    Restores the previous value rather than clearing it: `_runtime` is
    process-global, and a test that leaves it changed decides what the next
    test resolves.
    """
    from aleph_api import copilot_agent

    bindings = dict(TEST_AGENT_BINDINGS)
    previous = copilot_agent._runtime.get("agent_bindings")
    copilot_agent._runtime["agent_bindings"] = bindings
    try:
        yield bindings
    finally:
        if previous is None:
            copilot_agent._runtime.pop("agent_bindings", None)
        else:
            copilot_agent._runtime["agent_bindings"] = previous
