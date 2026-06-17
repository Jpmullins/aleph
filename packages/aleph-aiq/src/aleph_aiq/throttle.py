"""Admission gate bounding in-flight AIQ research jobs.

Every research job submitted to aiq-server runs as a Dask workload inside
that single container; with no gate, a bootstrap stampede (N projects x
max_topics) runs them all concurrently and can exhaust host memory. The
throttle is a Redis sorted set — member = agent_run_id, score = acquire
time — shared by every submitter (API ``/synthesize`` route and worker
jobs), so the bound holds across processes.

Admission is rank-based: add yourself, then check your rank. Concurrent
acquirers get distinct ranks, so the gate never over-admits; the losing
acquirer removes itself and retries later (via the deferred submit job).
Stale members past ``ttl_seconds`` are pruned on every acquire, so a slot
leaked by a crashed poller frees itself.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import Protocol

DEFAULT_KEY = "aleph:aiq:inflight"
# Must exceed the poll job's hard ceiling (90 polls x 20s = 30 min) so a slot
# only ever expires under the poller, never out from under a live job.
DEFAULT_TTL_S = 2700


class ZSetClient(Protocol):
    """The sorted-set subset of redis.asyncio.Redis the throttle needs."""

    def zadd(self, key: str, mapping: dict[str, float]) -> Awaitable[int]: ...

    def zrank(self, key: str, member: str) -> Awaitable[int | None]: ...

    def zrem(self, key: str, *members: str) -> Awaitable[int]: ...

    def zremrangebyscore(self, key: str, min: float | str, max: float | str) -> Awaitable[int]: ...


class AIQThrottle:
    def __init__(
        self,
        redis: ZSetClient,
        *,
        max_concurrent: int,
        ttl_seconds: int = DEFAULT_TTL_S,
        key: str = DEFAULT_KEY,
    ) -> None:
        self._redis = redis
        self._max = max_concurrent
        self._ttl = ttl_seconds
        self.key = key

    async def acquire(self, member: str) -> bool:
        """Claim a slot for ``member``. Idempotent for an already-held slot."""
        now = time.time()
        await self._redis.zremrangebyscore(self.key, "-inf", now - self._ttl)
        await self._redis.zadd(self.key, {member: now})
        rank = await self._redis.zrank(self.key, member)
        if rank is not None and rank < self._max:
            return True
        await self._redis.zrem(self.key, member)
        return False

    async def release(self, member: str) -> None:
        await self._redis.zrem(self.key, member)
