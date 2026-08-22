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
from dataclasses import dataclass, field

from sweep_subject import require_subject

__all__ = [
    "Mismatch",
    "Report",
    "catalog_components",
    "catalog_props",
    "client_props",
    "compare",
    "compare_actions",
    "compare_agent_props",
    "compare_catalog_and_client",
    "compare_emitted",
    "emitted_actions",
    "emitted_props",
    "producer_props",
    "registered_actions",
    "run",
]

#: Keys that describe the component itself rather than one of its props.
_STRUCTURAL = frozenset({"id", "component", "type", "props", "children"})

#: A prop declaration inside a `schema: z3.object({…})` block. Matches BOTH
#: spellings in use — `CommonSchemas.DynamicValue.optional()` and plain
#: `z3.any().optional()` — because what matters is whether the binder was told
#: about the prop, not which validator was chosen. Matching only the
#: `CommonSchemas.` form reported `GroundingSurface.claim` and `.groundings` as
#: undeclared when both are declared as `z3.any()`, which is the shape of
#: false positive that gets a sweep switched off.
_ZOD_PROP = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:CommonSchemas\.|z3\.)", re.MULTILINE)
#: A prop declaration the binder can RESOLVE. `CommonSchemas.Dynamic*` and
#: `CommonSchemas.Action` are bindable scalars; anything typed `z3.*` is a
#: literal the binder passes through verbatim, which is correct for a Vega-Lite
#: spec and catastrophic for a prop the producer sends as `{"path": ...}`.
_ZOD_BINDABLE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*CommonSchemas\.", re.MULTILINE)
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


