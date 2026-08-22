"""Retry policy for gateway calls — and the header that says how long to wait.

Three attempts, retrying 429, 5xx and transport errors, never any other 4xx.

**The wait is the gateway's number when the gateway gives one.** This module
used to back off 1s / 2s / 4s blind and read no headers at all, while
`aleph_scholar.http` — against friendlier upstreams — already parsed
`Retry-After` and honoured it. A gateway answering `429 Retry-After: 7` is
telling you exactly when it will serve you; retrying after one second is a
request that cannot succeed, sent while the endpoint is *already* over its
budget. Three of those turn one rate limit into four.

`Retry-After` is optional and comes in two shapes — delta-seconds and an
HTTP-date — and both are in the wild, so both are parsed. When there is no
header the exponential backoff is unchanged; that is the honest fallback rather
than a guess at the gateway's schedule.

The honoured wait is capped (`MAX_RETRY_AFTER_S`). A gateway that answers
`Retry-After: 3600` is not asking to be waited for inside one request — it is
telling you the key is exhausted, and blocking a caller for an hour converts a
clear error into a hang. The cap is applied and logged; the call then fails with
the gateway's own status, which is the answer the caller can act on.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

__all__ = ["MAX_RETRY_AFTER_S", "gateway_retry", "retry_after_seconds"]

_log = structlog.get_logger(__name__)

#: Longest `Retry-After` this policy will actually sleep for, in seconds. Beyond
#: this the header is reporting an exhausted budget rather than a queue position,
#: and waiting it out inside a request is indistinguishable from a hang.
MAX_RETRY_AFTER_S = 60.0

#: The unchanged blind schedule, used only when the gateway said nothing.
_EXPONENTIAL = wait_exponential(multiplier=1, min=1, max=4)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError | httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Seconds to wait per `Retry-After`, or ``None`` when the header says nothing.

    Both RFC 9110 forms are accepted. The HTTP-date form is measured against the
    response's own `Date` header when it has one, because a client clock that is
    a minute fast turns "wait 7 seconds" into "wait none" — and a retry that
    fires early is precisely what the header exists to prevent.
    """
    raw = response.headers.get("Retry-After")
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now: datetime | None = None
    served = response.headers.get("Date")
    if served:
        try:
            now = parsedate_to_datetime(served)
        except (TypeError, ValueError):
            now = None
        if now is not None and now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    if now is None:
        now = datetime.now(UTC)
    return max(0.0, (when - now).total_seconds())


def _wait(retry_state: RetryCallState) -> float:
    """The gateway's own number when it gave one; the blind schedule otherwise."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None and outcome.failed else None
    if isinstance(exc, httpx.HTTPStatusError):
        asked = retry_after_seconds(exc.response)
        if asked is not None:
            honoured = min(asked, MAX_RETRY_AFTER_S)
            if honoured < asked:
                _log.warning(
                    "gateway.retry_after_capped",
                    asked_s=asked,
                    waiting_s=honoured,
                    cap_s=MAX_RETRY_AFTER_S,
                    status=exc.response.status_code,
                    impact="the call will fail with the gateway's status rather than block",
                )
            return honoured
    return _EXPONENTIAL(retry_state)


def gateway_retry(*, sleep: Callable[[float], Awaitable[None]] | None = None) -> AsyncRetrying:
    """The shared policy. `sleep` is injectable so a test can assert the wait.

    Without that seam, proving "a `Retry-After: 7` produces a seven second wait"
    costs seven seconds of real time per assertion, which is how a test like
    that ends up not being written.
    """
    return AsyncRetrying(
        # Tenacity's own default resolves to this on an asyncio loop; naming it
        # keeps the parameter one type rather than an optional one, so a test
        # passing a recorder is checked against the same signature production
        # uses.
        sleep=asyncio.sleep if sleep is None else sleep,
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=_wait,
        reraise=True,
    )
