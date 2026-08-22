"""What a plugin contributes to the interface, declared as data.

WS-A4. A plugin says "here is my configuration, as a schema" and gets a working
settings screen without shipping any browser code.

**The generator already existed and worked.** `aleph_a2ui.settings_card` is 279
lines, unit-tested, a pure function, and emits only primitives the client
already renders — and it had zero importers outside its own tests. What was
missing is precisely the gap CLAUDE.md names as this codebase's dominant defect:
a producer with no consumer.

**Why this lives in `aleph-runtime` and not on `CapabilitySpec`.** Hanging a UI
contribution on the kernel's spec would make `aleph-kernel` know about A2UI, and
the strict DAG is real — the kernel declares exactly `aleph-core` and
`aleph-observability`. A registry in the composition root, keyed by capability
name, keeps the kernel leaf-ward and puts the knowledge where both halves are
already known.

**Trust is declared, not inferred.** `core` ships with Aleph, `verified` was
reviewed by a person, `authored` was written by an agent. It is on the
contribution because the interface should be able to say where a screen came
from — an agent-authored settings page and a core one are not the same
proposition, and rendering them identically is a decision nobody made.

**And trust is a DISPLAY attribute. Nothing branches on it, on purpose.**
`WS-A4` criterion 6 asked for two things: that the tier be observable at the
API, and that an `authored`-tier save return `requires_approval: true` while a
`core` save does not. The first is built — `_plugins_section` serves it on the
settings surface and `CapabilityOut.trust` serves it on
`GET /v1/projects/{id}/plugins`, where `core` is derived from unaddressability
rather than taken from the plugin's own claim — a contribution that declares
itself `core` while holding a `PluginId` is downgraded to `authored`, because
an agent-installed plugin describing itself as shipped-with-Aleph is the one
direction in which a wrong answer flatters.

The second was deliberately NOT built, and this is the record of why. A save
that returns `requires_approval: true` and is stored anyway is a flag with no
consumer — the dominant defect class in this codebase, and the exact shape of
the four other holes `WS-A4` was opened to close. A save that returns it and is
NOT stored needs a pending-value store, an approver, a route to approve on and a
screen to approve from; that is a feature, not the closing clause of a criterion
about a dataclass field. Until an approver exists, `authored` changes what a
screen SAYS about itself and changes nothing about what saving does.

What actually gates an agent-authored plugin today is stronger than an approval
flag and already runs: the AST gate refuses import-time side effects before a
row is written, `require_at_least(..., ProjectRole.OWNER)` means only an owner
may install one at all, the probe gate refuses one that cannot prove it works,
and `settings_card` REFUSES a secret-shaped field outright rather than asking
anybody to approve it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TrustTier = Literal["core", "verified", "authored"]


@dataclass(frozen=True)
class UIContribution:
    """One plugin's declared interface.

    `config_schema` is JSON Schema. `settings_card.settings_components` turns it
    into components, and it REFUSES a field declaring itself a secret
    (`format: password`, `writeOnly: true`) — a settings value is persisted to
    `card_actions` and to the append-only ledger, so a credential here would be
    plaintext in two permanent tables. Credentials go through
    `ConnectorCredential`.
    """

    plugin_id: str
    title: str
    description: str = ""
    config_schema: dict[str, Any] = field(default_factory=dict)
    #: Pane kinds this plugin adds. `PaneKind` carries its own builder, so a
    #: contribution declaring one is a plugin that adds a surface.
    panes: tuple[Any, ...] = ()
    trust: TrustTier = "authored"


class UIContributionRegistry:
    """The live set, keyed by capability name.

    Deliberately mirrors `PANE_REGISTRY`: `register` refuses to replace a name
    rather than silently overwriting it, because a plugin quietly taking over
    another's settings screen is the same class of bug as one taking over its
    pane, and neither would report anything.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, UIContribution] = {}

    def register(self, *contributions: UIContribution) -> None:
        for c in contributions:
            if c.plugin_id in self._by_id:
                msg = (
                    f"a UI contribution for {c.plugin_id!r} is already registered by "
                    f"{self._by_id[c.plugin_id].trust!r}; choose another id"
                )
                raise ValueError(msg)
            self._by_id[c.plugin_id] = c

    def remove(self, plugin_id: str) -> None:
        """Withdraw a contribution — what deactivating a plugin has to do."""
        self._by_id.pop(plugin_id, None)

    def get(self, plugin_id: str) -> UIContribution | None:
        return self._by_id.get(plugin_id)

    def all(self) -> tuple[UIContribution, ...]:
        return tuple(self._by_id.values())


#: Process-wide, like `PANE_REGISTRY`. Per-project enablement is WS-A1b's
#: `plugins` table; this is what the process can currently render.
UI_CONTRIBUTIONS = UIContributionRegistry()
