"""The ported literature-review skill loads and works (D6).

Ported from claude-science under Apache-2.0 — instructions verbatim, helpers
rebound to aleph-scholar. See skills/literature-review/NOTICE.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aleph_kernel.ast_gate import check_source
from aleph_kernel.kernel import Kernel
from aleph_kernel.skills import load_skill, skill_capability

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "literature-review"


def test_the_skill_directory_ships_with_the_repo() -> None:
    assert (SKILL_DIR / "SKILL.md").is_file()
    assert (SKILL_DIR / "kernel.py").is_file()
    assert (SKILL_DIR / "NOTICE").is_file(), "an Apache-2.0 port needs its attribution"


def test_it_loads_through_the_gate() -> None:
    skill = load_skill(SKILL_DIR)
    assert skill.name == "literature-review"
    assert "fabrication" in skill.instructions, "the substance of the guidance is missing"


def test_the_helpers_the_instructions_name_all_exist() -> None:
    """The failure this catches is invisible until a model follows the prose."""
    skill = load_skill(SKILL_DIR)
    for helper in (
        "verify_dois",
        "crossref_lookup",
        "search_openalex",
        "expand_citations",
        "extract_dois",
        "style_pass",
    ):
        assert helper in skill.exports, helper
        assert callable(skill.helper(helper))


def test_a_local_helper_actually_works() -> None:
    skill = load_skill(SKILL_DIR)
    found = skill.helper("extract_dois")("see 10.1234/abc-1 and https://doi.org/10.5678/XYZ.")
    assert found == ["10.1234/abc-1", "10.5678/xyz"]


def test_style_pass_delegates_to_aleph_scholar() -> None:
    """Rebound, not reimplemented — a second copy would be a worse copy."""
    skill = load_skill(SKILL_DIR)
    out = skill.helper("style_pass")("First [c3], second [c1], first again [c3].")
    assert out == "First [c1], second [c2], first again [c1]."


@pytest.mark.asyncio
async def test_it_activates_as_a_capability() -> None:
    kernel = Kernel()
    kernel.register_core(skill_capability(load_skill(SKILL_DIR)))
    await kernel.boot()
    assert "skill.literature-review" in kernel.active()


def test_the_upstream_kernel_passes_our_gate_unmodified() -> None:
    """Independent calibration check for the AST gate.

    The upstream file was written against claude-science's gate, not ours. That
    ours admits it unchanged is evidence the rule is the real definition-only
    discipline rather than our guess at it. Skipped where the reference corpus
    is not checked out.
    """
    upstream = Path.home() / "code/science/assets/skills/literature-review/kernel.py"
    if not upstream.is_file():
        pytest.skip("claude-science reference corpus not present")
    assert check_source(upstream.read_text()) == []
