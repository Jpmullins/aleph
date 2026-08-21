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
import pathlib
import re
from dataclasses import dataclass

__all__ = ["Mismatch", "client_props", "compare", "producer_props"]

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


def run(repo_root: pathlib.Path) -> list[Mismatch]:
    producer_file = repo_root / "packages/aleph-a2ui/src/aleph_a2ui/components/surfaces.py"
    client_file = repo_root / "apps/web/src/a2ui/aleph-catalog-v09.tsx"
    if not producer_file.exists() or not client_file.exists():
        return []
    return compare(
        producer_props(producer_file.read_text()),
        client_props(client_file.read_text()),
    )
