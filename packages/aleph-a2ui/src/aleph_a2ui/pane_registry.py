"""What surfaces exist — declared on the server, rendered by whoever asks.

The rail used to be a five-element constant in the web app: Wiki, Library,
Notes, Hypotheses, Briefs. That is the research plugin suite compiled into the
chrome, and it makes the workbench a research tool wearing general-purpose
clothes. Install an ability that has nothing to do with papers and it has
nowhere to appear; remove the research suite and the front end still advertises
it. Neither is survivable once abilities are added at runtime, which is the
whole product.

So the list moves here, and the client renders whatever it is given. Today every
entry is contributed by the research suite because that is the only suite there
is — the point is not that the list is short, it is that the CLIENT no longer
knows what is on it.

`extend()` is the seam a plugin uses. It exists now, unused, deliberately: a
registry you cannot add to is a constant with extra steps, and the next thing
that needs it should find a working door rather than build one.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PaneKind:
    """One openable surface."""

    #: Wire name. Appears in `?panes=` specs and in `createSurface`.
    id: str
    #: What a person sees in the rail and the block header.
    title: str
    #: Icon key the client resolves. An unknown key falls back rather than throwing.
    icon: str
    #: Does it appear in the rail as something you can open directly? A pane
    #: opened FROM something else (grounding, from a claim) is meaningless
    #: without its parameter and must not be launchable.
    launchable: bool = True
    #: Parameters the pane requires, e.g. ("claim_id",).
    params: tuple[str, ...] = ()
    #: Which suite contributed it. Shown nowhere yet; the moment a second suite
    #: exists, "where did this surface come from" becomes a real question.
    source: str = "research"
    #: How this pane's content is built: `(session, project_id, params, surface_id)`
    #: returning a v0.9 message list.
    #:
    #: The registry described `extend()` as "the seam a plugin uses" while the
    #: thing that BUILT a pane was a hardcoded if/elif chain in a 1,028-line
    #: route file that raised `NotFound` on an unknown name. So a plugin could
    #: register a pane and the app would break on it — the door was half built,
    #: and the missing half was the half that does anything.
    #:
    #: `None` for the core panes, which the route still resolves by name. A
    #: plugin supplies one, and that is the whole difference between extending
    #: the registry and editing the router.
    builder: object | None = None


_CORE: tuple[PaneKind, ...] = (
    PaneKind(id="wiki", title="Wiki", icon="wiki"),
    PaneKind(id="library", title="Library", icon="library"),
    PaneKind(id="artifacts", title="Artifacts", icon="artifacts"),
    PaneKind(id="notes", title="Notes", icon="notes"),
    PaneKind(id="hypotheses", title="Hypotheses", icon="hypotheses"),
    PaneKind(id="briefs", title="Briefs", icon="briefs"),
    PaneKind(
        id="grounding",
        title="Grounding",
        icon="grounding",
        launchable=False,
        params=("claim_id",),
    ),
    PaneKind(
        id="inspector",
        title="Inspector",
        icon="inspector",
        launchable=True,
        params=("run_id",),
    ),
)


@dataclass
class PaneRegistry:
    """The live set. One instance per process; plugins extend it at load."""

    _kinds: dict[str, PaneKind] = field(default_factory=dict)

    def extend(self, *kinds: PaneKind) -> None:
        """Contribute surfaces. Raises rather than silently replacing a name.

        A plugin quietly overwriting `wiki` would change what every existing
        pane spec resolves to, and nothing would report it.
        """
        for k in kinds:
            if k.id in self._kinds:
                raise ValueError(
                    f"pane kind {k.id!r} is already registered by "
                    f"{self._kinds[k.id].source!r}; choose another id"
                )
            self._kinds[k.id] = k

    def remove(self, pane_id: str) -> None:
        """Withdraw a surface — what deactivating a plugin has to do."""
        self._kinds.pop(pane_id, None)

    def all(self) -> tuple[PaneKind, ...]:
        return tuple(self._kinds.values())

    def ids(self) -> frozenset[str]:
        return frozenset(self._kinds)

    def get(self, pane_id: str) -> PaneKind | None:
        return self._kinds.get(pane_id)


PANE_REGISTRY = PaneRegistry()
PANE_REGISTRY.extend(*_CORE)
