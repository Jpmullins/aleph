"""Binding enforcement: a disabled connector is never instantiated, and the
bound list excludes it; credential-less kinds skip with a warning, not fatally."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aleph_research.tools import (
    ConnectorRow,
    dev_credential_defaults,
    effective_enabled_kinds,
    resolve_bound_tools,
)


def _row(kind: str, *, requires_auth: bool = False, default: bool = False, binding: bool | None):
    return ConnectorRow(
        kind=kind,
        connector_id=uuid4(),
        requires_auth=requires_auth,
        enabled_by_default=default,
        binding_enabled=binding,
    )


class TestEffectiveEnabledKinds:
    def test_explicit_binding_beats_enabled_by_default(self) -> None:
        rows = [
            _row("tavily", default=True, binding=False),  # default-on, explicitly disabled
            _row("exa", default=False, binding=True),  # default-off, explicitly enabled
            _row("openalex", default=True, binding=None),  # default-on, unbound
            _row("lens", default=False, binding=None),  # default-off, unbound
        ]
        registered = frozenset({"tavily", "exa", "openalex", "lens"})
        assert effective_enabled_kinds(rows, registered=registered) == ["exa", "openalex"]

    def test_allowed_filter(self) -> None:
        rows = [_row("tavily", binding=True), _row("arxiv", binding=True)]
        registered = frozenset({"tavily", "arxiv"})
        out = effective_enabled_kinds(rows, allowed=["arxiv"], registered=registered)
        assert out == ["arxiv"]

    def test_unregistered_kind_excluded(self) -> None:
        rows = [_row("upload", binding=True)]
        assert effective_enabled_kinds(rows, registered=frozenset({"tavily"})) == []


class _FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows

    def scalar_one_or_none(self) -> None:
        return None  # no ConnectorCredential row


class _FakeSession:
    """First execute() answers the connector-rows query; later ones (the
    credential service's ConnectorCredential lookup) return nothing."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.calls = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    async def execute(self, stmt: object) -> _FakeResult:
        self.calls += 1
        return _FakeResult(self._rows if self.calls == 1 else [])


def _tuple_row(
    kind: str, *, requires_auth: bool = False, default: bool = False, binding: bool | None
) -> tuple[Any, ...]:
    # (kind, connector_id, requires_auth, enabled_by_default, binding_enabled)
    return (kind, uuid4(), requires_auth, default, binding)


class _SpyFactory:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return SimpleNamespace(kind=self.kind)


async def test_disabled_connector_never_instantiated() -> None:
    rows = [
        _tuple_row("tavily", binding=True),
        _tuple_row("exa", default=True, binding=False),  # explicitly disabled
        _tuple_row("lens", default=False, binding=None),  # disabled by default
    ]
    session = _FakeSession(rows)
    factories = {
        "tavily": _SpyFactory("tavily"),
        "exa": _SpyFactory("exa"),
        "lens": _SpyFactory("lens"),
    }

    bound = await resolve_bound_tools(
        session_maker=lambda: session,  # type: ignore[arg-type]
        project_id=uuid4(),
        agent_token_secret="test-secret",
        auth_mode="local",
        factories=factories,  # type: ignore[arg-type]
    )

    assert [t.kind for t in bound] == ["tavily"]
    assert factories["tavily"].calls == 1
    assert factories["exa"].calls == 0, "explicitly disabled connector was constructed"
    assert factories["lens"].calls == 0, "default-disabled connector was constructed"


async def test_missing_credential_skips_with_warning_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    rows = [
        _tuple_row("serper", requires_auth=True, binding=True),  # no credential anywhere
        _tuple_row("arxiv", requires_auth=False, binding=True),
    ]
    session = _FakeSession(rows)
    factories = {"serper": _SpyFactory("serper"), "arxiv": _SpyFactory("arxiv")}
    skipped: list[tuple[str, str]] = []

    async def on_skip(kind: str, reason: str) -> None:
        skipped.append((kind, reason))

    bound = await resolve_bound_tools(
        session_maker=lambda: session,  # type: ignore[arg-type]
        project_id=uuid4(),
        agent_token_secret="test-secret",
        auth_mode="local",
        factories=factories,  # type: ignore[arg-type]
        on_skip=on_skip,
    )

    assert [t.kind for t in bound] == ["arxiv"]
    assert factories["serper"].calls == 0
    assert skipped and skipped[0][0] == "serper"


async def test_env_fallback_credential_binds_in_local_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    rows = [_tuple_row("tavily", requires_auth=True, binding=True)]
    session = _FakeSession(rows)
    factories = {"tavily": _SpyFactory("tavily")}

    bound = await resolve_bound_tools(
        session_maker=lambda: session,  # type: ignore[arg-type]
        project_id=uuid4(),
        agent_token_secret="test-secret",
        auth_mode="local",
        factories=factories,  # type: ignore[arg-type]
    )
    assert [t.kind for t in bound] == ["tavily"]
    assert bound[0].credential == "tvly-test"


def test_dev_credential_defaults_gated_on_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    assert dev_credential_defaults("local")["tavily"] == "tvly-test"
    assert dev_credential_defaults("oidc") == {}


# --------------------------------------------------------------------------
# dispatch_research (replaces deleted tests/unit/test_dispatch_research.py)
# --------------------------------------------------------------------------


class _RecordingSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flushed = 0
        self.committed = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def commit(self) -> None:
        self.committed += 1


class _RecordingLedger:
    def __init__(self) -> None:
        self.appends: list[dict[str, Any]] = []

    async def append(self, **kw: Any) -> None:
        self.appends.append(kw)


class _RecordingPool:
    def __init__(self) -> None:
        self.jobs: list[tuple[Any, ...]] = []

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
        self.jobs.append((args, kwargs))


async def test_dispatch_research_no_enabled_connectors_raises(monkeypatch: pytest.MonkeyPatch):
    import aleph_research.dispatch as disp

    async def _none(*_a: Any, **_k: Any) -> list[str]:
        return []

    monkeypatch.setattr(disp, "resolve_enabled_kinds", _none)
    with pytest.raises(ValueError, match="no connectors"):
        await disp.dispatch_research(
            session=_RecordingSession(),
            ledger=_RecordingLedger(),
            redis_pool=_RecordingPool(),
            agent_token_secret="x" * 32,
            project_id=uuid4(),
            principal_user_id=uuid4(),
            actor_kind="user",
            topic="t",
        )


async def test_dispatch_research_creates_run_ledgers_commits_and_enqueues(
    monkeypatch: pytest.MonkeyPatch,
):
    import aleph_research.dispatch as disp

    async def _enabled(*_a: Any, **_k: Any) -> list[str]:
        return ["openalex", "arxiv"]

    monkeypatch.setattr(disp, "resolve_enabled_kinds", _enabled)
    session, ledger, pool = _RecordingSession(), _RecordingLedger(), _RecordingPool()
    pid = uuid4()

    started = await disp.dispatch_research(
        session=session,
        ledger=ledger,
        redis_pool=pool,
        agent_token_secret="x" * 32,
        project_id=pid,
        principal_user_id=uuid4(),
        actor_kind="user",
        topic="fasting",
        depth="deep",
    )

    # pending AgentRun added with the resolved allowlist
    assert len(session.added) == 1
    run = session.added[0]
    assert run.status == "pending"
    assert run.agent_kind == "deep_research"
    assert run.input_payload["allowed_connectors"] == ["openalex", "arxiv"]
    # ledger 'synthesize.dispatch' in the same unit of work
    assert [a["action_kind"] for a in ledger.appends] == ["synthesize.dispatch"]
    # committed BEFORE enqueue (row durable when the worker runs)
    assert session.committed == 1
    assert len(pool.jobs) == 1
    (job_args, _job_kwargs) = pool.jobs[0]
    assert job_args[0] == "deep_research_job"
    # correlation id is the FULL hex (no truncation → no collision)
    assert started.correlation_id == f"research-{started.agent_run_id.hex}"


async def test_dispatch_research_enqueue_failure_marks_run_failed(
    monkeypatch: pytest.MonkeyPatch,
):
    """If redis is unreachable after the run commits, the committed run must
    be marked failed (never left stranded as `pending`), and the error re-raised."""
    import aleph_research.dispatch as disp

    async def _enabled(*_a: Any, **_k: Any) -> list[str]:
        return ["openalex"]

    monkeypatch.setattr(disp, "resolve_enabled_kinds", _enabled)

    class _FailingPool:
        async def enqueue_job(self, *_a: Any, **_k: Any) -> None:
            raise ConnectionError("redis down")

    session = _RecordingSession()
    with pytest.raises(ConnectionError):
        await disp.dispatch_research(
            session=session,
            ledger=_RecordingLedger(),
            redis_pool=_FailingPool(),
            agent_token_secret="x" * 32,
            project_id=uuid4(),
            principal_user_id=uuid4(),
            actor_kind="user",
            topic="t",
        )
    run = session.added[0]
    assert run.status == "failed"
    assert run.completed_at is not None
    # committed twice: the durable pending row, then the failed mark
    assert session.committed == 2
