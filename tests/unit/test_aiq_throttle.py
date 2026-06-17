"""AIQThrottle — Redis-backed admission gate for in-flight AIQ research jobs.

Bounds how many research jobs run inside the aiq-server container at once
(each is a Dask workload; an unbounded stampede is what swap-killed the host
on 2026-06-11). Backed by a sorted set: member = agent_run_id, score =
acquire time; stale members past the TTL are pruned on every acquire so a
crashed poller can never leak a slot forever.

Tested against an in-memory zset fake that mirrors Redis sorted-set
semantics (score order, lexical tie-break).
"""

from __future__ import annotations

import time

import pytest

from aleph_aiq.throttle import AIQThrottle

pytestmark = pytest.mark.asyncio


class FakeZSetRedis:
    """The five sorted-set commands AIQThrottle uses, Redis-faithful."""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}

    def _sorted(self, key: str) -> list[tuple[str, float]]:
        items = self.zsets.get(key, {})
        return sorted(items.items(), key=lambda kv: (kv[1], kv[0]))

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        zset = self.zsets.setdefault(key, {})
        added = sum(1 for m in mapping if m not in zset)
        zset.update(mapping)
        return added

    async def zrank(self, key: str, member: str) -> int | None:
        for i, (m, _) in enumerate(self._sorted(key)):
            if m == member:
                return i
        return None

    async def zrem(self, key: str, *members: str) -> int:
        zset = self.zsets.get(key, {})
        removed = 0
        for m in members:
            if m in zset:
                del zset[m]
                removed += 1
        return removed

    async def zremrangebyscore(self, key: str, min: float | str, max: float | str) -> int:
        lo = float("-inf") if min == "-inf" else float(min)
        hi = float("inf") if max == "+inf" else float(max)
        zset = self.zsets.get(key, {})
        doomed = [m for m, s in zset.items() if lo <= s <= hi]
        for m in doomed:
            del zset[m]
        return len(doomed)

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))


async def test_admits_up_to_max_then_rejects() -> None:
    redis = FakeZSetRedis()
    throttle = AIQThrottle(redis, max_concurrent=3)
    assert await throttle.acquire("run-1") is True
    assert await throttle.acquire("run-2") is True
    assert await throttle.acquire("run-3") is True
    assert await throttle.acquire("run-4") is False


async def test_rejected_member_leaves_no_residue() -> None:
    redis = FakeZSetRedis()
    throttle = AIQThrottle(redis, max_concurrent=1)
    assert await throttle.acquire("run-1") is True
    assert await throttle.acquire("run-2") is False
    # The rejected member must not occupy the zset (it would block release
    # ordering and inflate counts).
    assert await redis.zrank(throttle.key, "run-2") is None


async def test_release_frees_a_slot() -> None:
    redis = FakeZSetRedis()
    throttle = AIQThrottle(redis, max_concurrent=2)
    assert await throttle.acquire("run-1") is True
    assert await throttle.acquire("run-2") is True
    assert await throttle.acquire("run-3") is False
    await throttle.release("run-1")
    assert await throttle.acquire("run-3") is True


async def test_reacquire_same_member_is_idempotent() -> None:
    """A retried submit job re-acquiring its own slot must not double-count."""
    redis = FakeZSetRedis()
    throttle = AIQThrottle(redis, max_concurrent=2)
    assert await throttle.acquire("run-1") is True
    assert await throttle.acquire("run-1") is True
    assert await throttle.acquire("run-2") is True  # only one slot held by run-1


async def test_stale_holders_are_pruned_on_acquire() -> None:
    """Slots leaked by a dead poller free themselves after the TTL."""
    redis = FakeZSetRedis()
    throttle = AIQThrottle(redis, max_concurrent=2, ttl_seconds=600)
    stale = time.time() - 601
    await redis.zadd(throttle.key, {"dead-1": stale, "dead-2": stale})
    assert await throttle.acquire("run-1") is True
    assert await redis.zrank(throttle.key, "dead-1") is None


async def test_release_unknown_member_is_a_noop() -> None:
    redis = FakeZSetRedis()
    throttle = AIQThrottle(redis, max_concurrent=1)
    await throttle.release("never-acquired")  # must not raise
    assert await throttle.acquire("run-1") is True
