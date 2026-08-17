"""Bind an AG-UI agent request to projects the caller actually belongs to.

The Deep Agent's tools take their project scope from the *client-supplied*
thread id — `proj:<project_id>:<thread>`, parsed by
`copilot_agent._project_id_from_thread_id` — and then mint a self-call agent
token for whatever project that names. The graph never sees the HTTP principal,
so by the time a tool writes, the caller's identity is gone.

That makes the HTTP boundary the only place the binding can be enforced. This
module extracts every project a request names and refuses the request unless the
authenticated caller is a member of **all** of them.

Design notes:

* **Exhaustive, not best-effort.** Any channel the extractor misses is an open
  door, so it walks the whole JSON body for every key the agent will later read
  (`threadId`/`thread_id` prefixed ids, and explicit `projectId`/`project_id`),
  at any depth.
* **`NotFound`, not `PermissionDenied`** — matching `project_scope_dep`, so
  project existence never leaks across scopes.
* **Fails closed.** A body that cannot be parsed names no projects and is
  allowed through only because the tools then have no scope to act in; any
  project it *does* name is checked.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

from aleph_core.errors import NotFound

if TYPE_CHECKING:
    from collections.abc import Iterable

    from aleph_security.principal import Principal

#: Keys whose value is a project-prefixed thread id.
_THREAD_KEYS = ("threadId", "thread_id")
#: Keys whose value is a bare project UUID.
_PROJECT_KEYS = ("projectId", "project_id")


class MembershipCheck(Protocol):
    """`(user_id, project_id) -> bool`. Injected so this stays I/O-free."""

    async def __call__(self, user_id: UUID, project_id: UUID) -> bool: ...


def thread_project_id(thread_id: object) -> UUID | None:
    """Project UUID out of `proj:<uuid>:<thread>`, else None.

    Mirrors `copilot_agent._project_id_from_thread_id` deliberately: if the two
    ever disagree, the agent would act on a project this check never saw.
    `test_thread_parsers_agree` pins them together.
    """
    if not isinstance(thread_id, str) or not thread_id.startswith("proj:"):
        return None
    parts = thread_id.split(":", 2)
    if len(parts) < 2:
        return None
    try:
        return UUID(parts[1])
    except ValueError:
        return None


def _walk(node: object, out: set[UUID]) -> None:
    if isinstance(node, dict):
        mapping = cast("dict[object, object]", node)
        for key, value in mapping.items():
            if key in _THREAD_KEYS:
                pid = thread_project_id(value)
                if pid is not None:
                    out.add(pid)
            elif key in _PROJECT_KEYS and isinstance(value, str):
                with suppress(ValueError):
                    out.add(UUID(value))
            _walk(value, out)
    elif isinstance(node, list):
        for item in cast("list[object]", node):
            _walk(item, out)


def extract_project_ids(body: bytes) -> set[UUID]:
    """Every project the request names, at any depth."""
    if not body:
        return set()
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return set()
    found: set[UUID] = set()
    _walk(parsed, found)
    return found


async def assert_caller_may_use_projects(
    principal: Principal,
    project_ids: Iterable[UUID],
    is_member: MembershipCheck,
) -> None:
    """Refuse unless the caller is a member of every named project.

    All-or-nothing on purpose: checking only one of several named projects lets
    a caller smuggle an unauthorized project alongside an authorized one.
    """
    for project_id in project_ids:
        if principal.project_id is not None and principal.project_id != project_id:
            msg = f"project not found: {project_id}"
            raise NotFound(msg)
        if not await is_member(principal.user_id, project_id):
            msg = f"project not found: {project_id}"
            raise NotFound(msg)
