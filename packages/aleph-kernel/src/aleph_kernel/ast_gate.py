"""Static admission for agent-authored code.

A self-improving harness loads code its agent wrote. Importing a Python module
executes its top level, so by the time anything can inspect the module it has
already run — the gate has to happen on the *source*, before import.

The rule is narrow and decidable: **a module's top level must be
definition-only.** Imports, function and class definitions, literal constants,
docstrings, `__all__`, dataclass and typing machinery. No calls, no I/O, no
loops, no conditionals with side effects. Everything that does something lives
inside a function body, which runs only when the harness chooses to call it.

That is the discipline claude-science's skills follow, stated in their own
kernel modules: *"Module top level is definition-only (functions, imports,
literal constants) so the sidecar AST gate accepts it; everything that touches
the network or the host runtime happens inside a function body."*

WHAT THIS IS NOT. It is not a sandbox and not a capability check. A gated module
can still do anything Python can do once its functions are called — the kernel's
declared-capability mediation and the code-runner's container isolation are what
bound that. This gate buys one specific property: **loading is not running**, so
a plugin can be admitted, inspected, and rejected without its code having had a
turn. Treating it as a security boundary on its own would be a mistake.

It is deliberately conservative. A rejected module is a nuisance; an accepted
module that executes at import time defeats the entire admission sequence.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

__all__ = ["GateViolation", "check_source", "is_definition_only"]

#: Names a top-level assignment may call. These construct declarations rather
#: than perform work, and forbidding them would make ordinary typed modules
#: unwritable.
_ALLOWED_TOP_LEVEL_CALLS = frozenset(
    {
        "frozenset",
        "tuple",
        "set",
        "list",
        "dict",
        "field",
        "namedtuple",
        "NamedTuple",
        "TypeVar",
        "NewType",
        "Enum",
        "StrEnum",
        "IntEnum",
        "auto",
        "compile",  # re.compile — a literal pattern, not an action
        "getLogger",
        "get_logger",
    }
)

#: Modules an agent-authored skill may not import at all. Importing these is not
#: itself execution, but a skill has no legitimate use for them and their
#: presence is a strong signal about intent.
_FORBIDDEN_IMPORTS = frozenset(
    {"ctypes", "marshal", "pickle", "shelve", "subprocess", "multiprocessing"}
)


@dataclass(frozen=True)
class GateViolation:
    line: int
    reason: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.reason}"


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return None


def _check_assignment_target(target: ast.AST, line: int) -> list[GateViolation]:
    """A top-level assignment may bind a new name and nothing else."""
    if isinstance(target, ast.Name):
        return []
    if isinstance(target, ast.Tuple | ast.List):
        out: list[GateViolation] = []
        for element in target.elts:
            out.extend(_check_assignment_target(element, line))
        return out
    kind = "a subscript" if isinstance(target, ast.Subscript) else "an attribute"
    return [
        GateViolation(
            getattr(target, "lineno", line),
            f"top-level assignment to {kind} — that mutates something that "
            f"already exists. Move it into a function body.",
        )
    ]


def _check_value_expression(value: ast.AST, line: int) -> list[GateViolation]:
    """A top-level assignment's right-hand side must not do work."""
    violations: list[GateViolation] = []
    for node in ast.walk(value):
        name = _call_name(node)
        if name is not None and name not in _ALLOWED_TOP_LEVEL_CALLS:
            violations.append(
                GateViolation(
                    getattr(node, "lineno", line),
                    f"top-level call to {name}() — move it into a function body. "
                    f"Loading a skill must not run it.",
                )
            )
        if isinstance(node, ast.Await):
            violations.append(GateViolation(getattr(node, "lineno", line), "top-level await"))
    return violations


