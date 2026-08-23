"""WS-H1 — a skill the assistant writes for itself, and finds again tomorrow.

The smallest complete instance of the self-improvement loop: the agent works
something out, writes it down, and the next conversation starts already knowing
it. Four separate things have to hold and each fails silently on its own, which
is why these are six tests rather than one:

- a durable place to write (`/skills/authored/` → the Postgres store)
- permission to write THERE and nowhere else, in the right order
- the skills list naming BOTH sources, or the store's skills are never listed
- a ledger row, and a metadata refresh so the authoring turn can use it

`test_the_bundled_skills_stay_read_only` and `test_the_authored_route_is_writable`
are asserted in the same file on purpose. Each alone can be satisfied by
deleting the other's rule.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_api.authored_skills import (
    AUTHORED_PREFIX,
    SKILL_AUTHORED,
    SKILL_SOURCES,
    AuthoredSkillsMiddleware,
    is_authored_path,
    refresh_skill_metadata,
    skill_name_from_path,
)
from aleph_db.models.ledger import ActionLedgerEvent

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

SKILL_BODY = """---
name: probe-skill
description: A skill the agent wrote during a test.
---

Do the thing, then check the thing.
"""


def _composite(store: Any, namespace: tuple[str, ...]) -> Any:
    """The production backend shape, with the store namespace pinned.

    Built the same way `copilot_agent._memory_backend` builds it — same routes,
    same nesting — because a test over a differently-shaped composite would
    prove nothing about the one that ships.
    """
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend, StoreBackend

    from aleph_api.copilot_agent import _SKILLS_DIR

    return CompositeBackend(
        default=StateBackend(),
        routes={
            # A CALLABLE, never a tuple. `StoreBackend._get_namespace` calls it,
            # so a tuple raises `TypeError: 'tuple' object is not callable` at
            # the first write — the production `authored_namespace` is a
            # function for the same reason.
            "/skills/authored/": StoreBackend(store=store, namespace=lambda *_a: namespace),
            "/skills/": FilesystemBackend(root_dir=str(_SKILLS_DIR), virtual_mode=True),
        },
    )


async def _write_skill(backend: Any, name: str, body: str = SKILL_BODY) -> str:
    path = f"{AUTHORED_PREFIX}{name}/SKILL.md"
    result = backend.write(path, body)
    if hasattr(result, "__await__"):
        await result
    return path


def _req(file_path: str, tool: str = "write_file") -> Any:
    """A `ToolCallRequest`-shaped stand-in for a filesystem write."""

    class _Request:
        def __init__(self) -> None:
            self.tool_call = {
                "name": tool,
                "args": {"file_path": file_path},
                "id": "c1",
                "type": "tool_call",
            }
            self.runtime = None
            self.state: dict[str, Any] = {}

    return _Request()


async def _list(backend: Any, source: str) -> list[str]:
    from deepagents.middleware.skills import _alist_skills_with_errors

    skills, _error = await _alist_skills_with_errors(backend, source)
    return [s["name"] for s in skills]


@pytest.fixture
async def store() -> Any:
    """An in-memory langgraph store standing in for the Postgres-backed one.

    The thing under test is the ROUTING and the SOURCES, not the store's own
    durability — `AsyncPostgresStore` and `InMemoryStore` implement the same
    protocol, and langgraph tests the former. What this fixture cannot prove is
    survival across a process restart; `test_an_authored_skill_survives_the_thread`
    proves survival across a THREAD, which is the failure that was actually
    observed.
    """
    from langgraph.store.memory import InMemoryStore

    return InMemoryStore()


async def test_an_authored_skill_survives_the_thread(store: Any) -> None:
    """Written in one conversation, visible in another. THE criterion."""
    namespace = ("project-a", "skills")
    thread_a = _composite(store, namespace)
    await _write_skill(thread_a, "probe-skill")

    # A completely fresh backend — a new conversation, new state, same store.
    thread_b = _composite(store, namespace)
    assert "probe-skill" in await _list(thread_b, "/skills/authored")


async def test_one_source_is_not_enough(store: Any) -> None:
    """The specific mistake the "two sources, not one" correction exists to catch.

    Listing `/skills` returns the bundled four and NEVER the store's, however
    the route is configured — skills are listed per source path. Without this
    test, dropping `/skills/authored` from `skills=` is invisible: the agent
    simply never mentions the skill it just wrote, and that reads like the model
    being unhelpful rather than like a configuration error.
    """
    backend = _composite(store, ("project-a", "skills"))
    await _write_skill(backend, "probe-skill")

    from_bundled_only = await _list(backend, "/skills")
    assert "probe-skill" not in from_bundled_only
    assert from_bundled_only, "the bundled skills should still be there"

    merged = await refresh_skill_metadata(backend, SKILL_SOURCES)
    assert "probe-skill" in {s["name"] for s in merged}


async def test_project_a_cannot_read_project_bs_authored_skill(store: Any) -> None:
    """A skill is an instruction the model will follow.

    One project's authored skill appearing in another project's agent is not an
    inconvenience; it is a cross-tenant prompt injection with a durable store
    behind it.
    """
    await _write_skill(_composite(store, ("project-a", "skills")), "a-only-skill")
    b = _composite(store, ("project-b", "skills"))
    assert "a-only-skill" not in await _list(b, "/skills/authored")


async def test_the_authored_route_is_writable() -> None:
    """The allow rule matches, evaluated exactly as deepagents evaluates it."""
    from deepagents.middleware.filesystem import _check_fs_permission

    from aleph_api.copilot_agent import _agent_filesystem_permissions

    rules = _agent_filesystem_permissions()
    assert _check_fs_permission(rules, "write", "/skills/authored/probe-skill/SKILL.md") == "allow"


async def test_the_bundled_skills_stay_read_only() -> None:
    """...and the deny still matches everything else under /skills/.

    In the same file as the test above because each alone can be satisfied by
    deleting the other's rule. Order matters and is the whole mechanism:
    `_check_fs_permission` is first-match-wins, so an allow placed AFTER the
    deny is inert.
    """
    from deepagents.middleware.filesystem import _check_fs_permission

    from aleph_api.copilot_agent import _agent_filesystem_permissions

    rules = _agent_filesystem_permissions()
    for path in (
        "/skills/ach/SKILL.md",
        "/skills/research/SKILL.md",
        "/skills/wiki-style/SKILL.md",
        "/skills/report-authoring/SKILL.md",
    ):
        assert _check_fs_permission(rules, "write", path) == "deny", path


async def test_traversal_out_of_the_authored_route_is_rejected_upstream() -> None:
    """`/skills/authored/../ach/SKILL.md` — and where the defence actually is.

    Not in Aleph's rules. Measured: `_check_fs_permission` returns **allow** for
    that path, because the glob matcher does not normalise `..` and the string
    genuinely starts with `/skills/authored/`. The allow rule alone would let an
    agent rewrite a bundled skill.

    What stops it is `deepagents.backends.utils.validate_path`, which raises on
    any `..` or `~` before the permission check is reached. That is a real
    defence and it is sufficient — but it lives in a dependency, so this test
    pins it there rather than pretending Aleph checks it. A deepagents upgrade
    that relaxed traversal handling would go red here, which is the only reason
    the assertion is worth writing.
    """
    from deepagents.backends.utils import validate_path
    from deepagents.middleware.filesystem import _check_fs_permission

    from aleph_api.copilot_agent import _agent_filesystem_permissions

    traversal = "/skills/authored/../ach/SKILL.md"
    # Stated out loud so nobody "fixes" the rules believing this was covered.
    assert _check_fs_permission(_agent_filesystem_permissions(), "write", traversal) == "allow"
    with pytest.raises(ValueError, match=r"\.\."):
        validate_path(traversal)


async def test_every_authored_write_is_ledgered(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    from aleph_api.authored_skills import ledger_authored_skill

    await ledger_authored_skill(
        maker,
        project_id=committed_project,
        actor_id=ACTOR,
        skill_name="probe-skill",
        path=f"{AUTHORED_PREFIX}probe-skill/SKILL.md",
        tool="write_file",
    )
    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == committed_project,
                        ActionLedgerEvent.action_kind == SKILL_AUTHORED,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].payload_jsonb["skill"] == "probe-skill"


async def test_a_write_outside_the_authored_route_is_not_ledgered_as_authorship(
    maker: Callable[[], AsyncSession],
) -> None:
    """A recorded authorship for something that is not a skill is false evidence.

    `skill_name_from_path` used to slice a fixed prefix length off any path,
    so `/skills/ach/SKILL.md` produced the skill name `.md` — plausible enough
    to sit in the ledger unnoticed.
    """
    assert not is_authored_path("/skills/ach/SKILL.md")
    assert skill_name_from_path("/skills/ach/SKILL.md") is None
    assert not is_authored_path("/memories/notes.md")

    middleware = AuthoredSkillsMiddleware(session_maker=maker, actor_id=ACTOR)
    calls: list[str] = []

    async def handler(_req: Any) -> Any:
        from langchain_core.messages import ToolMessage

        calls.append("ran")
        return ToolMessage(content="ok", tool_call_id="c1", name="write_file")

    result = await middleware.awrap_tool_call(_req("/skills/ach/SKILL.md"), handler)
    assert calls == ["ran"]
    assert str(result.content) == "ok"


async def test_a_failed_write_is_not_ledgered_as_authorship(
    maker: Callable[[], AsyncSession], committed_project: uuid.UUID
) -> None:
    """A denied write must not produce an authorship row.

    The permission layer refuses by returning an error ToolMessage, not by
    raising, so a middleware that ledgers on "the handler returned" records an
    authorship for a write that never happened.
    """
    middleware = AuthoredSkillsMiddleware(session_maker=maker, actor_id=ACTOR)

    async def handler(_req: Any) -> Any:
        from langchain_core.messages import ToolMessage

        return ToolMessage(
            content="permission denied", tool_call_id="c1", name="write_file", status="error"
        )

    await middleware.awrap_tool_call(_req(f"{AUTHORED_PREFIX}nope/SKILL.md"), handler)
    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        # Scoped to THIS test's project. `action_ledger_events`
                        # is append-only by database trigger and the integration
                        # teardown deliberately leaves it alone, so an unscoped
                        # count here would be a count of every test that ever
                        # ran.
                        ActionLedgerEvent.project_id == committed_project,
                        ActionLedgerEvent.action_kind == SKILL_AUTHORED,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_the_authoring_session_sees_its_own_skill(store: Any) -> None:
    """WS-H1 criterion 2 — the half that made the feature look broken.

    `SkillsMiddleware` loads the skill list ONCE per thread: `before_agent`
    returns immediately when `skills_metadata` is already in state (asserted
    below, so a deepagents change that removed the short-circuit would tell us
    this mechanism is no longer needed rather than leaving it running for a
    reason that stopped being true). Without a refresh the agent writes a skill
    and then cannot use it until the user starts a new conversation — the write
    works, the ledger row is there, and the assistant behaves as though nothing
    happened.

    `_with_refreshed_metadata` is what closes it, and it had no test: the file's
    `test_the_authored_write_is_observed` only asserts the middleware is in the
    list, and `test_one_source_is_not_enough` calls `refresh_skill_metadata`
    directly. This drives `awrap_tool_call` — the hook that actually runs — and
    asserts the `Command` it returns carries the new skill.
    """
    from deepagents.middleware.skills import SkillsMiddleware
    from langchain_core.messages import ToolMessage
    from langgraph.types import Command

    backend = _composite(store, ("project-a", "skills"))
    path = f"{AUTHORED_PREFIX}probe-skill/SKILL.md"

    # The upstream behaviour this whole mechanism exists to work around.
    skills_mw = SkillsMiddleware(backend=backend, sources=list(SKILL_SOURCES))
    assert skills_mw.before_agent({"skills_metadata": []}, None, None) is None  # ty: ignore

    middleware = AuthoredSkillsMiddleware(backend_factory=lambda _rt: backend)

    async def handler(request: Any) -> Any:
        """The filesystem tool, doing what the filesystem tool does."""
        await _write_skill(backend, "probe-skill")
        return ToolMessage(content="ok", tool_call_id="c1", name="write_file")

    before = await refresh_skill_metadata(backend, SKILL_SOURCES)
    assert "probe-skill" not in {s["name"] for s in before}, (
        "the skill already existed; this test would pass without the refresh"
    )

    result = await middleware.awrap_tool_call(_req(path), handler)

    assert isinstance(result, Command), f"the tool result was not a state update: {result!r}"
    update = result.update
    assert isinstance(update, dict)
    names = {s["name"] for s in update["skills_metadata"]}
    assert "probe-skill" in names, names
    # The bundled skills are still there. A refresh that REPLACED the list with
    # only the authored source would satisfy the line above and silently remove
    # every skill the container ships.
    assert {"ach", "research"} <= names, names
    # And the tool's own message survives, or the model never sees the write
    # succeed.
    assert [str(m.content) for m in update["messages"]] == ["ok"]

    # What the model is actually handed. `skills_metadata` reaching state is
    # only useful because `SkillsMiddleware.modify_request` renders it into the
    # system prompt; asserting the dict key alone would pass for a payload the
    # prompt builder cannot read.
    rendered = skills_mw._format_skills_list(update["skills_metadata"])
    assert "probe-skill" in rendered, rendered


async def test_the_async_path_is_used_so_nothing_blocks_the_event_loop(store: Any) -> None:
    """WS-H1 criterion 5, measured rather than named.

    The criterion asserts `abefore_agent` is the exercised hook. There is no
    `abefore_agent` in `authored_skills.py` and there is not meant to be — the
    refresh happens at tool-call time, which is the only moment the authoring
    turn can still use what it just wrote. So what is left to check is the
    property behind it: a rescan runs a real directory listing and a real file
    read, and neither may happen on the event loop thread.

    Checked by recording `threading.get_ident()` inside the SYNC methods of the
    production composite's own routed backends, and asserting none of them ran
    on the loop's thread. `FilesystemBackend` and `StoreBackend` define no
    `als`/`adownload_files` of their own, so they inherit the protocol defaults,
    which are `asyncio.to_thread` wrappers — this asserts that resolution
    rather than reading it, so a future backend that implements `als` by calling
    `self.ls` inline goes red here.
    """
    import asyncio
    import threading

    backend = _composite(store, ("project-a", "skills"))
    await _write_skill(backend, "probe-skill")

    loop_thread = threading.get_ident()
    seen: dict[str, list[int]] = {}

    def record(target: Any, name: str) -> None:
        original = getattr(target, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            seen.setdefault(name, []).append(threading.get_ident())
            return original(*args, **kwargs)

        # Bound on the INSTANCE, so the protocol's async default still resolves
        # `self.ls` through this wrapper.
        target.__dict__[name] = wrapper

    for routed in backend.routes.values():
        record(routed, "ls")
        record(routed, "download_files")

    merged = await refresh_skill_metadata(backend, SKILL_SOURCES)
    assert "probe-skill" in {s["name"] for s in merged}

    assert seen.get("ls"), "no directory listing happened; the assertion below is vacuous"
    assert seen.get("download_files"), "no file was read; the assertion below is vacuous"
    offending = {name: ids for name, ids in seen.items() if loop_thread in ids}
    assert not offending, (
        f"{sorted(offending)} ran on the event loop thread — a blocking "
        f"filesystem or store call inside a FastAPI request"
    )

    # And the loop really was running, so "not the loop thread" means something.
    assert threading.get_ident() == loop_thread
    assert asyncio.get_running_loop() is not None


# ---------------------------------------------------------------------------
# The production namespace function, driven directly.
#
# `authored_namespace` had ZERO test coverage — its only mention anywhere in
# tests was a comment. The criterion it serves was checked by a test that built
# its OWN composite with `namespace=lambda *_a: namespace`, so it proved
# langgraph's store isolates two namespaces (a property of the library) and
# said nothing about Aleph's binding.
#
# Measured: making the production function return one constant namespace for
# every project left that test green. Its own docstring names the stake — "one
# project's authored skill appearing in another project's agent … is a
# cross-tenant prompt injection with a durable store behind it."


def _with_thread(thread_id: str | None) -> Any:
    """Run `authored_namespace` under a config carrying `thread_id`.

    Patches `current_config`, the one seam it reads, rather than standing up a
    graph — the property under test is the mapping from thread id to namespace.
    """
    from aleph_api import chat_runs
    from aleph_api.authored_skills import authored_namespace

    original = chat_runs.current_config
    chat_runs.current_config = lambda: (  # type: ignore[assignment]
        None if thread_id is None else {"configurable": {"thread_id": thread_id}}
    )
    try:
        return authored_namespace()
    finally:
        chat_runs.current_config = original  # type: ignore[assignment]


def test_two_projects_get_two_authored_namespaces() -> None:
    """The criterion, against the function production actually binds."""
    a, b = uuid.uuid4(), uuid.uuid4()
    ns_a = _with_thread(f"proj:{a}:t1")
    ns_b = _with_thread(f"proj:{b}:t1")

    assert ns_a == (str(a), "skills")
    assert ns_b == (str(b), "skills")
    assert ns_a != ns_b, (
        "both projects resolved to the same authored-skill namespace, so one "
        "project's agent reads another's instructions — a cross-tenant prompt "
        "injection with a durable store behind it"
    )


def test_the_same_project_is_stable_across_threads() -> None:
    """Scoping is by PROJECT, not by conversation — a skill outlives its thread."""
    p = uuid.uuid4()
    assert _with_thread(f"proj:{p}:t1") == _with_thread(f"proj:{p}:t2")


@pytest.mark.parametrize("thread_id", [None, "no-prefix", "proj:not-a-uuid:t1"])
def test_an_unresolvable_project_gets_the_shared_bucket_not_a_guess(thread_id: str | None) -> None:
    """An unscoped caller must not land in some real project's namespace.

    `("shared", "skills")` is deliberately not the shape of a project id, so a
    caller that cannot be resolved collects with other unresolved callers
    rather than silently joining a tenant.
    """
    assert _with_thread(thread_id) == ("shared", "skills")