def emitted_props(source: str) -> dict[str, set[str]]:
    """Component name → every prop name this producer sends, bound OR literal.

    `producer_props` reads only `{"path": ...}` bindings, which is why this
    sweep looked at 7 of 23 catalog components for its whole life: the CARD
    producers send plain values, and a plain value the client's zod schema does
    not declare is dropped by the binder in exactly the same silence as a
    dropped path binding. `ApprovalCard` shipped `diff_card_id` and
    `view_diff_action` this way, and `chart_card` shipped `dataset_version_id`
    and `_placeholder` — four props computed, serialised and discarded, with the
    sweep printing "all declared" because it was not looking at cards at all.

    Four emission shapes exist and all four are read here:

      * ``{"id": ..., "component": "WikiSurface", "pages": {...}}`` — surfaces;
      * ``{"type": "WikiPageCard", "id": ..., "props": {...}}`` — the dict form,
        which the dossier composer and the pinning paths build by hand;
      * ``_card("ClaimCard", card_id=..., props={...})`` — `components/cards.py`;
      * ``_surface_messages(component_name="BriefsSurface", props={...})``.

    The `"type"` form requires a sibling `props` dict. Without that condition a
    Vega-Lite encoding — ``{"field": "x", "type": "nominal"}`` — reads as a
    component called `nominal`, and the sweep starts reporting components that
    do not exist, which is how a sweep gets switched off.
    """
    found: dict[str, set[str]] = {}

    def add(component: str | None, props: set[str]) -> None:
        if component and props:
            found.setdefault(component, set()).update(props)

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {
                k.value: v
                for k, v in zip(node.keys, node.values, strict=True)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            nested = keys.get("props")
            if "component" in keys and isinstance(keys["component"], ast.Constant):
                add(
                    str(keys["component"].value),
                    {k for k in keys if k not in _STRUCTURAL}
                    | (_string_keys(nested) if isinstance(nested, ast.Dict) else set()),
                )
            elif (
                "type" in keys
                and isinstance(keys["type"], ast.Constant)
                and isinstance(nested, ast.Dict)
            ):
                add(str(keys["type"].value), _string_keys(nested))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            named = {kw.arg: kw.value for kw in node.keywords}
            component: str | None = None
            if node.func.id == "_card" and node.args and isinstance(node.args[0], ast.Constant):
                component = str(node.args[0].value)
            elif node.func.id == "_surface_messages" and isinstance(
                named.get("component_name"), ast.Constant
            ):
                component = str(named["component_name"].value)  # type: ignore[union-attr]
            body = named.get("props")
            if component and isinstance(body, ast.Dict):
                add(component, _string_keys(body))
    return found


def _string_keys(node: ast.Dict) -> set[str]:
    return {
        str(k.value) for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


def client_props(source: str) -> dict[str, set[str]]:
    """Component name → the prop names its zod schema declares."""
    found: dict[str, set[str]] = {}
    for match in _API_BLOCK.finditer(source):
        name = match.group("name")
        found[name] = set(_ZOD_PROP.findall(match.group("body")))
    return found


def client_bindable_props(source: str) -> dict[str, set[str]]:
    """Component name → the props whose declaration the binder can RESOLVE.

    Declared and bindable are different things, and the sweep only checked the
    first. `GroundingSurface` and `InspectorSurface` both shipped with five
    bound props typed `z3.any()`: the binder classifies those STATIC and passes
    the value through verbatim, so the view received `{path: "/runs"}`,
    `runs.map` threw, and React unmounted the pane on every open. The sweep
    printed "all declared client-side" throughout.

    Reverting one prop to `z3.any()` reproduced it exactly: sweep green,
    browser red.
    """
    found: dict[str, set[str]] = {}
    for match in _API_BLOCK.finditer(source):
        found[match.group("name")] = set(_ZOD_BINDABLE.findall(match.group("body")))
    return found


def compare_bindability(
    producers: dict[str, set[str]], bindable: dict[str, set[str]]
) -> list[Mismatch]:
    """Every prop the producer binds by PATH whose declaration cannot resolve one.

    The second direction of the same contract. `compare` asks whether the
    client declares the prop at all; this asks whether it declared it as
    something the binder will resolve. Both failures are silent and produce the
    same symptom — the view reads `undefined`, or worse a raw `{path: ...}`
    object — so a sweep that checks only the first says "all declared" over a
    pane that cannot render.
    """
    out: list[Mismatch] = []
    for component, bound in sorted(producers.items()):
        declared = bindable.get(component)
        if declared is None:
            continue  # the component has no client schema; `compare` reports that
        for prop in sorted(bound - declared):
            out.append(
                Mismatch(
                    component=component,
                    prop=prop,
                    reason=(
                        "bound by the producer as a path, but the client "
                        "declares it with `z3.*` rather than `CommonSchemas.*`. "
                        "The binder passes a `z3.*` prop through VERBATIM, so "
                        "the view receives the {path: ...} object itself"
                    ),
                )
            )
    return out


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


def catalog_props(catalog_json: str) -> dict[str, set[str]]:
    """Component name → the props `catalog.json` declares for it.

    This is what the SERVER validates a component against, and it is a third
    copy of the same contract: producer, catalog, renderer. Two of the three
    were compared and the third drifted — nine props declared here that the
    renderer had never heard of (so the binder dropped them), and fourteen the
    renderer resolves that the catalog did not mention (so `validate_component`
    only tolerated them via `additionalProperties`). `WikiSurface` went further
    and made `view_mode` REQUIRED, a prop no producer has ever sent.
    """
    parsed: object = json.loads(catalog_json)
    if not isinstance(parsed, dict):
        msg = "catalog.json is not a JSON object"
        raise ValueError(msg)
    components: object = parsed.get("components")
    if not isinstance(components, dict):
        msg = "catalog.json has no `components` object"
        raise ValueError(msg)
    out: dict[str, set[str]] = {}
    for name, spec in components.items():
        node = spec
        for step in ("schema", "properties", "props", "properties"):
            node = node.get(step, {}) if isinstance(node, dict) else {}
        out[str(name)] = {str(k) for k in node} if isinstance(node, dict) else set()
    return out


def catalog_agent_props(catalog_json: str) -> dict[str, set[str]]:
    """Component name → the props the AGENT is invited to set.

    A separate block in `catalog.json`, and separately capable of naming a prop
    the renderer cannot resolve. `ApprovalCard.diff_card_id` and
    `ChartCard.dataset_version_id` were both offered to the model and dropped by
    the binder: the agent could set them, correctly, forever, and see nothing.
    """
    parsed = json.loads(catalog_json)
    components = parsed.get("components", {}) if isinstance(parsed, dict) else {}
    out: dict[str, set[str]] = {}
    for name, spec in components.items():
        agent = spec.get("agent") if isinstance(spec, dict) else None
        props = agent.get("props") if isinstance(agent, dict) else None
        block = props.get("properties") if isinstance(props, dict) else None
        if isinstance(block, dict):
            out[str(name)] = {str(k) for k in block}
    return out


def compare_emitted(emitted: dict[str, set[str]], clients: dict[str, set[str]]) -> list[Mismatch]:
    """Every prop a producer SENDS that the client's zod schema never declares.

    The generalisation of `compare` from path bindings to every prop. The binder
    resolves only what the schema names, so an undeclared literal is discarded
    exactly like an undeclared binding — the payload is right, the view reads
    `undefined`, nothing raises.
    """
    out: list[Mismatch] = []
    for component, props in sorted(emitted.items()):
        declared = clients.get(component)
        if declared is None:
            continue
        for prop in sorted(props - declared):
            out.append(
                Mismatch(
                    component,
                    prop,
                    "sent by a producer but not declared in the client zod "
                    "schema — the binder drops it and the view sees undefined",
                )
            )
    return out


def compare_catalog_and_client(
    catalog: dict[str, set[str]], clients: dict[str, set[str]]
) -> list[Mismatch]:
    """Both directions between `catalog.json` and the renderer's zod schemas.

    Both are real. A catalog prop the renderer does not declare is offered to
    producers and to the agent and then dropped; a renderer prop the catalog
    does not declare survives only because `additionalProperties` is true, so
    the file that is supposed to BE the contract does not contain it.

    `children` is exempt: it is structural, declared one level up in the catalog
    component schema, and the surfaces that forward child components inline all
    declare it in zod.
    """
    out: list[Mismatch] = []
    for component in sorted(set(catalog) & set(clients)):
        declared_client = clients[component] - {"children"}
        declared_catalog = catalog[component] - {"children"}
        for prop in sorted(declared_catalog - declared_client):
            out.append(
                Mismatch(
                    component,
                    prop,
                    "declared in catalog.json but not in the client zod schema "
                    "— offered to every producer and to the agent, and dropped "
                    "by the binder",
                )
            )
        for prop in sorted(declared_client - declared_catalog):
            out.append(
                Mismatch(
                    component,
                    prop,
                    "declared in the client zod schema but not in catalog.json "
                    "— the canonical contract does not contain a prop the "
                    "renderer resolves",
                )
            )
    return out


def compare_agent_props(agent: dict[str, set[str]], clients: dict[str, set[str]]) -> list[Mismatch]:
    """Every prop the agent is invited to set that never reaches a view.

    Compared against DECLARED props rather than bindable ones, and the
    difference is the whole reason this direction is separate from
    `compare_bindability`. An agent sets a prop as a literal, so `z3.any()` is
    the CORRECT declaration for the six whole-object props it supplies — a
    Vega-Lite spec, table rows and columns, form fields, evidence refs,
    citations. Requiring `CommonSchemas.*` there would report all six and be
    wrong about all six, which is how a sweep gets switched off.

    What is not survivable is a prop offered to the model and declared nowhere:
    `ApprovalCard.diff_card_id` and `ChartCard.dataset_version_id` were both in
    the agent block, and the binder dropped both.
    """
    out: list[Mismatch] = []
    for component in sorted(agent):
        declared = clients.get(component)
        if declared is None:
            continue
        for prop in sorted(agent[component] - declared):
            out.append(
                Mismatch(
                    component,
                    prop,
                    "offered to the agent in catalog.json but declared in no "
                    "client zod schema — the model can set it correctly "
                    "forever and the binder will drop it every time",
                )
            )
    return out


#: `r.register("<kind>", handler)` in `build_action_router()`.
_REGISTER = re.compile(r'\br\.register\(\s*"([^"]+)"')


def registered_actions(source: str) -> set[str]:
    """The action kinds `build_action_router()` will dispatch."""
    return set(_REGISTER.findall(source))


def emitted_actions(sources: dict[str, str]) -> dict[str, set[str]]:
    """Action kind → the files that can send it.

    An emitter is any literal use of the kind as the first argument to one of
    the three dispatchers a person or an agent can reach: `onAction("kind", …)`
    in a card view, `dispatchAction("kind", …)` in the chat surface's frontend
    tools, and `_dispatch_card_action_impl("kind", …)` in the agent's own
    composition verbs. Registration in `catalog.json` is NOT an emitter — that
    is the declaration being checked. Nor is a mention in a prompt: the agent
    is TOLD about `compose_dossier` in three places, and none of them would
    dispatch it.
    """
    out: dict[str, set[str]] = {}
    call = re.compile(r'(?:onAction|dispatchAction|_dispatch_card_action_impl)\(\s*"([^"]+)"')
    for name, text in sources.items():
        for kind in call.findall(text):
            out.setdefault(kind, set()).add(name)
    return out


def compare_actions(registered: set[str], emitters: dict[str, set[str]]) -> list[Mismatch]:
    """Every dispatchable action kind that nothing in the product can send.

    Three of twenty-one were unsendable: `clarify` (an echo handler that wrote
    nothing) and `mark_handedit` / `clear_handedit` (a second, ledger-poorer
    copy of `routes/handedits.py`). A registered action with no emitter reads as
    capability, cannot be exercised, and hides whatever is wrong with it until
    somebody finally calls it.

    `plugin.settings.save` is exempt: the generated settings card names it in a
    JSON Schema the browser renders, so the literal never appears in a TSX call.
    """
    exempt = {"plugin.settings.save"}
    return [
        Mismatch(
            "actions",
            kind,
            "registered on the ActionRouter but no card view or frontend tool "
            "sends it — a dispatchable verb nothing can dispatch",
        )
        for kind in sorted(registered - set(emitters) - exempt)
    ]


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
    #: Action kinds the router dispatches, and how many have an emitter.
    actions_total: int = 0
    #: Components a producer emits that the client's zod file does not name.
    #: The nine basic-catalog primitives (`Text`, `Button`, `TextField`, …) come
    #: from `@a2ui/react`'s own catalog, not from Aleph's zod file, so they are
    #: legitimately outside this comparison — and counted rather than hidden.
    unknown_to_client: list[str] = field(default_factory=list)

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
#: Every Python file that emits an A2UI component, and what each contributes.
#:
#: This was ONE file — `components/surfaces.py` — which is why the sweep
#: compared 7 of 23 catalog components and said so in a footnote nobody acted
#: on. The cards, the generated settings screen and the panes built in the route
#: file all emit components too, and every one of them can drop a prop in the
#: same silence.
_PRODUCERS: tuple[tuple[str, str], ...] = (
    (
        "packages/aleph-a2ui/src/aleph_a2ui/components/surfaces.py",
        "it is where the Python producers bind surface props; with it gone this "
        "sweep compares nothing and reports success",
    ),
    (
        "packages/aleph-a2ui/src/aleph_a2ui/components/cards.py",
        "it builds every inline card, with LITERAL props — the half of the "
        "contract this sweep could not see for its whole life",
    ),
    (
        "packages/aleph-a2ui/src/aleph_a2ui/settings_card.py",
        "it generates a plugin's settings screen out of a JSON Schema, so its "
        "props are written by a generator and read by nobody until they render",
    ),
    (
        "apps/api/src/aleph_api/routes/surfaces.py",
        "it builds the panes, including the error surface every failed pane falls back to",
    ),
    (
        "apps/api/src/aleph_api/a2ui_handlers.py",
        "the dossier composer builds a `WikiPageCard` payload by hand and "
        "validates it against catalog.json, which does not check the renderer",
    ),
    (
        "apps/api/src/aleph_api/subagents/viz_builder.py",
        "it pins agent-authored charts to Briefs by writing the prop dict by "
        "hand, which is where two undeclared props lived",
    ),
)

_SUBJECTS: tuple[tuple[str, str], ...] = (
    *_PRODUCERS,
    (
        "apps/web/src/a2ui/aleph-catalog-v09.tsx",
        "it holds the zod schemas the A2UI binder resolves against; with it gone "
        "every producer binding looks declared",
    ),
    (
        "packages/aleph-a2ui/src/aleph_a2ui/catalog.json",
        "it is the canonical catalog, the third copy of the prop contract, and "
        "the denominator of this sweep's coverage number",
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
    producer_files = resolved[: len(_PRODUCERS)]
    client_file, catalog_file = resolved[-2], resolved[-1]

    producers: dict[str, set[str]] = {}
    emitted: dict[str, set[str]] = {}
    for path in producer_files:
        source = path.read_text()
        for name, props in producer_props(source).items():
            producers.setdefault(name, set()).update(props)
        for name, props in emitted_props(source).items():
            emitted.setdefault(name, set()).update(props)

    client_source = client_file.read_text()
    catalog_source = catalog_file.read_text()
    # Every card view, plus the chat surface's frontend tools. A card that sends
    # an action lives under `a2ui/components`, and the sweep must read all of
    # them or a still-emitted action looks abandoned.
    view_sources = {
        str(path.relative_to(repo_root)): path.read_text()
        for path in sorted((repo_root / "apps/web/src").rglob("*.tsx"))
        if not path.name.endswith(".test.tsx")
    }
    # The agent's composition verbs are dispatched from Python, not from a card.
    for path in sorted((repo_root / "apps/api/src/aleph_api").rglob("*.py")):
        view_sources[str(path.relative_to(repo_root))] = path.read_text()
    router_source = require_subject(
        repo_root / "apps/api/src/aleph_api/a2ui_handlers.py",
        "it is where `build_action_router()` declares what the ActionRouter will dispatch",
    ).read_text()
    clients = client_props(client_source)
    bindable = client_bindable_props(client_source)
    catalog = catalog_components(catalog_source)

    compared = sorted(set(emitted) & set(clients))
    sent = sum(len(emitted[name]) for name in compared)
    return Report(
        # FOUR directions over one contract, because the contract has three
        # copies (producer, catalog.json, renderer zod) and each pair can drift
        # in a way that renders as an empty component and raises nothing.
        mismatches=(
            compare_emitted(emitted, clients)
            + compare_bindability(producers, bindable)
            + compare_catalog_and_client(catalog_props(catalog_source), clients)
            + compare_agent_props(catalog_agent_props(catalog_source), clients)
            + compare_actions(registered_actions(router_source), emitted_actions(view_sources))
        ),
        compared=compared,
        catalog_total=len(catalog),
        bound_props=sent,
        actions_total=len(registered_actions(router_source)),
        unknown_to_client=sorted(set(emitted) - set(clients)),
    )
