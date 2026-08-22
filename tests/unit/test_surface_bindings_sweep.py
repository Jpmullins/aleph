"""The surface-bindings sweep must model reality, not just run.

`scripts/check-surface-bindings.sh` is only worth having if it fires on the
defect it was written for and stays quiet otherwise. Its first run reported two
false positives — `GroundingSurface.claim` and `.groundings`, both declared as
`z3.any()` rather than `CommonSchemas.DynamicValue` — which is the shape of
mistake that gets a sweep switched off rather than fixed. These pin both
directions.
"""

from __future__ import annotations

import pathlib
import shutil

import pytest
from surface_bindings import (
    _SUBJECTS,
    catalog_components,
    catalog_props,
    client_bindable_props,
    client_props,
    compare,
    compare_actions,
    compare_agent_props,
    compare_bindability,
    compare_catalog_and_client,
    compare_emitted,
    compare_view_reads,
    emitted_actions,
    emitted_props,
    producer_props,
    registered_actions,
    run,
    strip_ts_comments,
    tsx_emitted_props,
    view_modules,
)
from sweep_subject import MissingSubject

PRODUCER = """
def wiki_surface_v09(*, pages, open_page=None, categories=None):
    component = {
        "id": "root",
        "component": "WikiSurface",
        "pages": {"path": "/pages"},
        "categories": {"path": "/categories"},
    }
    return full_surface(components=[component],
                        data_model={"pages": pages, "categories": categories})
"""

CLIENT_COMPLETE = """
export const WikiSurfaceApi = {
  name: "WikiSurface",
  schema: z3.object({
    pages: CommonSchemas.DynamicValue.optional(),
    categories: CommonSchemas.DynamicValue.optional(),
  }),
};
"""

CLIENT_MISSING = """
export const WikiSurfaceApi = {
  name: "WikiSurface",
  schema: z3.object({
    pages: CommonSchemas.DynamicValue.optional(),
  }),
};
"""


def test_a_bound_prop_with_no_client_declaration_is_reported() -> None:
    """The exact defect: the payload is correct and the view sees undefined."""
    found = compare(producer_props(PRODUCER), client_props(CLIENT_MISSING))
    assert [(m.component, m.prop) for m in found] == [("WikiSurface", "categories")]


def test_a_fully_declared_producer_is_clean() -> None:
    assert compare(producer_props(PRODUCER), client_props(CLIENT_COMPLETE)) == []


def test_the_data_model_keys_are_not_mistaken_for_bindings() -> None:
    """`data_model={"pages": ...}` names the same words as the bindings.

    A regex over the source would count those too, so every producer would look
    like it bound every key it sends. The AST walk only accepts a key whose
    value is a `{"path": ...}` dict inside a dict that also names a component.
    """
    props = producer_props(PRODUCER)
    assert props == {"WikiSurface": {"pages", "categories"}}


def test_a_zod_any_declaration_counts_as_declared() -> None:
    """The false positive from the sweep's first run.

    What matters is whether the binder was told about the prop, not which
    validator the author reached for.
    """
    client = """
export const GroundingSurfaceApi = {
  name: "GroundingSurface",
  schema: z3.object({
    claim: z3.any().optional(),
    groundings: z3.any().optional(),
  }),
};
"""
    producer = """
def grounding_surface(*, claim, groundings):
    component = {
        "id": "root",
        "component": "GroundingSurface",
        "claim": {"path": "/claim"},
        "groundings": {"path": "/groundings"},
    }
"""
    assert compare(producer_props(producer), client_props(client)) == []


def test_a_component_the_client_never_mentions_is_skipped() -> None:
    """Rendered elsewhere. Reporting it would train people to ignore the sweep."""
    assert compare({"SomethingElse": {"x"}}, client_props(CLIENT_COMPLETE)) == []


def test_the_real_tree_is_clean() -> None:
    """The sweep's own subject, so a regression fails here and not only in CI."""
    root = pathlib.Path(__file__).resolve().parents[2]
    assert run(root).mismatches == []


