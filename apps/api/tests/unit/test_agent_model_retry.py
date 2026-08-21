"""A rate limit is a wait, not the end of the turn.

Three settings made the assistant fragile under any load: every model call gave
up after 60 seconds with two retries, the retries were IMMEDIATE — the worst
possible response to being rate limited — and the resulting exception killed the
run. The owner's two live defects, "weirdly rate limited" and "the run errored
with nothing traced", are the same event seen from opposite ends.

The sleeper is injected so these run instantly. A backoff test that actually
waits thirty seconds is a test people delete, and a deleted test is worse than
a slow one.
"""

from __future__ import annotations

from typing import Any

import pytest

from aleph_api.agent_middleware import (
    AgentModelUnavailable,
    AlephAgentMiddleware,
    classify_model_failure,
    retry_after_seconds,
    retry_delay,
)


class _Settings:
    aleph_agent_max_retries = 3
    aleph_agent_retry_base_delay_s = 1.0
    aleph_agent_retry_max_delay_s = 30.0


class _RateLimited(Exception):
    def __init__(self, retry_after: str | None = None) -> None:
        super().__init__("Too Many Requests")
        self.status_code = 429
        self.response = type(
            "R",
            (),
            {"status_code": 429, "headers": {"retry-after": retry_after} if retry_after else {}},
        )()


def _middleware(slept: list[float]) -> AlephAgentMiddleware:
    async def sleeper(seconds: float) -> None:
        slept.append(seconds)

    return AlephAgentMiddleware(settings=_Settings(), sleeper=sleeper, jitter=lambda: 0.0)


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_RateLimited(), "rate_limited"),
        (TimeoutError("slow"), "upstream_timeout"),
        (type("RateLimitError", (Exception,), {})("x"), "rate_limited"),
        (RuntimeError("rate limit exceeded for model"), "rate_limited"),
        (ValueError("bad arguments"), "internal"),
    ],
)
def test_failures_are_classified_without_importing_a_provider(
    exc: BaseException, expected: str
) -> None:
    """Matched on status and class NAME, not on an imported provider class.

    The gateway is whatever OpenAI-compatible endpoint the operator pointed
    Aleph at, so the exception type depends on which client library raised it —
    an import-based check silently stops recognising the next one.
    """
    assert classify_model_failure(exc) == expected


def test_retry_after_is_honoured_over_the_curve() -> None:
    """A server that told you when to come back beats any backoff guess."""
    assert retry_after_seconds(_RateLimited("7")) == 7.0
    assert retry_delay(0, _RateLimited("7"), base=1.0, ceiling=30.0) == 7.0


def test_retry_after_is_still_capped() -> None:
    """An upstream asking for an hour must not hang the turn for an hour."""
    assert retry_delay(0, _RateLimited("3600"), base=1.0, ceiling=30.0) == 30.0


def test_an_unparseable_retry_after_falls_back_to_the_curve() -> None:
    assert retry_after_seconds(_RateLimited("Wed, 21 Oct 2026 07:28:00 GMT")) is None


def test_the_delay_actually_grows() -> None:
    delays = [retry_delay(n, RuntimeError("x"), base=1.0, ceiling=30.0) for n in range(4)]
    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_jitter_separates_simultaneous_retries() -> None:
    """Six subagents rate limited together must not come back together — that
    reproduces the burst that caused the limit."""
    plain = retry_delay(1, RuntimeError("x"), base=1.0, ceiling=30.0, jitter=0.0)
    jittered = retry_delay(1, RuntimeError("x"), base=1.0, ceiling=30.0, jitter=0.25)
    assert jittered > plain


# --- the middleware hook ----------------------------------------------------


async def test_rate_limit_is_retried_with_backoff() -> None:
    slept: list[float] = []
    calls = {"n": 0}

    async def handler(_req: Any) -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _RateLimited()
        return "answered"

    result = await _middleware(slept).awrap_model_call(object(), handler)

    assert result == "answered"
    assert calls["n"] == 3
    assert slept == [1.0, 2.0], f"the retries did not back off: {slept}"


async def test_budget_exhaustion_is_typed() -> None:
    """A bare exception is what made the original failure untraceable."""
    slept: list[float] = []
    calls = {"n": 0}

    async def handler(_req: Any) -> str:
        calls["n"] += 1
        raise _RateLimited()

    with pytest.raises(AgentModelUnavailable) as caught:
        await _middleware(slept).awrap_model_call(object(), handler)

    assert caught.value.code == "rate_limited"
    assert caught.value.attempts == _Settings.aleph_agent_max_retries
    assert calls["n"] == _Settings.aleph_agent_max_retries


async def test_an_internal_error_is_not_retried() -> None:
    """Retrying a bad argument is a slower way to fail and three times the spend."""
    slept: list[float] = []
    calls = {"n": 0}

    async def handler(_req: Any) -> str:
        calls["n"] += 1
        msg = "bad arguments"
        raise ValueError(msg)

    with pytest.raises(AgentModelUnavailable) as caught:
        await _middleware(slept).awrap_model_call(object(), handler)

    assert calls["n"] == 1
    assert slept == []
    assert caught.value.code == "internal"


async def test_a_working_call_is_returned_untouched() -> None:
    slept: list[float] = []

    async def handler(_req: Any) -> str:
        return "fine"

    assert await _middleware(slept).awrap_model_call(object(), handler) == "fine"
    assert slept == []
