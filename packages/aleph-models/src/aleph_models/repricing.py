"""Keep the pricing table honest after boot.

Discovery ran exactly once — inside the `models` capability's setup — and that
was the whole of it. `GatewayCatalog.refresh_pricing` swallows an unreachable
gateway by design, so the failure mode is silent and total: an API that wins the
race against its own gateway in compose, a gateway restarted for a config
change, a network blip at the wrong second, and the `PricingTable` stays empty
for the life of the process. Every call that process then serves records
`pricing_source="unknown"` and `cost_usd=0`, which on a spend dashboard reads as
a quiet day rather than a broken meter. The only repair was a restart, and
nothing told anybody a restart was needed.

This module is the missing consumer of `refresh_pricing`: an interval task that
re-runs discovery and folds the result into the **same** `PricingTable` object
`LiteLLMClient` and the agent's cost callback already hold. `PricingTable.merge`
mutates in place precisely so a refresh reaches every holder without rebuilding
any of them — see its docstring; that is the property this depends on.

**Two intervals, not one.** While the gateway has never answered, it is presumed
missing and the retry is short. Once it has answered at all, the refresh is only
catching price changes and can be slow. A single interval has to choose between
a fifteen-minute hole in the ledger and a discovery sweep every thirty seconds
forever, and neither is the right answer to both questions.

The switch is keyed on **reachability**, not on whether anything got priced. A
LiteLLM virtual key restricted to `llm_api_routes` lists its models and reports
no rates at all — that is the normal deployment, not a fault, and hammering it
every thirty seconds until the heat death of the universe would be a retry loop
against a gateway that is working perfectly.

`sleep` and `clock` are injected. A test that proves "priced once the gateway
comes up" by sleeping the real interval is a test nobody runs, and a test nobody
runs is how this defect survived in the first place.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from aleph_models.limiter import env_number

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

    from aleph_models.discovery import GatewayCatalog
    from aleph_models.pricing import PricingTable

__all__ = [
    "DEFAULT_REFRESH_S",
    "DEFAULT_RETRY_S",
    "PricingRefresher",
    "refresh_intervals",
]

_log = structlog.get_logger(__name__)

#: Fifteen minutes between sweeps once the gateway is known to answer. Rates
#: change on the scale of a deployment, not a request.
DEFAULT_REFRESH_S = 900.0
#: Thirty seconds while it has never answered. Short enough that a gateway
#: coming up behind the API is priced within the first minute of its life, long
#: enough that a genuinely absent gateway is not being probed in a tight loop.
DEFAULT_RETRY_S = 30.0

_ENV_REFRESH = "ALEPH_GATEWAY_PRICING_REFRESH_S"
_ENV_RETRY = "ALEPH_GATEWAY_PRICING_RETRY_S"


def refresh_intervals(settings: Any = None) -> tuple[float, float]:
    """`(interval_s, retry_s)` from Settings if it carries them, else the environment.

    `getattr` rather than attribute access, for the same reason
    `LimiterConfig.from_settings` does it: `aleph-models` is imported by two
    processes with two different Settings classes and may not import either. A
    deployment can set `ALEPH_GATEWAY_PRICING_REFRESH_S` today, and the moment a
    field of that name lands on Settings it wins with no change here.
    """
    interval = getattr(settings, "aleph_gateway_pricing_refresh_s", None)
    retry = getattr(settings, "aleph_gateway_pricing_retry_s", None)
    return (
        float(
            interval
            if interval is not None
            else env_number(_ENV_REFRESH, DEFAULT_REFRESH_S, minimum=1.0)
        ),
        float(retry if retry is not None else env_number(_ENV_RETRY, DEFAULT_RETRY_S, minimum=1.0)),
    )


class PricingRefresher:
    """Re-runs gateway discovery on an interval, into a table others already hold."""

    def __init__(
        self,
        *,
        catalog: GatewayCatalog,
        pricing: PricingTable,
        interval_s: float = DEFAULT_REFRESH_S,
        retry_interval_s: float = DEFAULT_RETRY_S,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._catalog = catalog
        self._pricing = pricing
        # Clamped, not validated. A misconfigured interval of 0 is a tight loop
        # against the gateway — the one failure a rate-limit refresher must not
        # cause — so it becomes one second rather than taking the process down.
        self._interval_s = max(1.0, float(interval_s))
        self._retry_interval_s = max(1.0, float(retry_interval_s))
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._task: asyncio.Task[None] | None = None
        #: Sweeps attempted, successful or not.
        self.cycles = 0
        #: Models priced by the most recent sweep.
        self.priced = 0
        #: `clock()` at the last sweep that priced at least one model, or None.
        self.last_priced_at: float | None = None
        #: The last unexpected failure, as `Type: message`. Cleared on success.
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def next_interval(self) -> float:
        """Short while the gateway has never answered; long once it has.

        `catalog.cached` is non-empty only after a discovery call returned a
        model list, so this reads reachability. Keying it on
        `pricing.models()` instead would retry forever against the restricted
        virtual key that is the normal production configuration.
        """
        return self._interval_s if self._catalog.cached else self._retry_interval_s

    def describe(self) -> str:
        """One line for a probe or a health page: is this gap self-healing?

        An empty pricing table that is being retried and one that has been
        abandoned are the same table and the same log line, and they call for
        opposite responses — wait, or go and look at the gateway. Read by the
        `models` capability probe.
        """
        if not self.running:
            return "NOT retrying — the pricing refresher is not running"
        return f"retrying every {self.next_interval():g}s"

    async def refresh_once(self) -> int:
        """One discovery sweep, folded in place. Returns the count priced.

        Never raises. `refresh_pricing` already swallows `httpx.HTTPError` and
        `ValueError`; anything else — a DNS resolver raising `OSError`, a
        malformed URL raising `TypeError` — would otherwise end the loop, and a
        refresher that has stopped is indistinguishable from one that never
        started.
        """
        self.cycles += 1
        try:
            # `force=True` bypasses the catalog's own TTL. Without it a sweep
            # inside the TTL window returns the cached list — which after a
            # failed discovery is the EMPTY list — and merges nothing, so the
            # refresher would tick forever and change nothing.
            priced = await self._catalog.refresh_pricing(self._pricing, force=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            _log.error(
                "gateway.pricing_refresh_failed",
                error=self.last_error,
                impact="model calls keep recording pricing_source=unknown",
            )
            return 0
        self.priced = priced
        if priced:
            self.last_priced_at = self._clock()
            self.last_error = None
        return priced

    async def _run(self) -> None:
        while True:
            await self._sleep(self.next_interval())
            await self.refresh_once()

    def start(self) -> None:
        """Begin sweeping. Idempotent — a second call while running is a no-op."""
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="gateway.pricing_refresh")

    async def stop(self, *, timeout_s: float = 5.0) -> None:
        """Cancel and wait, with a ceiling on the wait.

        `models` is `protected = true` in both boot manifests, so this inverse
        runs on every shutdown of a process that cannot come up without it. An
        unbounded await would turn one stuck discovery request into a process
        that never exits.

        `asyncio.wait`, not `asyncio.wait_for`: `wait_for` re-raises the
        `CancelledError` this method just caused, and a `CancelledError` escaping
        into a shutdown path reads to the caller as *itself* being cancelled.
        `wait` reports the task's fate instead of adopting it.
        """
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        _done, pending = await asyncio.wait({task}, timeout=max(0.0, timeout_s))
        if pending:
            _log.warning(
                "gateway.pricing_refresh_stop_timeout",
                timeout_s=timeout_s,
                impact="the refresh task was left running; shutdown continued",
            )