def test_a_moved_subject_file_raises_instead_of_reporting_clean(
    tmp_path: pathlib.Path,
) -> None:
    """The fail-open that made this sweep worth re-reading.

    `run()` used to return `[]` when either subject was missing. It looked safe
    only because the wrapper re-read the same path a few lines later and died on
    an unhandled `FileNotFoundError` — so the *analyzer* was answering "no
    mismatches" about a file it had never opened, and reordering those two reads
    would have turned a moved producer into a silent pass. Three sweeps in this
    repo have already gone quiet by naming a path that moved.
    """
    with pytest.raises(MissingSubject) as caught:
        run(tmp_path)
    # The message must name the path, or the failure is "the sweep crashed"
    # rather than "your subject moved" — and the usual answer to the first is to
    # delete the sweep.
    assert "surfaces.py" in str(caught.value)


def test_the_coverage_denominator_is_the_canonical_catalog() -> None:
    """`5 components` reads as complete; `5 of N` reads as coverage.

    The denominator comes from `catalog.json` — the one editable copy — not from
    the client zod file, which is one of the two things that can drift.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    report = run(root)
    catalog = catalog_components(
        (root / "packages/aleph-a2ui/src/aleph_a2ui/catalog.json").read_text()
    )
    assert report.catalog_total == len(catalog)
    # `uncompared` was > 0 for as long as "compared" meant "has a Python
    # producer". That is the input to two of the five directions, and the
    # component it excluded — `ArtifactCard` — is read by the other three. It is
    # 0 now, and that is a claim with teeth only because a component NO
    # direction examines is a mismatch rather than an entry in a footnote:
    # see `test_a_producerless_component_nothing_examines_is_a_mismatch`.
    assert report.uncompared == 0
    assert set(report.compared) <= catalog


# ---------------------------------------------------------------------------
# Declared and BINDABLE are different things
# ---------------------------------------------------------------------------


_CLIENT_WITH_A_LITERAL = """
export const InspectorSurfaceApi = {
  name: "InspectorSurface",
  schema: z3.object({
    runs: z3.any().optional(),
    selected: CommonSchemas.DynamicValue.optional(),
  }),
};
"""

_PRODUCER_BINDING_BOTH = """
def _messages():
    return [{"component": "InspectorSurface",
             "runs": {"path": "/runs"},
             "selected": {"path": "/selected"}}]
"""


def test_a_bound_prop_typed_as_a_literal_is_reported() -> None:
    """The defect that shipped twice, in the shape it shipped.

    `GroundingSurface` and `InspectorSurface` each declared five bound props as
    `z3.any()`. The binder classifies those STATIC and passes them through
    VERBATIM, so the view received `{path: "/runs"}`, `runs.map` threw, and
    React unmounted the pane on every open — while the sweep printed
    "all declared client-side", because the prop WAS declared. Declared and
    resolvable are not the same claim.
    """
    producers = producer_props(_PRODUCER_BINDING_BOTH)
    bindable = client_bindable_props(_CLIENT_WITH_A_LITERAL)
    problems = compare_bindability(producers, bindable)

    assert [f"{m.component}.{m.prop}" for m in problems] == ["InspectorSurface.runs"], (
        "the z3.any() prop was not reported, or the CommonSchemas one was"
    )
    assert "verbatim" in problems[0].reason.lower()


def test_the_old_direction_would_not_have_caught_it() -> None:
    """States plainly why a second direction was needed rather than a tweak.

    `compare` sees `runs` declared and is satisfied. Both checks now run, and
    this is the evidence that one of them was not enough.
    """
    producers = producer_props(_PRODUCER_BINDING_BOTH)
    declared = client_props(_CLIENT_WITH_A_LITERAL)
    assert compare(producers, declared) == [], (
        "the declaration-only check now reports this too, so this test no "
        "longer demonstrates anything — delete it or fix the premise"
    )


def test_a_literal_prop_no_producer_binds_is_not_reported() -> None:
    """`z3.any()` is CORRECT for a Vega-Lite spec, table columns, graph edges.

    Without this the fix would be "ban z3.* everywhere", which would force real
    literal props to be declared as bindings and break them the other way.
    """
    client = """
export const ChartCardApi = {
  name: "ChartCard",
  schema: z3.object({
    title: CommonSchemas.DynamicString,
    vega_lite_spec: z3.any().optional(),
  }),
};
"""
    producer = """
def _messages():
    return [{"component": "ChartCard", "title": {"path": "/t"}}]
