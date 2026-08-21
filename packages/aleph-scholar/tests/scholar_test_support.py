"""Test doubles for aleph-scholar unit tests (no network, no real redis)."""

from __future__ import annotations


class FakeRedis:
    """In-memory stand-in for the redis.asyncio subset the package uses.

    A tiny stub matching only the commands under test, rather than a general
    fake — the surface it has to be right about is the surface it is used for.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        self.expiries: dict[str, int] = {}

    async def incr(self, name: str) -> int:
        self.counters[name] = self.counters.get(name, 0) + 1
        return self.counters[name]

    async def expire(self, name: str, time: int) -> bool:
        self.expiries[name] = time
        return True

    async def set(
        self, name: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and name in self.store:
            return None
        self.store[name] = value
        if ex is not None:
            self.expiries[name] = ex
        return True

    async def get(self, name: str) -> str | None:
        return self.store.get(name)

    async def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            if self.store.pop(name, None) is not None:
                removed += 1
        return removed
