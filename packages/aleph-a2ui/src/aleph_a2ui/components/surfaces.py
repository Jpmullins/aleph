"""Surface builders — typed wrappers that emit catalog-conformant payloads."""

from __future__ import annotations

from typing import Any

from aleph_a2ui.messages import create_surface, full_surface, update_components

# Catalog id of the shared v0.9 frontend catalog
# (`apps/web/src/a2ui/aleph-catalog-v09.tsx`). `createSurface.catalogId` must
# reference this exact value for the renderer to resolve component impls.
ALEPH_V09_CATALOG_ID = "aleph://v1"


# ---------------------------------------------------------------------------
# v0.9 message-list builders (Wave 4 T3)
# ---------------------------------------------------------------------------
#
# Each right-panel tab is rendered through the upstream `@a2ui` v0_9
# `MessageProcessor` + `<A2uiSurface>` against the shared catalog (`aleph://v1`).
# The canonical tabs (Wiki/Library/Notes/Briefs) are DATA-BOUND (see
# the builders below); `BriefsSurface` (agent-composed, WP-4d) still rides the
# single-component `_surface_messages` shell with an inline `children` card list
# (the frontend `adapt` helper forwards it to `component.children`).


def _surface_messages(
    *,
    surface_id: str,
    component_name: str,
    props: dict[str, Any] | None = None,
    children: list[dict[str, Any]] | None = None,
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """Wrap a single Aleph surface view as a v0.9 message list.

    The component carries its props inline. Legacy card children (Briefs
    `ApprovalCard`s, Wiki embeds) are forwarded as a structural `children` prop;
    the existing views render them via their own renderer.

    The single surface component MUST carry `id="root"`: the upstream
    `@a2ui/react` `<A2uiSurface>` renders exactly the component whose id is
    `"root"` (`DeferredChild id="root"`), falling back to `[Loading root...]`
    when no such component exists. (A data-bound surface satisfies this via
    its root `Column`; these single-component surfaces satisfy it by naming the
    surface view itself `root`.) `surface_id` remains the surface-level id used
    by `createSurface`/`updateComponents` and as the React key.
    """
    component: dict[str, Any] = {"id": "root", "component": component_name}
    if props:
        component.update(props)
    if children is not None:
        component["children"] = children
    return [
        create_surface(surface_id=surface_id, catalog_id=catalog_id),
        update_components(surface_id=surface_id, components=[component]),
    ]


# ---------------------------------------------------------------------------
# Data-bound canonical tab builders (WP-4 sub-spec (a)).
#
# Each of the canonical tabs (Wiki / Library / Notes / Briefs) is now
# SERVER-BUILT and DATA-BOUND: the builder loads its rows (in the route layer,
# which owns the session), then emits a `full_surface` — `createSurface` +
# `updateComponents` (structure, once) + a root `updateDataModel` (the typed
# data model). The single surface component carries its data as `{"path": ...}`
# BINDINGS into that model, so the
# React view renders ONLY from bound props (zero client fetch) and a mutation
# patches in place via a per-path `updateDataModel` delta (`diff_data_model`) —
# never a full re-render. The self-fetching react-query views are gone. The
# sweep that enforced that is gone too; the pane model replaced it, since a pane
# owns no transport of its own (`SurfaceStreamProvider` multiplexes one SSE
# connection for the whole reading region).
# ---------------------------------------------------------------------------


def wiki_surface_v09(
    *,
    pages: list[dict[str, Any]],
    open_page: dict[str, Any] | None = None,
    categories: list[dict[str, Any]] | None = None,
    health: dict[str, Any] | None = None,
    surface_id: str = "wiki",
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """Data-bound Wiki tab.

    Data model: ``{pages, open, categories, health}``.

    `pages` is the page-browser list; `open` is the currently-open page's reader
    payload (revision body, claims, citations, wikilinks) or ``None`` when
    browsing the index.

    `categories` is the project's schema categories — id, title, blurb — so the
    browser can group by category and name each group properly. It is sent with
    the surface rather than fetched by the view because a pane owns no transport
    of its own; a component that had to fetch its own category titles would be
    the self-fetching surface the pane model exists to remove.

    `health` is the lint's severity counts, not its findings: a one-line state
    of the corpus for the browser header. The findings themselves are a separate
    read — putting 300 of them in every surface push would make the wiki tab's
    payload dominated by a list nobody asked for.
    """
    component = {
        "id": "root",
        "component": "WikiSurface",
        "pages": {"path": "/pages"},
        "open": {"path": "/open"},
        "categories": {"path": "/categories"},
        "health": {"path": "/health"},
    }
    return full_surface(
        surface_id=surface_id,
        catalog_id=catalog_id,
        components=[component],
        data_model={
            "pages": pages,
            "open": open_page,
            "categories": categories or [],
            "health": health or {},
        },
    )


def artifacts_surface_v09(
    *,
    sources: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    surface_id: str = "library",
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """Data-bound Library tab. Data model: ``{sources: [...], artifacts: [...]}``.

    Ingested `sources` (raw PDFs/webpages/docs) alongside built `artifacts`.
    Each source carries a bound ``normalized_preview`` (WP-4e) so `SourceCard`
    renders its text preview from props with no self-fetch.
    """
    component = {
        "id": "root",
        "component": "ArtifactsSurface",
        "sources": {"path": "/sources"},
        "artifacts": {"path": "/artifacts"},
    }
    return full_surface(
        surface_id=surface_id,
        catalog_id=catalog_id,
        components=[component],
        data_model={"sources": sources, "artifacts": artifacts},
    )


def notes_surface_v09(
    *,
    notes: list[dict[str, Any]],
    surface_id: str = "notes",
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """Data-bound Notes tab. Data model: ``{notes: [{id, title, body_md,
    section_id, updated_at}]}``. Editing a note body is an `edit_note` action
    through the router; the debounced edit patches the model in place."""
    component = {
        "id": "root",
        "component": "NotesSurface",
        "notes": {"path": "/notes"},
    }
    return full_surface(
        surface_id=surface_id,
        catalog_id=catalog_id,
        components=[component],
        data_model={"notes": notes},
    )


def briefs_surface_v09(
    *,
    badge_count: int = 0,
    children: list[dict[str, Any]] | None = None,
    surface_id: str = "briefs",
) -> list[dict[str, Any]]:
    return _surface_messages(
        surface_id=surface_id,
        component_name="BriefsSurface",
        props={"badge_count": badge_count},
        children=children or [],
    )


def grounding_surface_v09(
    *,
    claim: dict[str, Any] | None,
    groundings: list[dict[str, Any]],
    surface_id: str = "grounding",
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """The grounding inspector: what a claim actually rests on.

    Data model: ``{claim: {id, text, confidence, page_title} | null,
    groundings: [{marker, source: {...}, chunks: [{id, ordinal, text,
    char_start, char_end, section_path}]}]}``.

    This is the surface that makes the platform's claims about itself checkable:
    it walks claim → citation → chunk → character span → the source text a
    reader can read. Every hop existed in the schema and none of them carried
    data until the three writers were fixed, so an inspector built earlier would
    have rendered an authoritative-looking empty chain.

    `groundings` being empty is a first-class state, not an error — an
    ungrounded claim is exactly what an analyst most needs to see.
    """
    component = {
        "id": "root",
        "component": "GroundingSurface",
        "claim": {"path": "/claim"},
        "groundings": {"path": "/groundings"},
    }
    return full_surface(
        surface_id=surface_id,
        catalog_id=catalog_id,
        components=[component],
        data_model={"claim": claim, "groundings": groundings},
    )


def inspector_surface_v09(
    *,
    runs: list[dict[str, Any]],
    selected: dict[str, Any] | None,
    events: list[dict[str, Any]],
    surface_id: str = "inspector",
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """What the assistant did, and where it stopped.

    Data model: ``{runs: [{id, status, started_at, completed_at, duration_ms,
    tool_calls, error_text}], selected: {...} | null, events: [{kind, tool,
    subagent, duration_ms, args, error_class, error, at}]}``.

    Until WS-C3a there was nothing to render: a chat turn wrote no `AgentRun`,
    no events, and the only place an agent failure was legible was the API
    container's stderr. That is what this removes the need for — "what did it
    just do and why did it stop" is the primary operational question the moment
    the agent starts authoring its own plugins, and there was no answer to it.

    An empty `runs` is a first-class state, not an error. So is a `selected` run
    with no events: a turn that died before its first tool call is exactly the
    shape a reader needs to recognise, and rendering it as "nothing here" would
    hide the most informative case.
    """
    component = {
        "id": "root",
        "component": "InspectorSurface",
        "runs": {"path": "/runs"},
        "selected": {"path": "/selected"},
        "events": {"path": "/events"},
    }
    return full_surface(
        surface_id=surface_id,
        catalog_id=catalog_id,
        components=[component],
        data_model={"runs": runs, "selected": selected, "events": events},
    )


def settings_surface_v09(
    *,
    title: str,
    sections: list[dict[str, Any]],
    surface_id: str = "settings",
    catalog_id: str = ALEPH_V09_CATALOG_ID,
) -> list[dict[str, Any]]:
    """Configuration and instrumentation, as a pane rather than a slide-over.

    Data model: ``{title, sections}`` where each section is
    ``{kind, title, ...payload}`` and `kind` selects the renderer.

    **Why one component and a list of sections rather than four surfaces.**
    WS-B1 deletes `Drawers.tsx`, 742 lines carrying seven hand-written sections
    behind a `fixed inset-0` overlay. The plan's stated reason is not cosmetic:
    a drawer with a React function per section means a new plugin needs a new
    React function, so settings is the one part of the workbench a plugin cannot
    extend. Sending the SECTION LIST as data moves that decision to the server —
    what a settings pane contains, and in what order, is now a value, and the
    four panes that used to be drawers (`settings`, `logs`, `notifications`,
    `profile`) are four different values of it rather than four components.

    A plugin whose configuration is expressible as JSON Schema does not come
    through here at all: `settings_card.settings_surface` generates its screen
    from the declaration, which is the cheaper path and the one that needs no
    entry in any of these lists.

    Section kinds, and what each carries:

    * ``fields``     — ``rows: [{label, value, mono?, multiline?}]``
    * ``members``    — ``members: [{id, user_id, role}]``
    * ``model_profile`` — ``profiles: [str]``, ``current: str|null``,
      ``capabilities: [{id, label, help, bound, eligible: [{id, label}]}]``,
      ``gateway: {reachable, model_count, note}``
    * ``connectors`` — ``connectors: [{id, kind, name, requires_auth, enabled,
      has_key, status}]``
    * ``plugins``    — ``plugins: [{id, title, description, trust}]``
    * ``ledger``     — ``chain: {ok, count, first_divergence_event_id}``,
      ``events: [{id, actor_kind, action_kind, target_kind, trace_id, timestamp}]``
    * ``runs``       — ``runs: [{id, agent_kind, status, error_text, created_at,
      completed_at}]``
    * ``note``       — ``text``: a stated reason a section could not be built.

    A section kind the client does not know is rendered as a named placeholder,
    not skipped. Skipping is how a settings screen loses a section and still
    looks complete — the exact regression `docs/plan.md` WS-B1 calls the risk of
    this workstream.
    """
    component = {
        "id": "root",
        "component": "SettingsSurface",
        "title": {"path": "/title"},
        "sections": {"path": "/sections"},
    }
    return full_surface(
        surface_id=surface_id,
        catalog_id=catalog_id,
        components=[component],
        data_model={"title": title, "sections": sections},
    )