"""
    assert compare_bindability(producer_props(producer), client_bindable_props(client)) == []


# ---------------------------------------------------------------------------
# The other two copies of the contract
# ---------------------------------------------------------------------------
#
# WS-UI-3. `producer_props` reads only `{"path": ...}` bindings, so the sweep
# compared 7 of 23 catalog components for its whole life and printed the gap in
# a footnote. The cards send LITERAL props and drop them in exactly the same
# silence — `ApprovalCard.diff_card_id`, `ApprovalCard.view_diff_action`,
# `ChartCard.dataset_version_id`, `ChartCard._placeholder` and
# `WikiPageCard.dossier_refs` were all live when this was written.


_CARDS = """
def _card(type_name, *, card_id, props):
    return {"type": type_name, "id": card_id, "props": props}


def approval_card(p, *, card_id=None):
    return _card(
        "ApprovalCard",
        card_id=card_id,
        props={"title": p.title, "diff_card_id": p.diff_card_id},
    )
"""

_CARDS_CLIENT = """
export const ApprovalCardApi = {
  name: "ApprovalCard",
  schema: z3.object({
    title: CommonSchemas.DynamicString,
  }),
};
"""


def test_a_literal_card_prop_the_client_never_declares_is_reported() -> None:
    """The half of the contract the sweep could not see.

    `_card(...)` sends plain values, not `{"path": ...}`, so `producer_props`
    returns nothing at all for this file and `compare` had nothing to compare.
    """
    assert producer_props(_CARDS) == {}
    found = compare_emitted(emitted_props(_CARDS), client_props(_CARDS_CLIENT))
    assert [f"{m.component}.{m.prop}" for m in found] == ["ApprovalCard.diff_card_id"]


def test_a_vega_encoding_is_not_mistaken_for_a_component() -> None:
    """`{"field": "x", "type": "nominal"}` is not a component called `nominal`.

    The `"type"` emission shape needs a sibling `props` dict or a Vega-Lite spec
    inside a producer starts inventing components — and a sweep that reports
    components which do not exist is a sweep somebody switches off.
    """
    spec = """
def chart():
    return {"mark": "bar", "encoding": {"x": {"field": "a", "type": "nominal"}}}
"""
    assert emitted_props(spec) == {}


def test_a_surface_built_through_the_keyword_helper_is_seen() -> None:
    """`briefs_surface_v09` names its component in a kwarg, not a dict key."""
    source = """
def briefs_surface_v09(*, badge_count=0, children=None, surface_id="briefs"):
    return _surface_messages(
        surface_id=surface_id,
        component_name="BriefsSurface",
        props={"badge_count": badge_count},
        children=children or [],
    )
"""
    assert emitted_props(source) == {"BriefsSurface": {"badge_count"}}


_CATALOG_DRIFT = """
{"components": {"WikiSurface": {"schema": {"properties": {"props": {"properties": {
  "view_mode": {}, "pages": {}}}}}}}}
"""

_CATALOG_DRIFT_CLIENT = """
export const WikiSurfaceApi = {
  name: "WikiSurface",
  schema: z3.object({
    pages: CommonSchemas.DynamicValue.optional(),
    health: CommonSchemas.DynamicValue.optional(),
  }),
};
"""


def test_catalog_and_renderer_drift_is_reported_in_both_directions() -> None:
    """Nine props one way and fourteen the other were live when this was added.

    Both directions matter. A catalog prop the renderer never declares is
    offered to every producer and to the agent and then dropped; a renderer prop
    the catalog omits survives only on `additionalProperties`, so the file that
    is supposed to BE the contract does not contain it.
    """
    found = compare_catalog_and_client(
        catalog_props(_CATALOG_DRIFT), client_props(_CATALOG_DRIFT_CLIENT)
    )
    assert sorted(f"{m.component}.{m.prop}" for m in found) == [
        "WikiSurface.health",
        "WikiSurface.view_mode",
    ]


def test_children_is_not_reported_as_drift() -> None:
    """Structural, declared one level up in the catalog component schema.

    Without the exemption every surface that forwards child components inline
    reports a mismatch, which is four false positives out of the box.
    """
    catalog = (
        '{"components": {"BriefsSurface": {"schema": {"properties":'
        ' {"props": {"properties": {"badge_count": {}}}}}}}}'
    )
    client = """
