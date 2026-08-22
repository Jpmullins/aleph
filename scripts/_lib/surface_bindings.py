"""Compare surface producers against the client bindings that consume them.

The A2UI binder resolves ONLY the props declared in a component's zod schema in
`apps/web/src/a2ui/aleph-catalog-v09.tsx`. A producer in
`packages/aleph-a2ui/.../surfaces.py` that emits `{"path": "/categories"}`
without a matching `categories` entry in that schema is dropped silently: the
SSE payload is correct, the view reads `undefined`, and nothing anywhere reports
an error.

That happened. The wiki surface shipped ten categories and a health summary, the
data model carried both, and the wiki rendered as though the project had no
categories at all. It is the dominant defect class in this codebase — a value
written correctly and read by nothing — with the added property that both halves
look right in isolation.

Used by `scripts/check-surface-bindings.sh` and by the test that proves the
sweep models reality.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
from dataclasses import dataclass

from sweep_subject import require_subject

__all__ = [
    "Mismatch",
    "Report",
    "catalog_components",
    "client_props",
    "compare",
    "producer_props",
    "run",
]

#: A prop declaration inside a `schema: z3.object({…})` block. Matches BOTH
#: spellings in use — `CommonSchemas.DynamicValue.optional()` and plain
#: `z3.any().optional()` — because what matters is whether the binder was told
#: about the prop, not which validator was chosen. Matching only the
#: `CommonSchemas.` form reported `GroundingSurface.claim` and `.groundings` as
#: undeclared when both are declared as `z3.any()`, which is the shape of
#: false positive that gets a sweep switched off.
_ZOD_PROP = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:CommonSchemas\.|z3\.)", re.MULTILINE)
_API_BLOCK = re.compile(
    r"export const (?P<name>\w+)Api = \{.*?schema: z3\.object\((?P<body>.*?)\),\s*\};",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class Mismatch:
    component: str
    prop: str
    reason: str

    def __str__(self) -> str:
        return f"{self.component}.{self.prop}: {self.reason}"


def producer_props(source: str) -> dict[str, set[str]]:
    """Component name → the prop names its Python producer binds.

    Reads the AST rather than the text: a binding is a dict literal key whose
    value is `{"path": ...}`, and only inside a dict that also names a
    `component`. Regex over the source would match the data-model keys too,
    which are the same words in the same file.
    """
    found: dict[str, set[str]] = {}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        component: str | None = None
        props: set[str] = set()
        for key, value in zip(node.keys, node.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            name = key.value
            if name == "component" and isinstance(value, ast.Constant):
                component = str(value.value)
            elif isinstance(value, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "path" for k in value.keys
            ):
                props.add(name)
        if component and props:
            found.setdefault(component, set()).update(props)
    return found


def client_props(source: str) -> dict[str, set[str]]:
    """Component name → the prop names its zod schema declares."""
    found: dict[str, set[str]] = {}
    for match in _API_BLOCK.finditer(source):
        name = match.group("name")
        found[name] = set(_ZOD_PROP.findall(match.group("body")))
    return found


def compare(producers: dict[str, set[str]], clients: dict[str, set[str]]) -> list[Mismatch]:
    """Every producer binding with no client declaration.

    One-directional on purpose. A client schema declaring a prop no producer
    sends is harmless — the view reads `undefined` and falls back — while the
    reverse is data that is computed, serialised, streamed, and discarded.
    Components with no client entry at all are skipped rather than reported:
    they are rendered somewhere else, and flagging them would train whoever
    runs this to ignore it.
    """
    out: list[Mismatch] = []
    for component, props in sorted(producers.items()):
        declared = clients.get(component)
        if declared is None:
            continue
        for prop in sorted(props - declared):
            out.append(
                Mismatch(
                    component,
                    prop,
                    "bound by the producer but not declared in the client zod "
                    "schema — the binder drops it and the view sees undefined",
                )
            )
    return out


@dataclass(frozen=True, slots=True)
class Report:
    """What the sweep found AND how much of the catalog it looked at.

    The second half is the point. This sweep printed `5 components, 11 bound
    props` for its whole life, which reads as "the catalog is clean" and means
    "five of the components in the catalog have a Python producer that binds a
    path, and those five are clean". The rest are the card components, bound
    from `cards.py`, which declares no path bindings at all — so the sweep has
    never looked at them. A coverage number that is *stated* invites somebody to
    raise it; a bare count invites nobody.
    """

    mismatches: list[Mismatch]
    compared: list[str]
    catalog_total: int
    bound_props: int

    @property
    def uncompared(self) -> int:
        return self.catalog_total - len(self.compared)


def catalog_components(catalog_json: str) -> set[str]:
    """The component names the canonical A2UI catalog declares.

    The denominator comes from `catalog.json` rather than from the client's zod
    file, because `catalog.json` is the one editable copy (CLAUDE.md rule 5) and
    the client file is one of the things that can drift away from it. Measuring
    coverage against the drifting side would make coverage look complete exactly
    when the two disagree.
    """
    parsed: object = json.loads(catalog_json)
    if not isinstance(parsed, dict):
        msg = "catalog.json is not a JSON object"
        raise ValueError(msg)
    components: object = parsed.get("components")
    if not isinstance(components, dict):
        msg = "catalog.json has no `components` object"
        raise ValueError(msg)
    return {str(name) for name in components}


#: The three files this sweep reads, with what it needs from each. Named once so
#: a move is reported against the path the sweep actually opened, and so the
#: existence check cannot drift out of step with the read.
_SUBJECTS: tuple[tuple[str, str], ...] = (
    (
        "packages/aleph-a2ui/src/aleph_a2ui/components/surfaces.py",
        "it is where the Python producers bind surface props; with it gone this "
        "sweep compares nothing and reports success",
    ),
    (
        "apps/web/src/a2ui/aleph-catalog-v09.tsx",
        "it holds the zod schemas the A2UI binder resolves against; with it gone "
        "every producer binding looks declared",
    ),
    (
        "packages/aleph-a2ui/src/aleph_a2ui/catalog.json",
        "it is the canonical catalog, and the denominator of this sweep's coverage number",
    ),
)


def run(repo_root: pathlib.Path) -> Report:
    """Compare producers against client declarations, and state the coverage.

    Raises `MissingSubject` if any subject file has moved. It used to return
    `[]` in that case — a fail-OPEN in the one place a fail-open is least
    affordable. The nonzero exit that made it look safe came from the wrapper
    re-reading the same path a few lines later and getting an unhandled
    `FileNotFoundError`; the sweep itself was reporting "no mismatches" about a
    file it had never opened, so reordering those two reads would have made a
    moved producer file a silent pass.
    """
    resolved = [require_subject(repo_root / name, why) for name, why in _SUBJECTS]
    producer_file, client_file, catalog_file = resolved
    producers = producer_props(producer_file.read_text())
    clients = client_props(client_file.read_text())
    catalog = catalog_components(catalog_file.read_text())

    compared = sorted(set(producers) & set(clients))
    bound = sum(len(producers[name]) for name in compared)
    return Report(
        mismatches=compare(producers, clients),
        compared=compared,
        catalog_total=len(catalog),
        bound_props=bound,
    )
