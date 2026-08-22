"""A skill the assistant writes for itself, and finds again tomorrow.

This is the smallest complete instance of the self-improvement loop Aleph is
for: the agent works something out, writes it down, and the next conversation
starts already knowing it.

**Why it did not work before.** Four skills ship in the container's source tree
and `/skills/` is a read-only `FilesystemBackend` with a blanket write deny —
correctly, because a skill is an instruction the model will follow and its
content can originate in a document the agent ingested. So there was nowhere
durable to put a new one. Anything the agent wrote landed in the container's own
filesystem: gone at the next deploy, invisible to the workers, and — because
`SkillsMiddleware.before_agent` returns early when `skills_metadata` is already
in state — not visible even in the conversation that wrote it.

**Three separate things have to hold, and each fails on its own.**

1. *A place to write.* `/skills/authored/` routes to a `StoreBackend` over the
   Postgres-backed langgraph store. `CompositeBackend` sorts routes
   longest-prefix-first (backends/composite.py:162), so the nested route wins
   over `/skills/` without any change to the parent.

2. *Permission to write there, and nowhere else.* `_check_fs_permission` is
   first-match-wins (middleware/filesystem.py:111-116), so the allow rule for
   `/skills/authored/**` must sit AHEAD of the deny on `/skills/**`. Reversed,
   it is inert and the whole feature is silently off — which is exactly the
   defect class this repo keeps producing, so `test_the_bundled_skills_stay_read_only`
   and `test_the_authored_route_is_writable` are asserted in the same run.

3. *Two sources, not one.* `skills=["/skills"]` never returns the store's
   skills, no matter how the route is set up: `_list_skills_with_errors` is
   called per source path and `/skills/` lists only what the filesystem backend
   holds. The sources list must name both. Measured through the composite:
   `/skills/` returns `['ach','report-authoring','research','wiki-style']` and
   never the authored one.

**And a fourth thing, which is why this file exists at all rather than being
three lines in `copilot_agent.py`.** The write has to be ledgered, and the
authoring conversation has to see its own skill. `SkillsMiddleware` loads the
list once per thread and never rescans, so without a refresh the agent writes a
skill and then cannot use it until the user starts a new conversation — which
reads as the feature not working.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from langchain.agents.middleware.types import AgentMiddleware

from aleph_core.ids import uuid7

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

_log = structlog.get_logger(__name__)

#: The one writable prefix inside `/skills/`. Everything else under `/skills/`
#: is the bundled, human-owned set and stays denied.
AUTHORED_PREFIX = "/skills/authored/"

#: Both agent-facing sources, in load order. `SkillsMiddleware` applies
#: last-one-wins on name collisions, so authored skills sit LAST deliberately:
#: an agent that improves on a bundled procedure should get its version, and the
#: bundled file is still there unmodified when someone wants to compare.
SKILL_SOURCES = ["/skills", "/skills/authored"]

#: Ledger action kind. `<entity>.<verb>` per the naming rule.
SKILL_AUTHORED = "skill.author"

#: The filesystem tools that can create a skill. Both map to the single "write"
#: operation in deepagents' permission model, and both have to be watched here
#: or an `edit_file` on an existing authored skill goes unledgered.
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})


def authored_namespace(_rt: object = None) -> tuple[str, ...]:
    """Where one project's authored skills live in the store.

    The same per-project scoping as `_memory_namespace`, and for the same
    reason: a skill is an instruction, so one project's authored skill appearing
    in another project's agent is not an inconvenience, it is a cross-tenant
    prompt injection with a durable store behind it.

    Falls back to a shared namespace when the project cannot be resolved, rather
    than to a default key that would collect every unscoped caller's skills into
    one bucket that everybody then reads.
    """
    from aleph_api.copilot_agent import _project_id_from_thread_id

    project_id = _project_for_current_config()
    if project_id is None:
        # Kept for the direct-caller case (a test, or a thread id with no project
        # prefix). Deliberately not the same shape as a real project id.
        _ = _project_id_from_thread_id  # imported for symmetry with _memory_namespace
        return ("shared", "skills")
    return (str(project_id), "skills")


def _project_for_current_config() -> UUID | None:
    from aleph_api.chat_runs import current_config
    from aleph_api.copilot_agent import _project_id_from_thread_id

    config = current_config()
    if config is None:
        return None
    configurable = config.get("configurable") or {}
    return _project_id_from_thread_id(configurable.get("thread_id"))


def is_authored_path(path: object) -> bool:
    """Whether a tool argument names a file under the writable skills route.

    Normalised, because `write_file` takes whatever the model typed. A model
    writing `/skills//authored/x/SKILL.md` or `/skills/authored/../ach/SKILL.md`
    must not slip past the ledger — and in the second case must not be treated
    as authored at all, since the permission layer is what stops the write and
    this function is what decides whether it gets recorded.
    """
    import posixpath

    if not isinstance(path, str) or not path:
        return False
    normalised = posixpath.normpath("/" + path.lstrip("/"))
    return normalised.startswith(AUTHORED_PREFIX.rstrip("/") + "/")


def skill_name_from_path(path: str) -> str | None:
    """`/skills/authored/<name>/SKILL.md` → `<name>`, or None.

    Guarded by `is_authored_path` rather than slicing off a fixed prefix length.
    Unguarded, `/skills/ach/SKILL.md` slices into the middle of the string and
    returns `.md` — a plausible-looking skill name for a path that is not an
    authored skill at all, which would then be what the ledger records.
    """
    import posixpath

    if not is_authored_path(path):
        return None
    normalised = posixpath.normpath("/" + path.lstrip("/"))
    remainder = normalised[len(AUTHORED_PREFIX) :]
    parts = [p for p in remainder.split("/") if p]
    return parts[0] if parts else None


async def ledger_authored_skill(
    session_maker: Callable[[], Any],
    *,
    project_id: UUID,
    actor_id: UUID,
    skill_name: str,
    path: str,
    tool: str,
) -> None:
    """One `ActionLedgerEvent` per authored write.

    A skill is state the agent added to itself. The standing rule is that every
    state mutation is ledgered in the same transaction as the mutation — this
    one cannot be, because the write goes to the langgraph store through a
    backend Aleph does not own and does not share a session with. That is a real
    gap and it is recorded here rather than papered over: the ledger row is
    written immediately after a *successful* write, so a store write that
    succeeds while this fails leaves a skill with no ledger row.

    The failure is one-directional on purpose. Ledgering first would produce the
    opposite and worse asymmetry — a recorded authorship for a skill that does
    not exist, which is evidence, and false.
    """
    from aleph_db.repos.ledger import LedgerWriter

    try:
        async with session_maker() as session:
            await LedgerWriter(session).append(
                project_id=project_id,
                actor_id=actor_id,
                actor_kind="agent",
                action_kind=SKILL_AUTHORED,
                target_id=uuid7(),
                target_kind="skill",
                payload={"skill": skill_name, "path": path, "tool": tool},
                trace_id=None,
            )
            await session.commit()
    except Exception:
        _log.exception("skill.author.ledger_failed", skill=skill_name, path=path)


async def refresh_skill_metadata(backend: object, sources: Sequence[str]) -> list[Any]:
    """Rescan every source and return the merged list, last-source-wins.

    Mirrors `SkillsMiddleware.before_agent`'s own merge exactly, because a
    refresh that ordered sources differently would let an authored skill win
    here and lose on the next turn — the same agent seeing two different
    procedures under one name, with nothing anywhere reporting it.
    """
    from deepagents.middleware.skills import _alist_skills_with_errors

    merged: dict[str, Any] = {}
    for source in sources:
        skills, error = await _alist_skills_with_errors(backend, source)  # ty: ignore
        if error is not None:
            _log.warning("skill.author.rescan_error", source=source, error=error)
        for skill in skills:
            merged[skill["name"]] = skill
    return list(merged.values())


class AuthoredSkillsMiddleware(AgentMiddleware):
    """Ledgers an authored skill, and makes the authoring turn able to use it.

    Deliberately a separate middleware from `AlephAgentMiddleware` rather than
    another branch inside it. That one exists to keep a turn alive when a tool
    throws; this one grants a durable capability to the agent. Merging them
    would put a security-relevant write path inside an error handler, where it
    would be read as incidental.
    """

    def __init__(
        self,
        *,
        session_maker: Callable[[], Any] | None = None,
        actor_id: UUID | None = None,
        sources: Sequence[str] = tuple(SKILL_SOURCES),
        backend_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._maker = session_maker
        self._actor_id = actor_id
        self._sources = list(sources)
        # The SAME factory `create_deep_agent` was given, not a captured
        # instance. deepagents resolves the backend by calling the factory with
        # a `ToolRuntime` (middleware/skills.py:845-859), and `awrap_tool_call`
        # is handed a real one — so calling it here resolves the identical
        # object the filesystem tools just wrote through. A captured instance
        # would be the wrong object the moment the backend becomes per-request,
        # and would silently rescan a different store.
        self._backend_factory = backend_factory
        super().__init__()

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        result = await handler(request)

        call = getattr(request, "tool_call", None) or {}
        tool = call.get("name")
        if tool not in _WRITE_TOOLS:
            return result
        args = call.get("args") or {}
        path = args.get("file_path") or args.get("path")
        if not is_authored_path(path):
            return result
        if _tool_failed(result):
            # A denied or failed write must not be ledgered as an authorship.
            return result

        skill_name = skill_name_from_path(str(path)) or "unknown"
        project_id = _project_for_current_config()
        if self._maker is not None and self._actor_id is not None and project_id is not None:
            await ledger_authored_skill(
                self._maker,
                project_id=project_id,
                actor_id=self._actor_id,
                skill_name=skill_name,
                path=str(path),
                tool=str(tool),
            )

        return await self._with_refreshed_metadata(request, result)

    def _backend(self, request: Any) -> object | None:
        if self._backend_factory is None:
            return None
        try:
            return self._backend_factory(getattr(request, "runtime", None))
        except Exception:
            _log.exception("skill.author.backend_unresolved")
            return None

    async def _with_refreshed_metadata(self, request: Any, result: Any) -> Any:
        """Attach a rescanned skill list to the tool result as a state update.

        Without this the agent writes a skill and then cannot use it until the
        user starts a new conversation, because `before_agent` short-circuits on
        `"skills_metadata" in state` — a feature that works and looks broken.
        """
        from langgraph.types import Command

        backend = self._backend(request)
        if backend is None:
            return result
        try:
            skills = await refresh_skill_metadata(backend, self._sources)
        except Exception:
            _log.exception("skill.author.refresh_failed")
            return result
        message = result if not isinstance(result, Command) else None
        if message is None:
            return result
        return Command(update={"messages": [message], "skills_metadata": skills})


def _tool_failed(result: object) -> bool:
    status = getattr(result, "status", None)
    return status == "error"