export const BriefsSurfaceApi = {
  name: "BriefsSurface",
  schema: z3.object({
    badge_count: CommonSchemas.DynamicNumber.optional(),
    children: z3.array(z3.any()).optional(),
  }),
};
"""
    assert compare_catalog_and_client(catalog_props(catalog), client_props(client)) == []


def test_a_prop_offered_to_the_agent_that_reaches_no_view_is_reported() -> None:
    """`ApprovalCard.diff_card_id` and `ChartCard.dataset_version_id`, exactly.

    The agent block is a separate declaration and can name a prop the renderer
    has never heard of. The model then sets it, correctly, forever, and sees
    nothing.
    """
    agent = {"ChartCard": {"title", "vega_lite_spec", "dataset_version_id"}}
    found = compare_agent_props(agent, client_props(_CHART_CLIENT))
    assert [f"{m.component}.{m.prop}" for m in found] == ["ChartCard.dataset_version_id"]


_CHART_CLIENT = """
export const ChartCardApi = {
  name: "ChartCard",
  schema: z3.object({
    title: CommonSchemas.DynamicString.optional(),
    vega_lite_spec: z3.any().optional(),
  }),
};
"""


def test_an_agent_prop_declared_as_a_literal_is_NOT_reported() -> None:
    """An agent supplies whole objects, so `z3.any()` is the right declaration.

    Comparing the agent block against BINDABLE props instead of declared ones
    reported all six whole-object props — a Vega-Lite spec, table rows and
    columns, form fields, evidence refs, citations — and was wrong about every
    one. That is the shape of false positive that gets a sweep switched off.
    """
    agent = {"ChartCard": {"vega_lite_spec"}}
    assert compare_agent_props(agent, client_props(_CHART_CLIENT)) == []


def test_the_sweep_now_covers_the_cards_it_never_looked_at() -> None:
    """Coverage is a number this sweep states, and it must not silently fall.

    7 of 23 when this was written, because only path bindings counted. The
    floor is deliberately below today's 18 so that adding a component to the
    catalog does not fail this test, and deliberately above 7 so that reverting
    to path-bindings-only does.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    report = run(root)
    assert len(report.compared) >= 20, report.compared
    assert report.bound_props >= 80, report.bound_props
    assert report.props_inspected >= 95, report.props_inspected
    # The card components specifically — the ones the old sweep could not see.
    assert {"ApprovalCard", "ClaimCard", "SourceCard", "TableCard"} <= set(report.compared)
    # And the three the sweep gained when it stopped assuming a producer is
    # Python: `ImageCard` and `HtmlFrameCard` are pinned from a worker,
    # `NoteEditorCard` and `HtmlDocCard` are built in the browser.
    assert {"ImageCard", "HtmlFrameCard", "NoteEditorCard", "HtmlDocCard"} <= set(report.compared)


# ---------------------------------------------------------------------------
# The other half of the contract: the actions
# ---------------------------------------------------------------------------


def test_a_registered_action_nothing_can_send_is_reported() -> None:
    """Three of twenty-one, when this was written.

    `clarify` returned `answer_length` and wrote nothing; `mark_handedit` and
    `clear_handedit` duplicated `routes/handedits.py` and skipped the ledger row
    that route writes. All three were reachable only by hand-crafting a POST.
    """
    registered = registered_actions("""
def build_action_router():
    r.register("approve", _approve)
    r.register("clarify", _clarify)
""")
    emitters = emitted_actions({"Card.tsx": 'onAction("approve", {x: 1})'})
    found = compare_actions(registered, emitters)
    assert [m.prop for m in found] == ["clarify"]


def test_an_agent_dispatched_verb_counts_as_an_emitter() -> None:
    """`compose_dossier` and `spotlight` are sent from PYTHON, not from a card.

    Scanning only `.tsx` reported both, and both are live. A sweep that reports
    working code is a sweep somebody switches off — which is why the emitter
    set spans all three dispatchers.
    """
    registered = registered_actions('r.register("compose_dossier", _compose_dossier)')
    emitters = emitted_actions(
        {"copilot_agent.py": '_dispatch_card_action_impl("compose_dossier", {}, config)'}
    )
    assert compare_actions(registered, emitters) == []