def _check_definition_header(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    violations: list[GateViolation],
) -> None:
    """Check the parts of a `def`/`class` that run when it is DEFINED.

    A function body runs when it is called, which is the whole basis of this
    gate. Its header does not: decorators are applied, default arguments are
    evaluated, and base classes are resolved at import time, in that order,
    before the module finishes loading.

    The gate used to `continue` past these nodes entirely, and its docstring
    argued the case for decorators specifically — that `@dataclass` and
    `@lru_cache(...)` only build a wrapper. True of those two, and the
    conclusion did not follow: `@d(os.makedirs("/tmp/x"))` builds a wrapper too,
    after making the directory.

    So decorators are still permitted, and their ARGUMENTS are checked.
    `@lru_cache(maxsize=128)` passes because 128 does nothing. That distinction
    is what the old comment wanted and could not express while skipping the node.
    """
    line = getattr(node, "lineno", 0)

    for decorator in node.decorator_list:
        # A bare `@dataclass` or `@app.route` names something already imported;
        # naming it is not doing anything. A CALL's arguments are evaluated
        # right here, so they are held to the same rule as any other top-level
        # expression.
        if isinstance(decorator, ast.Call):
            for arg in [*decorator.args, *(kw.value for kw in decorator.keywords)]:
                violations.extend(_check_value_expression(arg, line))
        elif not isinstance(decorator, ast.Name | ast.Attribute):
            violations.extend(_check_value_expression(decorator, line))

    if isinstance(node, ast.ClassDef):
        # `class C(build_base())` resolves its bases at class creation.
        for base in node.bases:
            violations.extend(_check_value_expression(base, line))
        for keyword in node.keywords:
            violations.extend(_check_value_expression(keyword.value, line))
        return

    # `def f(x=os.makedirs("/tmp/x"))` evaluates that default once, at def time.
    args = node.args
    defaults: list[ast.expr] = [*args.defaults, *[d for d in args.kw_defaults if d is not None]]
    for default in defaults:
        violations.extend(_check_value_expression(default, line))


def _check_statements(body: list[ast.stmt], violations: list[GateViolation]) -> None:
    """Apply the definition-only rule to a run of statements.

    Called for a module's top level and, recursively, for a CLASS body — which
    is not a special case but the same case: a class body is executed
    statement by statement when the class is created, exactly like a module's
    top level when it is imported.
    """
    for node in body:
        line = getattr(node, "lineno", 0)

        if isinstance(node, ast.Import | ast.ImportFrom):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                root = name.split(".", 1)[0]
                if root in _FORBIDDEN_IMPORTS:
                    violations.append(
                        GateViolation(line, f"import of {root!r} is not permitted in a skill")
                    )
            continue

        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            _check_definition_header(node, violations)
            continue

        if isinstance(node, ast.ClassDef):
            _check_definition_header(node, violations)
            # The body runs at class creation. Recursing is what stops
            # `class C: m = os.makedirs("/tmp/x")` — which the gate admitted.
            _check_statements(node.body, violations)
            continue

        if isinstance(node, ast.Pass):
            continue

        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            # The TARGET matters as much as the value. `os.environ['X'] = '1'`
            # has a constant right-hand side and mutates the process — checking
            # only the value misses it entirely. A top-level binding may name
            # something new; it may not reach into something that exists.
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                violations.extend(_check_assignment_target(target, line))
            value = node.value
            if value is not None:
                violations.extend(_check_value_expression(value, line))
            continue

        if isinstance(node, ast.Expr):
            # A bare string is a docstring. Anything else is an expression
            # evaluated for effect, which is the thing this gate exists to stop.
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            violations.append(GateViolation(line, "top-level expression evaluated for effect"))
            continue

        if isinstance(node, ast.If | ast.For | ast.While | ast.With | ast.AsyncWith | ast.Try):
            violations.append(
                GateViolation(
                    line,
                    f"top-level {type(node).__name__.lower()} — control flow at import time. "
                    f"Move it into a function.",
                )
            )
            continue

        violations.append(
            GateViolation(line, f"top-level {type(node).__name__} is not a definition")
        )


def check_source(source: str, *, filename: str = "<skill>") -> list[GateViolation]:
    """Return every reason ``source`` may not be loaded. Empty means admissible.

    Reports all violations rather than the first, so an author fixes a module in
    one pass instead of discovering problems one rejection at a time.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [GateViolation(exc.lineno or 0, f"syntax error: {exc.msg}")]

    violations: list[GateViolation] = []
    _check_statements(tree.body, violations)
    return violations


def is_definition_only(source: str, *, filename: str = "<skill>") -> bool:
    """True when ``source`` may be loaded. See :func:`check_source` for reasons."""
    return not check_source(source, filename=filename)
