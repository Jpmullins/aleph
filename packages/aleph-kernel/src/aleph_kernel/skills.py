"""A skill: instructions plus code, loaded as a kernel capability.

The unit both claude-science and deepseek-harness converged on independently —
two unrelated teams reaching the same shape, which is the strongest signal
available for a design decision. A skill is a directory:

    my-skill/
      SKILL.md     front-matter (name, description) + instructions for the model
      kernel.py    helper functions the instructions refer to

The instructions are what the model reads. The code is what it calls. Shipping
them together is the point: an instruction that says "call `verify_dois`" is
only true if `verify_dois` is loaded alongside it, and a prompt library that
drifts from its helpers is worse than either alone.

**Loading is gated, then executed in a fresh namespace.** `kernel.py` passes the
AST gate (`aleph_kernel.ast_gate`) before it is executed at all, so admission
happens without the module having had a turn. It is then exec'd into a fresh
dict rather than imported into `sys.modules`, which means a skill cannot shadow
a real module, two generations of the same skill can coexist, and unloading is
dropping a reference rather than trying to un-import.

**The honest boundary.** Once a skill's function is CALLED it runs in this
process with this process's authority. The gate makes loading safe, not
execution. A skill authored by an agent and doing real work belongs in the
code-runner, where cap_drop, a read-only rootfs and an internal-only network
bound it. What lives here is the loading, declaration and lifecycle — the parts
the kernel must own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aleph_kernel.ast_gate import GateViolation, check_source
from aleph_kernel.errors import KernelError
from aleph_kernel.spec import CapabilitySpec, ProbeResult, ok, problem

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from aleph_kernel.context import Context

__all__ = ["Skill", "SkillRejected", "load_skill", "skill_capability", "skill_from_source"]

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)


class SkillRejected(KernelError):
    """A skill failed admission. Carries every reason, not just the first."""

    def __init__(self, name: str, violations: list[GateViolation]) -> None:
        detail = "; ".join(str(v) for v in violations)
        super().__init__(f"skill {name!r} rejected: {detail}")
        self.skill = name
        self.violations = violations


@dataclass(frozen=True)
class Skill:
    """An admitted skill. Its code is loaded but nothing in it has been called."""

    name: str
    description: str
    instructions: str
    #: Callables the instructions may refer to, from the skill's own namespace.
    helpers: dict[str, Any] = field(default_factory=dict)
    #: What the skill says it provides. Advisory: the probe checks reality.
    exports: tuple[str, ...] = ()

    def helper(self, name: str) -> Any:
        if name not in self.helpers:
            msg = f"skill {self.name!r} has no helper {name!r}"
            raise KeyError(msg)
        return self.helpers[name]


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Return (metadata, body). Front matter is optional but conventional."""
    match = _FRONT_MATTER.match(text)
    if match is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and not key.startswith((" ", "\t", "#")):
            meta[key.strip()] = value.strip()
    return meta, match.group(2)


def skill_from_source(
    name: str,
    instructions: str,
    code: str = "",
    *,
    description: str = "",
) -> Skill:
    """Admit a skill from strings. This is the path an agent-authored skill takes.

    The gate runs before the code is executed, so a rejected skill never has a
    turn. Execution happens in a fresh namespace rather than via import, so
    loading cannot shadow a real module and unloading is dropping a reference.
    """
    meta, body = _parse_front_matter(instructions)
    resolved_name = meta.get("name", name)
    resolved_description = description or meta.get("description", "")

    helpers: dict[str, Any] = {}
    if code.strip():
        violations = check_source(code, filename=f"{resolved_name}/kernel.py")
        if violations:
            raise SkillRejected(resolved_name, violations)
        namespace: dict[str, Any] = {"__name__": f"aleph_skill_{resolved_name}"}
        try:
            exec(compile(code, f"<skill:{resolved_name}>", "exec"), namespace)
        except Exception as exc:
            raise SkillRejected(
                resolved_name, [GateViolation(0, f"module body raised: {exc!r}")]
            ) from exc
        helpers = {
            key: value
            for key, value in namespace.items()
            if callable(value) and not key.startswith("_")
        }

    return Skill(
        name=resolved_name,
        description=resolved_description,
        instructions=body.strip(),
        helpers=helpers,
        exports=tuple(sorted(helpers)),
    )


def load_skill(directory: Path) -> Skill:
    """Admit a skill from a directory containing SKILL.md and optional kernel.py."""
    skill_md = directory / "SKILL.md"
    if not skill_md.is_file():
        msg = f"{directory} has no SKILL.md"
        raise SkillRejected(directory.name, [GateViolation(0, msg)])
    kernel_py = directory / "kernel.py"
    return skill_from_source(
        directory.name,
        skill_md.read_text(encoding="utf-8"),
        kernel_py.read_text(encoding="utf-8") if kernel_py.is_file() else "",
    )


def skill_capability(skill: Skill, *, requires: frozenset[str] = frozenset()) -> CapabilitySpec:
    """Wrap a skill as a kernel capability so it gets the whole lifecycle.

    A skill is a plugin: it declares what it provides, it is activated and
    deactivated like anything else, and removing it is subject to the same
    blast-radius refusal. It publishes itself under ``skill.<name>``.

    The probe requires the skill to be genuinely usable — instructions present,
    and every helper it advertises actually resolvable and callable. A skill
    whose instructions reference a helper that failed to define is the exact
    failure this catches, and it is invisible until someone follows the
    instructions.
    """
    key = f"skill.{skill.name}"

    async def setup(ctx: Context) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        ctx.provide(key, skill)
        if False:  # pragma: no cover - the binding's withdrawal is the inverse
            yield

    async def probe(ctx: Context) -> ProbeResult:
        loaded: Skill = ctx.get(key)
        if not loaded.instructions.strip():
            return problem("the skill has no instructions; a model would be told nothing")
        missing = [name for name in loaded.exports if not callable(loaded.helpers.get(name))]
        if missing:
            return problem(f"advertised helpers are not callable: {', '.join(missing)}")
        # An instruction naming a helper that does not exist is a broken skill,
        # and nothing else notices until a model tries to follow it.
        referenced = set(re.findall(r"`([a-z_][a-z0-9_]*)\(", loaded.instructions))
        dangling = sorted(referenced - set(loaded.exports) - _PYTHON_BUILTINS)
        if dangling:
            return problem(
                f"instructions reference helpers the skill does not define: {', '.join(dangling)}"
            )
        return ok(f"{len(loaded.exports)} helper(s), instructions present")

    return CapabilitySpec(
        name=key, setup=setup, probe=probe, provides=frozenset({key}), requires=requires
    )


#: Names an instruction may plausibly reference without the skill defining them.
_PYTHON_BUILTINS = frozenset(
    {"print", "len", "open", "range", "str", "int", "list", "dict", "dir", "type", "repr"}
)
