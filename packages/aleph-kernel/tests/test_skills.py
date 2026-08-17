"""Skills: instructions plus code, admitted through the gate, run as plugins.

D2 (a skill is a kernel plugin) and D3 (an agent authors one end to end).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aleph_kernel.agent_api import AgentPluginAPI
from aleph_kernel.kernel import Kernel, PluginId
from aleph_kernel.skills import SkillRejected, load_skill, skill_capability, skill_from_source

INSTRUCTIONS = """---
name: doi-check
description: Verify a DOI resolves before citing it.
---

# Verifying a DOI

A DOI you emit either resolves to a real paper or it is a fabrication, and the
difference is checkable. Call `normalize_doi(raw)` first, then `looks_valid(doi)`.
"""

CODE = '''
"""Helpers for the doi-check skill."""

import re

_DOI = re.compile(r"10\\.\\d{4,9}/\\S+")


def normalize_doi(raw: str) -> str:
    return raw.strip().lower().removeprefix("https://doi.org/")


def looks_valid(doi: str) -> bool:
    return bool(_DOI.fullmatch(doi))
'''


# -- admission ---------------------------------------------------------------


def test_a_skill_loads_its_instructions_and_helpers() -> None:
    skill = skill_from_source("doi-check", INSTRUCTIONS, CODE)
    assert skill.name == "doi-check"
    assert "fabrication" in skill.instructions
    assert set(skill.exports) == {"normalize_doi", "looks_valid"}
    assert skill.helper("normalize_doi")("https://DOI.org/10.1234/AB") == "10.1234/ab"


def test_front_matter_is_not_left_in_the_instructions() -> None:
    skill = skill_from_source("doi-check", INSTRUCTIONS, CODE)
    assert not skill.instructions.startswith("---")
    assert skill.description.startswith("Verify a DOI")


def test_code_with_an_import_time_side_effect_is_refused() -> None:
    """The gate runs BEFORE execution, so a rejected skill never has a turn."""
    evil = "import os\nos.environ['PWNED'] = '1'\n"
    with pytest.raises(SkillRejected) as exc:
        skill_from_source("evil", INSTRUCTIONS, evil)
    assert exc.value.violations
    import os

    assert "PWNED" not in os.environ, "the rejected skill executed anyway"


def test_a_skill_may_have_no_code() -> None:
    skill = skill_from_source("prose-only", "---\nname: prose\n---\n\nJust guidance.")
    assert skill.exports == ()


def test_a_module_that_raises_while_loading_is_refused_not_crashing() -> None:
    code = "def f():\n    pass\n\n\nclass C:\n    x = 1 / 0\n"
    with pytest.raises(SkillRejected, match="raised"):
        skill_from_source("boom", INSTRUCTIONS, code)


def test_loading_does_not_pollute_sys_modules() -> None:
    """A skill must not be able to shadow a real module."""
    import sys

    before = set(sys.modules)
    skill_from_source("doi-check", INSTRUCTIONS, CODE)
    assert set(sys.modules) == before


def test_two_generations_can_coexist() -> None:
    """Fresh-namespace loading is what makes replacement possible later."""
    v1 = skill_from_source("s", INSTRUCTIONS, "def version():\n    return 1\n")
    v2 = skill_from_source("s", INSTRUCTIONS, "def version():\n    return 2\n")
    assert v1.helper("version")() == 1
    assert v2.helper("version")() == 2


def test_loading_from_a_directory(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text(INSTRUCTIONS)
    (tmp_path / "kernel.py").write_text(CODE)
    skill = load_skill(tmp_path)
    assert skill.name == "doi-check"
    assert skill.helper("looks_valid")("10.1234/abc")


def test_a_directory_without_instructions_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SkillRejected, match=r"SKILL\.md"):
        load_skill(tmp_path)


# -- as a kernel plugin ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_skill_activates_as_a_capability() -> None:
    kernel = Kernel()
    skill = skill_from_source("doi-check", INSTRUCTIONS, CODE)
    kernel.register_core(skill_capability(skill))
    await kernel.boot()
    assert "skill.doi-check" in kernel.active()
    assert kernel.is_provided("skill.doi-check")


@pytest.mark.asyncio
async def test_a_skill_whose_instructions_reference_a_missing_helper_is_refused() -> None:
    """Broken until someone follows the instructions — exactly what a probe is for."""
    kernel = Kernel()
    skill = skill_from_source(
        "broken",
        "---\nname: broken\n---\n\nCall `verify_dois(x)` to check.",
        "def something_else():\n    return 1\n",
    )
    kernel.register_core(skill_capability(skill))
    from aleph_kernel.errors import ProbeFailed

    with pytest.raises(ProbeFailed, match="reference helpers the skill does not define"):
        await kernel.boot()


@pytest.mark.asyncio
async def test_a_skill_with_no_instructions_is_refused() -> None:
    from aleph_kernel.errors import ProbeFailed

    kernel = Kernel()
    kernel.register_core(skill_capability(skill_from_source("empty", "---\nname: e\n---\n")))
    with pytest.raises(ProbeFailed, match="no instructions"):
        await kernel.boot()


# -- D3: an agent authors one end to end -------------------------------------


@pytest.mark.asyncio
async def test_an_agent_installs_a_skill_it_authored() -> None:
    kernel = Kernel()
    await kernel.boot()
    api = AgentPluginAPI(kernel)

    outcome = await api.install(
        skill_capability(skill_from_source("doi-check", INSTRUCTIONS, CODE))
    )

    assert outcome.installed, outcome.detail
    assert "skill.doi-check" in kernel.active()

    # And can take its own skill back out.
    import uuid

    removed = await api.disable(PluginId(uuid.UUID(outcome.plugin_id)))
    assert "disabled" in removed.detail
    assert "skill.doi-check" not in kernel.active()


@pytest.mark.asyncio
async def test_an_agent_authored_skill_that_does_not_work_is_refused() -> None:
    """The full loop: authored, gated, probed, refused, nothing left behind."""
    kernel = Kernel()
    await kernel.boot()
    api = AgentPluginAPI(kernel)

    bad = skill_from_source(
        "hopeful",
        "---\nname: hopeful\n---\n\nCall `does_not_exist(x)`.",
        "def unrelated():\n    return 1\n",
    )
    outcome = await api.install(skill_capability(bad))

    assert not outcome.installed
    assert "refused" in outcome.detail
    assert kernel.active() == frozenset()


@pytest.mark.asyncio
async def test_an_agent_cannot_install_code_with_import_time_effects() -> None:
    """The gate stops it before the kernel ever sees a capability."""
    with pytest.raises(SkillRejected):
        skill_from_source("evil", INSTRUCTIONS, "import os\nos.system('echo pwned')\n")
