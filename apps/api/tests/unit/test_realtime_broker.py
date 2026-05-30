"""Unit tests for the realtime push fan-out (`aleph_api.realtime`).

`ChangeBroker` is pure in-process state — testable without a DB. `NotifyListener`
owns a real asyncpg connection and is covered by the integration suite.
"""

from __future__ import annotations

import asyncio
import uuid

from aleph_api.realtime import ChangeBroker, asyncpg_dsn


def test_asyncpg_dsn_strips_driver_tag() -> None:
    assert asyncpg_dsn("postgresql+asyncpg://u:p@host:5432/db") == "postgresql://u:p@host:5432/db"


def test_asyncpg_dsn_preserves_params_and_is_idempotent_on_plain() -> None:
    assert (
        asyncpg_dsn("postgresql+asyncpg://u:p@h/db?sslmode=require")
        == "postgresql://u:p@h/db?sslmode=require"
    )
    # A DSN with no driver tag is returned unchanged.
    assert asyncpg_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


async def test_publish_reaches_subscriber() -> None:
    broker = ChangeBroker()
    pid = uuid.uuid4()
    async with broker.subscribe(pid) as sub:
        broker.publish(pid, {"kind": "committed", "n": 1})
        got = await sub.wait(timeout=1.0)
    assert got == {"kind": "committed", "n": 1}


async def test_publish_is_project_scoped() -> None:
    broker = ChangeBroker()
    a, b = uuid.uuid4(), uuid.uuid4()
    async with broker.subscribe(a) as sub_a, broker.subscribe(b) as sub_b:
        broker.publish(a, {"for": "a"})
        assert await sub_a.wait(timeout=1.0) == {"for": "a"}
        # b's subscriber sees nothing → times out → None.
        assert await sub_b.wait(timeout=0.05) is None


async def test_multiple_subscribers_same_project_all_receive() -> None:
    broker = ChangeBroker()
    pid = uuid.uuid4()
    async with broker.subscribe(pid) as s1, broker.subscribe(pid) as s2:
        assert broker.subscriber_count(pid) == 2
        broker.publish(pid, {"x": 1})
        assert await s1.wait(timeout=1.0) == {"x": 1}
        assert await s2.wait(timeout=1.0) == {"x": 1}


async def test_wait_times_out_to_none() -> None:
    broker = ChangeBroker()
    pid = uuid.uuid4()
    async with broker.subscribe(pid) as sub:
        assert await sub.wait(timeout=0.05) is None


async def test_unsubscribe_on_context_exit() -> None:
    broker = ChangeBroker()
    pid = uuid.uuid4()
    async with broker.subscribe(pid):
        assert broker.subscriber_count(pid) == 1
    assert broker.subscriber_count(pid) == 0
    # Publishing to a project with no subscribers is a no-op (no raise).
    broker.publish(pid, {"x": 1})


async def test_drain_coalesces_a_burst() -> None:
    broker = ChangeBroker()
    pid = uuid.uuid4()
    async with broker.subscribe(pid) as sub:
        broker.publish(pid, {"n": 1})
        broker.publish(pid, {"n": 2})
        broker.publish(pid, {"n": 3})
        # First wait returns one; drain pulls the rest without blocking.
        first = await sub.wait(timeout=1.0)
        rest = sub.drain()
    assert [first, *rest] == [{"n": 1}, {"n": 2}, {"n": 3}]


def test_full_queue_drops_without_raising() -> None:
    broker = ChangeBroker(queue_maxsize=2)
    pid = uuid.uuid4()

    async def _run() -> None:
        async with broker.subscribe(pid):
            # 3 publishes into a maxsize-2 queue: the 3rd is dropped, no exception.
            for i in range(3):
                broker.publish(pid, {"n": i})

    asyncio.run(_run())
