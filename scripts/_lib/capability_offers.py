"""Three lists that must agree: what Settings offers, what has a policy, what is called.

A `Capability` is only real if three separate things say so:

1. the Settings picker offers it (`CAPABILITIES` in
   `apps/api/src/aleph_api/routes/surfaces.py`),
2. `CAPABILITY_POLICIES` in `packages/aleph-models/src/aleph_models/discovery.py`
   says what a model must be able to do to serve it, so autoconfigure can bind
   one and the picker can filter the list, and
3. some code somewhere actually resolves it — `client.chat(capability=…)`,
   `client.embed(...)`, `resolve_binding(bindings, Capability.X)`.

Miss (2) and the picker offers a capability whose model list is empty and whose
binding autoconfigure will never make. Miss (3) and an operator carefully binds a
model to a job that nothing will ever ask for — a setting that looks like it
works, changes nothing, and cannot be observed to have changed nothing.

`rerank` was both. It had a policy (`mode="rerank"`), it was offered in the
picker, it had a help string — and Aleph contains no reranker at all, so every
`autoconfigure` run reported it permanently unbound and every operator learned
to ignore the unbound list.

The obvious check — `grep -rn '"rerank"' | wc -l` — is the reason this file
exists instead. When the picker lived in the web app's settings drawer,
`CAPABILITY_HELP` spelled the key **unquoted** (`rerank: "Reorders retrieved
chunks",`) and the grep never matched it: deleting the policy and the array
entry drove the count to its target while the orphan help text carried on
shipping. A count of matching text is not a statement about what the system does.

**WS-B1 moved list (1) from the browser to the server, and the parsers moved
with it.** The drawer became a pane, and a pane renders what the server sends —
so a capability list compiled into the client was exactly the copy of a
server-owned list that workstream removes. Both lists are now module constants
in the surface producer and are read with the AST rather than with a regex,
which retires the unquoted-key trap by construction: a Python dict key is a
string literal or it is a syntax error.

What did NOT change, and is why this file was not simply deleted: the two lists
are still spelled out by hand rather than derived from `CAPABILITY_POLICIES`.
Deriving them would make "offered but unbindable" and "bindable but not offered"
inexpressible, and therefore uncheckable — which sounds like a win until the
call-site check is the only one left standing.

Used by `tests/unit/test_capability_offers.py`. Runnable directly for a report:
``python3 scripts/_lib/capability_offers.py``.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
from dataclasses import dataclass

__all__ = [
    "KNOWN_UNRESOLVED",
    "Problem",
    "call_site_capabilities",
    "declared_capabilities",
    "help_capabilities",
    "offered_capabilities",
    "policy_capabilities",
    "run",
]

#: Capabilities that have a policy and an offer but no code that resolves them,
#: kept anyway. One entry, and it is a decision rather than a backlog:
#:
#: `vision` is here and `rerank` is not, and the difference is what each removal
#: costs. Dropping `rerank` cost nothing — Aleph has no reranker, no gateway it
#: has been pointed at reports `mode="rerank"`, and the policy's only observable
#: effect was a permanent entry in every autoconfigure run's `unbound` list.
#: Dropping `vision` would delete `CapabilityPolicy.needs_vision` — the filter
#: that stops a vision binding landing on a model that cannot read an image —
#: and the test that pins it, as a side effect of a workstream about rate
#: limiting. That is a product decision, so it is named here rather than made
#: silently in either direction.
#:
#: To close it: give `vision` a resolving call site, or remove it from
#: `CAPABILITY_POLICIES` and from `CAPABILITIES` / `CAPABILITY_HELP` in
#: `routes/surfaces.py` — then delete this entry, which
#: `tests/unit/test_capability_offers.py` will demand.
KNOWN_UNRESOLVED = frozenset({"vision"})

#: Where each of the three lists lives.
#:
#: This was `apps/web/src/components/Drawers.tsx` until WS-B1 deleted that file.
#: The constant was renamed with it on purpose: a sweep whose variable still says
#: `DRAWERS` after the drawer is gone is how the next reader concludes it is
#: still there.
OFFERS = "apps/api/src/aleph_api/routes/surfaces.py"
DISCOVERY = "packages/aleph-models/src/aleph_models/discovery.py"
ENUM = "packages/aleph-core/src/aleph_core/schemas/model_profile.py"

#: Files that mention `Capability.X` without resolving anything: the enum's own
#: definition, and the policy table itself. Counting the policy table as
#: evidence that a policy is used would make the check circular — every entry
#: would prove itself.
_NOT_CALL_SITES = (ENUM, DISCOVERY)

#: Source roots scanned for call sites. `tests/` is excluded everywhere: a
#: capability exercised only by its own test is exactly the orphan this sweep is
#: looking for, and `test_gateway_fake.py` passes `Capability.VISION` today.
_SOURCE_GLOBS = ("apps/*/src/**/*.py", "packages/*/src/**/*.py")

_CAPABILITY_REF = re.compile(r"\bCapability\.([A-Z][A-Z0-9_]*)\b")


@dataclass(frozen=True, slots=True)
class Problem:
    capability: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.capability}: {self.detail}"


def declared_capabilities(source: str) -> dict[str, str]:
    """`Capability` enum member name → value, read from the enum's own source."""
    out: dict[str, str] = {}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Capability":
            continue
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                out[stmt.targets[0].id] = stmt.value.value
    return out


