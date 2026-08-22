"""Every route whose URL names a project must resolve that project's scope.

Per-project scoping is the entire security story of this system. There is no
tenant boundary below it and none above it: a handler that accepts
`{project_id}` and does not run `project_scope_dep` will read and write another
tenant's rows for any caller who can guess a UUID, and it will do so with a 200.

That is not hypothetical here. The F1 defect was exactly this shape — the agent
endpoint took its project scope from a client-supplied thread id — and CLAUDE.md
files "every row carries project_id" under *Rules that are real but only held by
review*. This is the sweep that moves the routing half of it out of review.

What counts as scoped, and why each form is accepted:

  * a parameter annotated `ProjectScopeDep` (or `Depends(project_scope_dep)`) —
    the normal case, 116 handlers today;
  * `dependencies=[Depends(project_scope_dep)]` on the route decorator or on the
    `APIRouter` — idiomatic FastAPI, used by nothing today. Accepted anyway,
    because a sweep that rejects the framework's own spelling produces a false
    positive on the first person to use it, and the response to a false positive
    is to switch the sweep off;
  * an awaited `assert_stream_access(..., project_id, ...)` in the body. SSE
    routes cannot take `ProjectScopeDep`: it pulls in the request-scoped
    `SessionDep`, which stays checked out until the request ends, and an SSE
    request never ends — every open stream would pin one of the 30 pool
    connections for its whole life. `assert_stream_access` does the same
    membership and credential-scope check with a session it releases
    immediately. The `project_id` name must actually be passed to it; a call
    that scopes some *other* id is not a scope check for this route.

Anything else is an offender, reported with file, line and route template.

Deliberately NOT checked here: which ROLE a route requires. Several handlers
call `require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)` and
several do not, and which mutating routes need which role is a per-route
judgement with no record yet. Claiming to check it would be the overclaim this
sweep exists to prevent. See WS-P6's Iterate step.

Used by `scripts/check-project-scope.sh` and by
`tests/unit/test_project_scope_sweep.py` — one implementation, two callers, so
the gate does not depend on the test suite existing.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

from sweep_subject import MissingSubject

__all__ = [
    "ALLOWLIST",
    "Route",
    "offenders",
    "project_scoped_routes",
    "scan",
]

#: The decorator attributes that declare an HTTP route on a FastAPI router.
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})

#: The path parameter that makes a route project-scoped.
_PROJECT_PARAM = "{project_id}"

#: The names that scope a request, in any of the forms above.
_SCOPE_DEP_NAMES = frozenset({"ProjectScopeDep", "project_scope_dep"})
_STREAM_GUARD = "assert_stream_access"

#: Handlers exempted by name, each with the reason it is safe.
#:
#: Empty, and it should stay that way. An allowlist is where a sweep goes to
#: die: entries accumulate, the reasons go stale, and the number of exempt
#: routes becomes the number nobody looks at. Every entry must say what makes
#: the route safe WITHOUT the dependency — not why adding it was inconvenient.
#:
#: Key format: "<repo-relative file>::<function name>".
ALLOWLIST: dict[str, str] = {}


@dataclass(frozen=True, slots=True)
class Route:
    """One project-scoped route handler and how (or whether) it is scoped."""

    file: str
    line: int
    function: str
    path: str
    #: "ProjectScopeDep" | "dependencies=" | "assert_stream_access" | None
    scoped_by: str | None

    @property
    def key(self) -> str:
        return f"{self.file}::{self.function}"

    def __str__(self) -> str:
        return f"{self.file}:{self.line} {self.function}() — {self.path}"


def _router_prefixes(tree: ast.Module) -> dict[str, str]:
    """Local name of every `APIRouter(...)` → its `prefix=`.

    Needed because the `{project_id}` segment can live in the prefix rather than
    in the decorator. It does not today (every router prefixes `/v1/projects`
    and the decorators carry `/{project_id}/...`), but a router declared as
    `APIRouter(prefix="/v1/projects/{project_id}")` with bare `@router.get("")`
    handlers would otherwise be invisible to this sweep — which is the silent
    pass, not the false positive.
    """
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Name) and func.id == "APIRouter"):
            continue
        prefix = ""
        for keyword in node.value.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                prefix = str(keyword.value.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def _router_level_scope(tree: ast.Module) -> set[str]:
    """Routers whose own `dependencies=` already applies the scope to every route."""
    scoped: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Name) and func.id == "APIRouter"):
            continue
        for keyword in node.value.keywords:
            if keyword.arg == "dependencies" and _mentions_scope_dep(keyword.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        scoped.add(target.id)
    return scoped


def _mentions_scope_dep(node: ast.AST) -> bool:
    """True if `ProjectScopeDep` / `project_scope_dep` appears anywhere in `node`."""
    return any(
        isinstance(child, ast.Name) and child.id in _SCOPE_DEP_NAMES for child in ast.walk(node)
    )


def _guards_this_project(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the body awaits `assert_stream_access(...)` on THIS `project_id`.

    The id must be passed by that name. A stream that scope-checks a different
    identifier is not scoped — and "it calls the guard somewhere" is precisely
    the kind of shallow match that let `with_for_update(read=True)` past the
    first version of the page-lock sweep.
    """
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name != _STREAM_GUARD:
            continue
        passed = list(node.args) + [kw.value for kw in node.keywords]
        if any(isinstance(arg, ast.Name) and arg.id == "project_id" for arg in passed):
            return True
    return False