def test_a_prompt_mentioning_an_action_is_not_an_emitter() -> None:
    """The agent is TOLD about `compose_dossier` in three prompts. Telling it is
    not dispatching it, and a substring search would have counted all three."""
    registered = registered_actions('r.register("compose_dossier", _compose_dossier)')
    emitters = emitted_actions({"copilot_agent.py": '"`compose_dossier` groups pages"'})
    assert [m.prop for m in compare_actions(registered, emitters)] == ["compose_dossier"]


def test_every_registered_action_in_the_real_tree_has_an_emitter() -> None:
    root = pathlib.Path(__file__).resolve().parents[2]
    report = run(root)
    assert [m for m in report.mismatches if m.component == "actions"] == []
    # And the router still HAS actions — a router that registered nothing would
    # satisfy the assertion above trivially.
    assert report.actions_total >= 15, report.actions_total


# ---------------------------------------------------------------------------
# The fifth direction: zod → view
# ---------------------------------------------------------------------------
#
# The other four all end at the browser's front door. This one walks the last
# step: the binder resolves a declared prop, hands it to the view, and the view
# can still ignore it. Thirty props were in that state when this was written,
# including `FindingCard.evidence_refs` — read out of
# `ReviewFinding.evidence_refs_jsonb`, sent, declared, resolved, and not
# destructured, so every finding rendered with no evidence under it.


_VIEW_CLIENT = """
export const FindingCardApi = {
  name: "FindingCard",
  schema: z3.object({
    summary: CommonSchemas.DynamicString,
    evidence_refs: z3.array(z3.any()).optional(),
    approve_action: CommonSchemas.Action.optional(),
    children: z3.array(z3.any()).optional(),
  }),
};
"""

_VIEW_READS_EVERYTHING = """
export function FindingCard({ component, onAction }: RendererProps) {
  const p = component.props as { summary: string; evidence_refs?: unknown[] };
  return <button onClick={() => onAction("approve", {})}>{p.summary}</button>;
}
"""


def _views(source: str) -> dict[str, tuple[str, str]]:
    return {"FindingCard": ("components/FindingCard.tsx", source)}


_CONSTS = {"FindingCard": {"approve_action": "approve"}}


def test_a_declared_prop_the_view_never_reads_is_reported() -> None:
    """The defect, in the shape it shipped.

    Every earlier direction is satisfied: the producer sends it, the catalog
    declares it, the zod schema declares it, the binder resolves it. The value
    then arrives in a component that does not mention the word.
    """
    blind = _VIEW_READS_EVERYTHING.replace("; evidence_refs?: unknown[]", "")
    found = compare_view_reads(client_props(_VIEW_CLIENT), _CONSTS, _views(blind))
    assert [f"{m.component}.{m.prop}" for m in found] == ["FindingCard.evidence_refs"]
    assert "never looks" in found[0].reason


def test_a_view_that_reads_every_declared_prop_is_clean() -> None:
    assert (
        compare_view_reads(client_props(_VIEW_CLIENT), _CONSTS, _views(_VIEW_READS_EVERYTHING))
        == []
    )


def test_a_const_action_prop_the_view_hardcodes_is_not_a_gap() -> None:
    """Twenty-one props are pinned to a `const` in catalog.json.

    The value is fixed by the contract, so the prop carries no information and
    `onAction("approve", …)` is EQUIVALENT to reading it. Reporting these would
    have been twenty-one false positives out of the box, which is the shape of
    mistake that gets a direction deleted rather than fixed — the view above
    never mentions `approve_action` and is correct.
    """
    assert "approve_action" not in _VIEW_READS_EVERYTHING
    assert (
        compare_view_reads(client_props(_VIEW_CLIENT), _CONSTS, _views(_VIEW_READS_EVERYTHING))
        == []
    )


def test_a_const_action_prop_whose_verb_the_view_never_fires_is_reported() -> None:
    """The exemption is a RE-POINT, not a skip, and this is why it has to be.

    Hardcoding the verb is only equivalent to reading the prop while the two
    agree, and nothing checked that they did. Three cards failed it on the first
    run: `ChartCard`, `DiffCard` and `TableCard` each REQUIRED an
    `open_action: "open"` no button fires, and the ActionRouter's `open` has no
    branch that could route any of the three anywhere.
    """
    silent = _VIEW_READS_EVERYTHING.replace('onAction("approve", {})', "undefined")
    found = compare_view_reads(client_props(_VIEW_CLIENT), _CONSTS, _views(silent))
    assert [f"{m.component}.{m.prop}" for m in found] == ["FindingCard.approve_action"]
    assert "no control fires" in found[0].reason


