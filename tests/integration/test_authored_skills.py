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