def _params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    args = func.args
    return [*args.posonlyargs, *args.args, *args.kwonlyargs]


def project_scoped_routes(source: str, filename: str) -> list[Route]:
    """Every route handler in `source` whose full path template names a project."""
    tree = ast.parse(source, filename=filename)
    prefixes = _router_prefixes(tree)
    router_scoped = _router_level_scope(tree)

    routes: list[Route] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            target = decorator.func
            if not (
                isinstance(target, ast.Attribute)
                and target.attr in _HTTP_METHODS
                and isinstance(target.value, ast.Name)
            ):
                continue
            router = target.value.id
            if router not in prefixes:
                # A `.get(...)` on something that is not a router in this module —
                # `dict.get`, an httpx client, a mock. Not a route.
                continue
            path = ""
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                path = str(decorator.args[0].value)
            full = prefixes[router] + path
            if _PROJECT_PARAM not in full:
                continue

            scoped_by: str | None = None
            if router in router_scoped:
                scoped_by = "APIRouter(dependencies=)"
            elif any(
                keyword.arg == "dependencies" and _mentions_scope_dep(keyword.value)
                for keyword in decorator.keywords
            ):
                scoped_by = "dependencies="
            elif any(
                param.annotation is not None and _annotation_is_scope(param.annotation)
                for param in _params(node)
            ):
                scoped_by = "ProjectScopeDep"
            elif _guards_this_project(node):
                scoped_by = _STREAM_GUARD

            routes.append(
                Route(
                    file=filename,
                    line=node.lineno,
                    function=node.name,
                    path=full,
                    scoped_by=scoped_by,
                )
            )
    return routes


def _annotation_is_scope(annotation: ast.expr) -> bool:
    """True for `ProjectScopeDep` and for `Annotated[UUID, Depends(project_scope_dep)]`."""
    return _mentions_scope_dep(annotation)


def offenders(routes: list[Route], allowlist: dict[str, str] | None = None) -> list[Route]:
    """Project-scoped routes that resolve no scope and are not allowlisted."""
    allowed = ALLOWLIST if allowlist is None else allowlist
    return [r for r in routes if r.scoped_by is None and r.key not in allowed]


@dataclass(frozen=True, slots=True)
class Report:
    routes: list[Route]
    offenders: list[Route]
    allowlisted: list[Route]

    @property
    def by_mechanism(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for route in self.routes:
            if route.scoped_by is not None:
                counts[route.scoped_by] = counts.get(route.scoped_by, 0) + 1
        return counts


def scan(repo_root: pathlib.Path, allowlist: dict[str, str] | None = None) -> Report:
    """Walk the API source tree and classify every project-scoped route.

    Scans all of `apps/api/src`, not just `routes/`. Every router lives under
    `routes/` today; scoping the sweep to that directory would mean the first
    router declared anywhere else is exempt by accident, which is the failure
    mode this whole workstream is about.
    """
    api_src = repo_root / "apps/api/src"
    if not api_src.is_dir():
        msg = (
            f"{api_src} is not there — it is the API source tree this sweep reads "
            "routes from, and with it gone the sweep finds no routes and reports "
            "every project scoped"
        )
        raise MissingSubject(msg)

    allowed = ALLOWLIST if allowlist is None else allowlist
    routes: list[Route] = []
    for path in sorted(api_src.rglob("*.py")):
        rel = str(path.relative_to(repo_root))
        routes.extend(project_scoped_routes(path.read_text(), rel))

    if not routes:
        # Zero project-scoped routes is not "clean", it is "the parser matched
        # nothing" — a decorator spelling this sweep does not recognise, or a
        # tree it is no longer pointed at. Either way it must not print a tick.
        msg = (
            f"{api_src} contains no project-scoped routes at all — this sweep is "
            "not parsing what it thinks it is parsing"
        )
        raise MissingSubject(msg)

    return Report(
        routes=routes,
        offenders=offenders(routes, allowed),
        allowlisted=[r for r in routes if r.scoped_by is None and r.key in allowed],
    )
