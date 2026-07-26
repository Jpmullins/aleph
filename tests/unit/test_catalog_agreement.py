"""The A2UI catalogs must agree — on props and actions, not just on names.

Aleph describes the same 20 components in four places, in four different
envelopes:

1. ``packages/aleph-a2ui/src/aleph_a2ui/catalog.py`` — JSON Schema, the
   validation contract for agent-emitted / pinned cards.
2. ``apps/web/src/a2ui/catalog.ts`` — the component + action name lists.
3. ``apps/web/src/a2ui/aleph-catalog-v09.tsx`` — the zod3 render contract.
4. ``apps/copilot-runtime/src/server.ts`` — the schema the AGENT is shown, which
   determines what it believes it may emit.

``scripts/check-catalog-roster.sh`` compares (1) and (2) **by name only**, so
every shape difference between them is invisible to CI, and it never reads the
action lists or (4) at all. Two live consequences it could not see:

* ``ClaimCard.confidence`` disagreed three ways. None of the lists contained
  ``"cited"`` — the value ``agent/workflow.py`` and ``synthesis_workflow.py``
  both hardcode, and therefore the most common one in the database — so
  validating a real card would have rejected it. The agent-facing list instead
  offered ``"initial"`` (recognised by nothing) and omitted ``"retracted"``,
  making the WP-6 retracted state unemittable by the agent.
* ``"dismiss"`` was declared to the frontend as a dispatchable action with no
  handler registered anywhere.

These tests close that gap. They are deliberately source-parsing rather than
import-based, because three of the four catalogs are TypeScript.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG_TS = REPO_ROOT / "apps" / "web" / "src" / "a2ui" / "catalog.ts"
HANDLERS_PY = REPO_ROOT / "apps" / "api" / "src" / "aleph_api" / "a2ui_handlers.py"
# The agent-facing catalog moved out of `server.ts` into a GENERATED file. The
# invariants below are unchanged — what the agent is told it may emit must be
# what the validator accepts — but they now also check that the generator keeps
# the two views of `catalog.json` aligned, rather than that two humans did.
SERVER_TS = REPO_ROOT / "apps" / "copilot-runtime" / "src" / "catalog.generated.ts"
CANONICAL_JSON = REPO_ROOT / "packages" / "aleph-a2ui" / "src" / "aleph_a2ui" / "catalog.json"
RENDERER_TSX = REPO_ROOT / "apps" / "web" / "src" / "a2ui" / "components" / "WikiPageCard.tsx"


def test_sources_exist() -> None:
    """Guard the guard: a moved file must fail loudly, not silently pass."""
    for path in (CATALOG_TS, HANDLERS_PY, SERVER_TS, RENDERER_TSX):
        assert path.is_file(), f"catalog source not found: {path}"


def _ts_string_list(source: str, symbol: str) -> set[str]:
    m = re.search(rf"{symbol}\s*=\s*\[(.*?)\]", source, re.S)
    assert m, f"could not locate {symbol}"
    return {x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip().strip("\"'")}


def _registered_actions() -> set[str]:
    return set(re.findall(r"""r\.register\(\s*["']([a-z_]+)["']""", HANDLERS_PY.read_text()))


# --------------------------------------------------------------------- actions


def test_every_declared_action_has_a_handler() -> None:
    """A declared-but-unhandled action is a button that fails when clicked."""
    declared = _ts_string_list(CATALOG_TS.read_text(), "ACTION_NAMES")
    registered = _registered_actions()
    orphaned = declared - registered
    assert not orphaned, (
        f"actions declared to the frontend with no registered handler: "
        f"{sorted(orphaned)}. The roster sweep never compares action lists, so "
        f"CI cannot catch this."
    )


def test_every_handler_is_declared() -> None:
    """The reverse: a handler the frontend cannot name is dead code."""
    declared = _ts_string_list(CATALOG_TS.read_text(), "ACTION_NAMES")
    undeclared = _registered_actions() - declared
    assert not undeclared, (
        f"action handlers registered but absent from ACTION_NAMES: {sorted(undeclared)}"
    )


# ------------------------------------------------------------------ confidence


def _canonical_confidence() -> tuple[str, ...]:
    """The one vocabulary, read from the schema the validator actually uses.

    This used to import a `_CLAIM_CONFIDENCE` literal from `catalog.py`. That
    module no longer defines components at all — it loads `catalog.json` — so
    reading the enum off the live schema is both simpler and stricter: a test
    that imports the constant a module exports can agree with it while both
    disagree with what is enforced.
    """
    from aleph_a2ui.catalog import _COMPONENTS

    enum = _COMPONENTS["ClaimCard"]["properties"]["props"]["properties"]["confidence"]["enum"]
    return tuple(enum)


def test_python_catalog_uses_the_canonical_confidence_list() -> None:
    """The validator's enum and the agent's enum live in one file; assert so."""
    canonical = json.loads(CANONICAL_JSON.read_text())
    claim = canonical["components"]["ClaimCard"]
    schema_enum = tuple(claim["schema"]["properties"]["props"]["properties"]["confidence"]["enum"])
    agent_enum = tuple(claim["agent"]["props"]["properties"]["confidence"]["enum"])
    assert schema_enum == _canonical_confidence()
    assert agent_enum == schema_enum, (
        f"inside catalog.json the agent view offers {agent_enum} while the "
        f"validator enforces {schema_enum} — one file, two disagreeing halves"
    )


def test_agent_facing_catalog_matches_the_python_catalog() -> None:
    """What the agent is told it may emit must be what validation accepts.

    A mismatch here is silent in both directions: the agent emits a value the
    validator rejects, or never learns a legal value exists.
    """
    m = re.search(
        r'"confidence":\s*\{\s*"type":\s*"string",\s*"enum":\s*\[(.*?)\]',
        SERVER_TS.read_text(),
        re.S,
    )
    assert m, "could not locate ClaimCard.confidence in the agent-facing catalog"
    agent_enum = tuple(
        x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip().strip("\"'")
    )
    assert agent_enum == _canonical_confidence(), (
        f"agent-facing enum {agent_enum} != canonical {_canonical_confidence()}"
    )


def test_renderer_handles_every_canonical_confidence_value() -> None:
    """An unhandled value falls through to a neutral grey pill.

    That is the quiet failure mode this codebase specialises in: a retracted
    claim would render as calmly as a well-supported one.
    """
    tone_block = re.search(
        r"const CONFIDENCE_TONE[^=]*=\s*\{(.*?)\};", RENDERER_TSX.read_text(), re.S
    )
    assert tone_block, "could not locate CONFIDENCE_TONE in the reader"
    handled = set(re.findall(r"""["']?([a-z][a-z_-]*)["']?\s*:""", tone_block.group(1)))
    missing = {c for c in _canonical_confidence() if c not in handled}
    assert not missing, (
        f"reader has no tone for {sorted(missing)}; those claims render as an "
        f"undifferentiated grey pill. Handled: {sorted(handled)}"
    )


# ---------------------------------------------------- production writes are legal


@pytest.mark.parametrize("value", ["cited", "retracted", "contested"])
def test_values_production_actually_writes_are_legal(value: str) -> None:
    """The vocabulary must cover reality, not an idealised subset.

    `agent/workflow.py` + `synthesis_workflow.py` write "cited";
    `aleph_reviewer.retraction` writes "retracted"; the refresh-flag handler
    writes "contested". Any of these missing means a real row cannot be
    represented.
    """
    assert value in _canonical_confidence(), (
        f"production writes confidence={value!r} but the canonical catalog "
        f"vocabulary is {_canonical_confidence()} — a real claim could not be "
        f"validated or rendered."
    )


def test_component_names_agree_between_catalogs() -> None:
    """What the roster sweep already checks, kept here so this module is the
    single place to look when the catalogs drift."""
    from aleph_a2ui.catalog import _COMPONENTS

    ts_names = _ts_string_list(CATALOG_TS.read_text(), "COMPONENT_NAMES")
    py_names = set(_COMPONENTS)
    assert ts_names == py_names, (
        f"only in catalog.ts: {sorted(ts_names - py_names)}; "
        f"only in catalog.json: {sorted(py_names - ts_names)}"
    )


def test_agent_catalog_is_valid_json_shaped() -> None:
    """The agent-facing schema is hand-written TS; make sure it still parses as
    a component map rather than silently becoming unreadable."""
    src = SERVER_TS.read_text()
    m = re.search(r"export const ALEPH_A2UI_CATALOG\s*=\s*\{", src)
    assert m, "ALEPH_A2UI_CATALOG not found in the generated agent catalog"
    assert "GENERATED by scripts/gen_catalog.py" in src, (
        "the agent catalog lost its generated banner — someone may be editing "
        "it by hand again, which is the drift this whole file exists to stop"
    )
    assert '"ClaimCard"' in src or "ClaimCard:" in src, "ClaimCard missing from the agent catalog"
    json.dumps(_canonical_confidence())  # the canonical list must be serialisable
