"""Real-time push layer: Postgres LISTEN/NOTIFY → in-process fan-out.

The DB triggers (migration `realtime_notify_triggers`) `pg_notify('aleph_changes', …)`
on every mutation, delivered on COMMIT. `NotifyListener` holds one dedicated asyncpg
connection per API process that `LISTEN`s on the channel and republishes each signal to
`ChangeBroker`, an in-process per-project fan-out. Each SSE stream subscribes to the
broker and wakes the instant a relevant write commits — with a slow poll fallback
underneath (the stream's own `wait(timeout=…)`) so a dropped listener self-heals instead
of going silent.

`ChangeBroker` is pure in-process state (no I/O) so it is unit-testable directly;
`NotifyListener` owns the DB connection + reconnect supervision.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID

import asyncpg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

CHANNEL = "aleph_changes"


def asyncpg_dsn(database_url: str) -> str:
    """Convert a SQLAlchemy ``postgresql+asyncpg://…`` URL to a plain asyncpg DSN.

    asyncpg.connect wants a driverless ``postgresql://`` DSN; the SQLAlchemy URL
    carries the ``+asyncpg`` driver tag. Host/db/credentials/query params are
    preserved untouched.
    """
    return database_url.replace("+asyncpg", "", 1)


class Subscription:
    """A single subscriber's view of the broker — an awaitable signal queue."""

    def __init__(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queue = queue

    async def wait(self, timeout: float) -> dict[str, Any] | None:  # noqa: ASYNC109 — `timeout` is the poll-fallback window; encapsulating it keeps every stream's call site a one-liner
        """Return the next signal, or ``None`` if `timeout` seconds elapse first.

        The timeout is what drives each stream's poll fallback: a push delivers a
        signal immediately; absent any push, `wait` returns ``None`` after the
        window and the stream runs its query anyway (self-healing safety net).
        """
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    def drain(self) -> list[dict[str, Any]]:
        """Non-blockingly pull all currently-queued signals (coalesce a burst)."""
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return out


class ChangeBroker:
    """In-process per-project pub/sub fan-out.

    Each process has its own broker and its own listener; since every process
    receives every NOTIFY, cross-process delivery already works without an
    external broker.
    """

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        self._subs: dict[UUID, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._maxsize = queue_maxsize

    def publish(self, project_id: UUID, signal: dict[str, Any]) -> None:
        """Deliver `signal` to every live subscriber of `project_id`.

        A full subscriber queue (a stalled/slow client) drops the signal with a
        warning rather than blocking the listener — the client's poll fallback
        will still reconcile it.
        """
        for queue in self._subs.get(project_id, set()):
            try:
                queue.put_nowait(signal)
            except asyncio.QueueFull:
                logger.warning(
                    "change broker queue full for project %s; dropped (fallback reconciles)",
                    project_id,
                )

    @asynccontextmanager
    async def subscribe(self, project_id: UUID) -> AsyncIterator[Subscription]:
        """Register a subscriber for `project_id` for the life of the context."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._maxsize)
        self._subs.setdefault(project_id, set()).add(queue)
        try:
            yield Subscription(queue)
        finally:
            subs = self._subs.get(project_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    self._subs.pop(project_id, None)

    def subscriber_count(self, project_id: UUID) -> int:
        return len(self._subs.get(project_id, set()))


class NotifyListener:
    """Supervises one asyncpg `LISTEN aleph_changes` connection → `ChangeBroker`.

    Reconnects with capped backoff on connection loss; while down, every stream's
    poll fallback keeps data flowing, so a dropped listener degrades gracefully
    instead of going silent.
    """

    def __init__(self, *, dsn: str, broker: ChangeBroker) -> None:
        self._dsn = dsn
        self._broker = broker
        self._conn: asyncpg.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="aleph-notify-listener")

    async def _run(self) -> None:
        backoff = 0.5
        while not self._closing:
            # `terminated` is set by asyncpg when the connection drops (server
            # close / network loss), so we wait on it event-driven rather than
            # polling `is_closed()`. On stop() the task is cancelled out of the wait.
            terminated = asyncio.Event()
            try:
                conn = await asyncpg.connect(self._dsn)
                self._conn = conn
                conn.add_termination_listener(lambda _c, ev=terminated: ev.set())
                await conn.add_listener(CHANNEL, self._on_notify)
                logger.info("notify listener connected, LISTEN %s", CHANNEL)
                backoff = 0.5
                await terminated.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("notify listener connection error; will reconnect")
            finally:
                await self._close_conn()
            if self._closing:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def _on_notify(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            signal = json.loads(payload)
            project_id = UUID(str(signal["project_id"]))
        except (ValueError, KeyError, TypeError):
            logger.warning("ignoring malformed notify payload: %r", payload)
            return
        self._broker.publish(project_id, signal)

    async def _close_conn(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None or conn.is_closed():
            return
        # Best-effort cleanup on a possibly-dying connection.
        with contextlib.suppress(Exception):
            await conn.remove_listener(CHANNEL, self._on_notify)
        with contextlib.suppress(Exception):
            await conn.close()

    async def stop(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._close_conn()