def test_children_is_exempt_from_the_view_direction_too() -> None:
    """Structural — declared one level up and forwarded, never destructured.

    Same exemption and same reason as `compare_catalog_and_client`; without it
    the four surfaces that take inline children are four false positives.
    """
    assert "children" in client_props(_VIEW_CLIENT)["FindingCard"]
    assert "children" not in _VIEW_READS_EVERYTHING
    assert (
        compare_view_reads(client_props(_VIEW_CLIENT), _CONSTS, _views(_VIEW_READS_EVERYTHING))
        == []
    )


def test_a_component_with_no_adapted_view_is_skipped_not_guessed() -> None:
    """No view means nothing claims to read it; guessing a path would compare
    the prop against the wrong file and report whatever that file happens to
    say."""
    assert compare_view_reads(client_props(_VIEW_CLIENT), _CONSTS, {}) == []


def test_a_prop_named_only_in_a_comment_does_not_count_as_read() -> None:
    """Found by mutating the fix, not by reading the sweep.

    Deleting the `evidence_refs` reader from `FindingCard.tsx` left this
    direction GREEN, because the comment written to explain the fix names the
    prop three times. Every "does the view read it" check has that hole, and a
    sweep a comment can satisfy certifies the defect it exists to find.
    """
    prose_only = """
// evidence_refs is rendered below.
/* and again: evidence_refs */
export function FindingCard({ component, onAction }: RendererProps) {
  const p = component.props as { summary: string };
  return <button onClick={() => onAction("approve", {})}>{p.summary}</button>;
}
"""
    found = compare_view_reads(client_props(_VIEW_CLIENT), _CONSTS, _views(prose_only))
    assert [f"{m.component}.{m.prop}" for m in found] == ["FindingCard.evidence_refs"]


def test_a_url_in_a_string_does_not_swallow_the_rest_of_the_line() -> None:
    """The other half of the comment stripper, and the way it could go wrong.

    Treating `//` as a comment start regardless of context would eat everything
    after a `https://` inside a string — hiding a real read and reporting a
    prop the view does use, which is a false positive on working code.
    """
    line = 'const href = "https://example.test/x"; const s = p.evidence_refs;'
    assert "evidence_refs" in strip_ts_comments(line)
    assert "// gone" not in strip_ts_comments("keep // gone")


def test_every_view_in_the_real_tree_reads_what_it_declares() -> None:
    root = pathlib.Path(__file__).resolve().parents[2]
    report = run(root)
    unread = [m for m in report.mismatches if "never looks" in m.reason]
    assert unread == [], unread


# ---------------------------------------------------------------------------
# Producers that are not Python
# ---------------------------------------------------------------------------


def test_a_browser_producer_is_read_like_any_other() -> None:
    """`NotesSurface.tsx` builds the only `NoteEditorCard` payload that exists.

    A sweep that read Python alone reported `NoteEditorCard` and `HtmlDocCard`
    as "emitted by no producer" and compared nothing about either — while both
    are registered with an `adapt()` impl, so the agent can emit them through
    the binder and hit the zod schema neither was ever checked against.
    """
    source = """
        <NoteEditorCard
          component={{
            type: "NoteEditorCard",
            id: `note-editor-${n.id}`,
            props: { note_id: n.id, section_id: n.section_id, body_md: n.body_md },
          }}
          onAction={onAction}
        />
    """
    assert tsx_emitted_props(source) == {"NoteEditorCard": {"note_id", "section_id", "body_md"}}


def test_a_nested_object_prop_does_not_leak_its_inner_keys() -> None:
    """`props: {meta: {title, slug}}` sends ONE prop, not three.

    Counting `title` and `slug` as props of the component would report two
    undeclared props on every card that nests anything.
    """
    source = 'x = { type: "WikiPageCard", props: { page_meta: { title: t, slug: s } } }'
    assert tsx_emitted_props(source) == {"WikiPageCard": {"page_meta"}}


def test_a_tsx_type_field_with_no_props_is_not_a_component() -> None:
    """The same guard the Python reader needs, for the same reason.

    A Vega-Lite encoding written in TSX is `{field: "x", type: "nominal"}`, and
    a sweep that starts reporting a component called `nominal` gets switched
    off. `type:` only counts with a `props: {` beside it.
    """
    assert tsx_emitted_props('const enc = { field: "x", type: "Nominal" };') == {}