def policy_capabilities(source: str, members: dict[str, str]) -> set[str]:
    """Capability VALUES keyed in `CAPABILITY_POLICIES`.

    AST rather than regex: the keys are `Capability.SYNTHESIS`-shaped attribute
    references, and the same words appear in this module's prose and in
    `candidates_for`'s comments.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target = node.targets[0].id
        if target != "CAPABILITY_POLICIES" or not isinstance(node.value, ast.Dict):
            continue
        out: set[str] = set()
        for key in node.value.keys:
            if isinstance(key, ast.Attribute) and key.attr in members:
                out.add(members[key.attr])
        return out
    return set()


def _module_assignment(source: str, name: str) -> ast.expr | None:
    """The value assigned to a module-level `name`, annotated or not.

    AST, not regex, and for a sharper reason than tidiness: `CAPABILITIES` and
    `CAPABILITY_HELP` also appear in that module's own prose — the comment
    explaining why the two lists are written out rather than derived from each
    other names both. A text search matches the explanation as well as the code.
    """
    tree = ast.parse(source)
    for node in tree.body:
        target: str | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target = node.targets[0].id
        if target == name:
            return node.value
    return None


def offered_capabilities(source: str) -> list[str]:
    """The picker's `CAPABILITIES` tuple, in order."""
    value = _module_assignment(source, "CAPABILITIES")
    if not isinstance(value, (ast.Tuple, ast.List)):
        return []
    return [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]


def help_capabilities(source: str) -> list[str]:
    """The keys of `CAPABILITY_HELP`, in order."""
    value = _module_assignment(source, "CAPABILITY_HELP")
    if not isinstance(value, ast.Dict):
        return []
    return [k.value for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]


def call_site_capabilities(root: pathlib.Path, members: dict[str, str]) -> dict[str, set[str]]:
    """Capability value → the source files that name it, excluding tests.

    Textual on purpose. A resolving call site is `capability=Capability.JUDGE`
    passed into `chat()`, `Capability.EMBEDDING` handed to `resolve_binding`, or
    any of the several wrappers in between; following the call graph through
    LangGraph nodes and arq jobs to prove it *reaches* `resolve_binding` would
    be a much larger claim with much more to go wrong. Naming the member outside
    the enum and the policy table is the honest, checkable proxy: nobody writes
    `Capability.RERANK` for decoration.
    """
    found: dict[str, set[str]] = {value: set() for value in members.values()}
    excluded = {(root / p).resolve() for p in _NOT_CALL_SITES}
    for glob in _SOURCE_GLOBS:
        for path in root.glob(glob):
            if path.resolve() in excluded or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name in _CAPABILITY_REF.findall(text):
                value = members.get(name)
                if value is not None:
                    found[value].add(str(path.relative_to(root)))
    return found


def run(root: pathlib.Path, *, exempt: frozenset[str] = frozenset()) -> list[Problem]:
    """Every disagreement between the three lists, most structural first.

    `exempt` suppresses `no-call-site` problems for capabilities a caller has
    decided to keep anyway. Nothing else is suppressible: an offer with no
    policy and an orphan help string are always defects.
    """
    members = declared_capabilities((root / ENUM).read_text(encoding="utf-8"))
    policies = policy_capabilities((root / DISCOVERY).read_text(encoding="utf-8"), members)
    offers = (root / OFFERS).read_text(encoding="utf-8")
    offered = offered_capabilities(offers)
    helped = help_capabilities(offers)
    call_sites = call_site_capabilities(root, members)
    values = set(members.values())

    problems: list[Problem] = []
    for value in offered:
        if value not in values:
            problems.append(
                Problem(
                    value,
                    "not-a-capability",
                    f"{OFFERS} offers it; there is no such Capability member",
                )
            )
        elif value not in policies:
            problems.append(
                Problem(
                    value,
                    "no-policy",
                    f"offered by {OFFERS} but absent from CAPABILITY_POLICIES — the picker "
                    f"lists no models for it and autoconfigure can never bind it",
                )
            )
    for value in sorted(policies - set(offered)):
        problems.append(
            Problem(
                value,
                "not-offered",
                f"has a policy but {OFFERS} does not offer it; nobody can bind it by hand",
            )
        )
    for value in sorted(set(helped) - set(offered)):
        problems.append(
            Problem(value, "orphan-help", f"help text in {OFFERS} for a capability nothing offers")
        )
    for value in sorted(set(offered) - set(helped)):
        problems.append(
            Problem(value, "no-help", f"offered by {OFFERS} with no entry in CAPABILITY_HELP")
        )
    for value in sorted(policies):
        if value in exempt:
            continue
        if not call_sites.get(value):
            problems.append(
                Problem(
                    value,
                    "no-call-site",
                    "has a policy and is offered, but no source file outside the enum and "
                    "the policy table resolves it — binding it changes nothing",
                )
            )
    return problems


def _main() -> int:
    root = pathlib.Path(__file__).resolve().parents[2]
    problems = run(root, exempt=KNOWN_UNRESOLVED)
    if problems:
        print("✗ capability offers: Settings, CAPABILITY_POLICIES and the call sites disagree")
        for problem in problems:
            print(f"   [{problem.kind}] {problem}")
        return 1
    # Printed, never hidden. An exemption nobody sees is an exemption nobody
    # prunes, and the test asserts this set is exactly the one still needed.
    exempted = ", ".join(sorted(KNOWN_UNRESOLVED)) or "none"
    print(
        "✓ capability offers: every offered capability has a policy and a caller "
        f"(documented exemptions: {exempted})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