def test_the_view_module_comes_from_adapt_rather_than_the_file_name() -> None:
    """`components/<Name>.tsx` is a convention, and a convention is not a link.

    Resolving by name would keep comparing against `components/ClaimCard.tsx`
    after `adapt()` was pointed somewhere else — silently, and with a green
    result, because the old file still reads the old props.
    """
    source = """
import { ClaimCard as ClaimCardView } from "./components/renamed/Claim";
export const ClaimCardImpl = createComponentImplementation(
  ClaimCardApi,
  adapt("ClaimCard", ClaimCardView, "claim"),
);
"""
    assert view_modules(source) == {"ClaimCard": "./components/renamed/Claim"}


def test_every_catalog_component_has_a_producer_or_a_written_reason() -> None:
    """A coverage gap that is named is a decision; an unnamed one is a footnote.

    The old success line ended at "5 catalog component(s) are emitted by no
    Python producer", which is true, unactionable, and was printed on every run
    for as long as the sweep existed. An unexplained gap is now a mismatch, so
    adding a component to `catalog.json` that nothing emits fails the build
    until somebody either writes the producer or writes down why there is none.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    report = run(root)
    catalog = catalog_components(
        (root / "packages/aleph-a2ui/src/aleph_a2ui/catalog.json").read_text()
    )
    assert set(report.compared) | set(report.no_producer) == catalog
    # And the reasons are reasons, not placeholders.
    assert all(len(why) > 40 for why in report.no_producer.values()), report.no_producer


def _mirror_the_repo_the_sweep_reads(root: pathlib.Path, into: pathlib.Path) -> None:
    """Copy just the trees `run()` opens, so a mutation can be applied to them.

    `run()` reads nine named subject files plus two globs — every `.tsx` under
    `apps/web/src` and every `.py` under `apps/api/src/aleph_api`. Copying those
    is ~2.6MB and lets the mutation below be applied to a real tree rather than
    to a hand-built dict, which is the difference between pinning the sweep and
    pinning a fixture.
    """
    for name, _why in _SUBJECTS:
        src = root / name
        dst = into / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for tree in ("apps/web/src", "apps/api/src/aleph_api"):
        shutil.copytree(root / tree, into / tree, dirs_exist_ok=True)


def test_a_producerless_component_nothing_examines_is_a_mismatch(
    tmp_path: pathlib.Path,
) -> None:
    """A written reason must not become an exemption.

    `_NO_PRODUCER_REASON` names a catalog component nothing emits and says why.
    Those components are counted as compared and contribute their props to the
    coverage number, which is only honest while some direction still reads
    them: `ArtifactCard` has no producer and three of the five directions check
    it. Take away the last of the three — here by renaming its zod entry, the
    single edit that hides it from catalog↔zod, agent→zod and zod→view at once
    — and the sentence would otherwise buy it seven props of free coverage.

    The rename targets `export const ArtifactCardApi`, which is where
    `_API_BLOCK` reads the component name from. Renaming the `name:` field
    inside the block changes nothing and would have made this test green
    against an unmutated tree.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    _mirror_the_repo_the_sweep_reads(root, tmp_path)

    before = run(tmp_path)
    assert "ArtifactCard" in before.compared
    assert before.no_producer_directions["ArtifactCard"] == [
        "agent→zod",
        "catalog↔zod",
        "zod→view",
    ]
    assert not [m for m in before.mismatches if m.component == "ArtifactCard"]

    client = tmp_path / "apps/web/src/a2ui/aleph-catalog-v09.tsx"
    source = client.read_text()
    mutated = source.replace("export const ArtifactCardApi = {", "export const AGQApi = {", 1)
    assert mutated != source, "the mutation string is not in the file; this test proves nothing"
    client.write_text(mutated)

    after = run(tmp_path)
    assert "ArtifactCard" not in after.compared
    assert after.no_producer_directions["ArtifactCard"] == []
    excused = [
        m
        for m in after.mismatches
        if m.component == "ArtifactCard" and "exemption, not coverage" in m.reason
    ]
    assert excused, after.mismatches
    # And the seven props go with it, rather than staying in the total.
    assert after.props_inspected == before.props_inspected - 7
