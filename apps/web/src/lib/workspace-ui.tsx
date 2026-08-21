/**
 * Shared workspace-UI state (Wave 2).
 *
 * The right panel's active surface tab and the currently-open wiki page are
 * lifted here so two parties can both read and drive them:
 *
 *   - the **analyst**, by clicking tabs / opening pages in the right panel;
 *   - the **assistant agent**, via CopilotKit `useAgentContext` (it reads what
 *     the analyst is looking at) and `useFrontendTool` (it can switch tabs or
 *     open a page in response to a request).
 *
 * This is the frontend half of CopilotKit shared state — the agent's own
 * run state (phase, coverage, cited pages) flows the other way over AG-UI.
 */
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

/**
 * The pane registry — one source of truth for what can be opened.
 *
 * This replaces a five-element `SURFACE_TABS` constant that had drifted out of
 * agreement with the server: `routes/surfaces.py` accepted seven pane kinds
 * while the client type admitted five, so `artifacts` and `grounding` could be
 * streamed by the backend and had nowhere on the client to land.
 * `GroundingSurface` was the visible cost — a React impl, a catalog entry, a
 * registered component api, a server builder and a route branch, all complete,
 * and no code path able to open it. The Rail's own docstring says a rail was
 * chosen because "Aleph needs more surfaces than that", and then the ceiling
 * was reintroduced one file over.
 *
 * A constant cannot survive plugins. When a plugin brings a surface, it appends
 * an entry here at load time; nothing else in the UI needs to change, because
 * everything downstream reads the registry rather than a hardcoded union.
 *
 * `wire` is what the server parses (`_PANE_KINDS`), kept explicit rather than
 * lowercasing the id, so a display rename never silently changes the protocol.
 * `scripts/check-pane-registry.sh` fails the build if the two disagree.
 */
export interface PaneKindDef {
  /** Wire name the server's `_PANE_KINDS` accepts. */
  readonly wire: string;
  /** Icon key in `components/Icons`. */
  readonly icon: string;
  /** Does it appear in the rail as something you can open directly? */
  readonly launchable: boolean;
  /** Params the pane requires; a non-launchable pane is opened *from* one. */
  readonly params: readonly string[];
}

export const PANE_REGISTRY = {
  Wiki: { wire: "wiki", icon: "wiki", launchable: true, params: [] },
  Library: { wire: "library", icon: "library", launchable: true, params: [] },
  Artifacts: { wire: "artifacts", icon: "artifacts", launchable: true, params: [] },
  Notes: { wire: "notes", icon: "notes", launchable: true, params: [] },
  Hypotheses: { wire: "hypotheses", icon: "hypotheses", launchable: true, params: [] },
  Briefs: { wire: "briefs", icon: "briefs", launchable: true, params: [] },
  // Opened from a claim, never from the rail — it is meaningless without one.
  Grounding: { wire: "grounding", icon: "grounding", launchable: false, params: ["claim_id"] },
} as const satisfies Record<string, PaneKindDef>;

export type SurfaceTab = keyof typeof PANE_REGISTRY;

/** Every pane kind, launchable or not. Use this to validate an incoming name. */
export const ALL_PANE_KINDS = Object.keys(PANE_REGISTRY) as SurfaceTab[];

/** What the rail offers: the panes a person can open unprompted. */
export const SURFACE_TABS = ALL_PANE_KINDS.filter(
  (k) => PANE_REGISTRY[k].launchable,
) as [SurfaceTab, ...SurfaceTab[]];

/** Display name -> the name the server understands. */
export function paneWireName(tab: SurfaceTab): string {
  return PANE_REGISTRY[tab].wire;
}

/** Is this string a pane kind? Narrows, so callers stop casting. */
export function isPaneKind(value: string): value is SurfaceTab {
  return Object.hasOwn(PANE_REGISTRY, value);
}

/**
 * The analyst's current text/claim selection in the reader (WP-4d). Published by
 * the reader tier (`WikiPageCard`) and exposed to the agent via `useAgentContext`
 * so it can act on "this claim" / "this page" with nothing named.
 */
export interface WorkspaceSelection {
  claim_id: string | null;
  text: string | null;
  page_id: string | null;
}

export interface WorkspaceUIState {
  /** Open panes, left to right. The workspace is these. */
  panes: Pane[];
  /** Pane the analyst last interacted with — what "this" refers to. */
  focusedPaneId: string;
  setFocusedPaneId: (id: string) => void;
  /** Open a view, or focus it if already open. */
  openPane: (kind: SurfaceTab, opts?: { title?: string; params?: Record<string, string> }) => void;
  closePane: (id: string) => void;
  /**
   * The focused pane's kind. Kept because the agent's shared-state payload and
   * its `focus_tab` tool speak in surface names; setting it opens/focuses the
   * corresponding pane rather than swapping a single slot.
   */
  activeSurface: SurfaceTab;
  setActiveSurface: (tab: SurfaceTab) => void;
  /** Title of the wiki page the analyst currently has open (if any). */
  openPageTitle: string | null;
  setOpenPageTitle: (title: string | null) => void;
  /**
   * Externally-requested wiki page to open (card "open" actions, agent
   * navigation). WikiSurface consumes it and syncs its own selection.
   */
  openPageId: string | null;
  setOpenPageId: (id: string | null) => void;
  /** The analyst's current claim/text selection in the reader (UI → agent). */
  selection: WorkspaceSelection | null;
  setSelection: (selection: WorkspaceSelection | null) => void;
  /**
   * Claim the agent asked the reader to highlight (agent → UI, via the
   * `highlight_claim` frontend tool). `WikiPageCard` rings the matching claim.
   */
  highlightedClaimId: string | null;
  setHighlightedClaimId: (claimId: string | null) => void;
}

/**
 * A pane is one thing being read, and the unit the workspace is built from.
 *
 * Tabs assumed the app knows in advance what views exist — coherent for a CRUD
 * app, incoherent here, because the A2UI premise is that the *agent composes
 * the view*. Once `code_runner` can generate one there is no tab for "the
 * grounding tree for this claim" or "these two contradictory claims side by
 * side", and comparison is the thing the old shell structurally could not do.
 *
 * `id` is the wire `surfaceId`: the A2UI protocol already stamps it on every
 * message and `MessageProcessor` already holds a `surfacesMap`, so many panes
 * over one connection is what the protocol was built for. One-surface-at-a-time
 * was a UI constraint imposed on a multi-surface protocol.
 */
export interface Pane {
  /** Wire `surfaceId`. Stable for the pane's life. */
  id: string;
  /** Which builder renders it. */
  kind: SurfaceTab;
  /** Shown in the pane header. */
  title: string;
  /** Builder params, e.g. `{ page_id }`. Part of the stream URL. */
  params?: Record<string, string>;
}

/** Tiling gets unreadable past three columns at any realistic window width. */
export const MAX_PANES = 3;

function paneKey(kind: SurfaceTab, params?: Record<string, string>): string {
  const p = params ? Object.entries(params).sort().map(([k, v]) => `${k}=${v}`).join("&") : "";
  return p ? `${kind.toLowerCase()}:${p}` : kind.toLowerCase();
}

const WorkspaceUIContext = createContext<WorkspaceUIState | null>(null);

export function WorkspaceUIProvider({ children }: { children: ReactNode }) {
  const [panes, setPanes] = useState<Pane[]>([
    { id: "wiki", kind: "Wiki", title: "Wiki" },
  ]);
  const [focusedPaneId, setFocusedPaneId] = useState<string>("wiki");

  /**
   * Open a pane, or focus it if that exact view is already open.
   *
   * Re-opening the same thing must never duplicate it — that is how a pane
   * workspace turns into clutter — so identity is (kind, params), not a fresh
   * id per click.
   */
  const openPane = (kind: SurfaceTab, opts: { title?: string; params?: Record<string, string> } = {}) => {
    const id = paneKey(kind, opts.params);
    setPanes((prev) => {
      if (prev.some((p) => p.id === id)) return prev;
      // A pane of this kind that is merely showing something (`wiki:page_id=…`)
      // is still that pane. Matching on the exact id alone opened a SECOND Wiki
      // pane every time a card's `open` action fired, because the action
      // handler re-keys the pane and then asks for the surface by name.
      if (!opts.params) {
        const existing = prev.find((p) => p.kind === kind);
        if (existing) {
          setFocusedPaneId(existing.id);
          return prev;
        }
      }
      const next = [...prev, { id, kind, title: opts.title ?? kind, params: opts.params }];
      // Oldest unfocused pane makes way rather than refusing the open — the
      // user asked for this view and should get it.
      return next.length > MAX_PANES ? next.slice(next.length - MAX_PANES) : next;
    });
    setFocusedPaneId(id);
  };

  const closePane = (id: string) => {
    setPanes((prev) => {
      const next = prev.filter((p) => p.id !== id);
      // Never leave an empty stage.
      return next.length ? next : [{ id: "wiki", kind: "Wiki", title: "Wiki" }];
    });
    setFocusedPaneId((cur) => (cur === id ? "wiki" : cur));
  };

  // DERIVED, not stored. Its own docstring calls it "the focused pane's kind",
  // and holding it separately let the two drift: the rail opened a pane and
  // focused it while `activeSurface` stayed on Wiki, so the context bar — and
  // therefore the `active_tab` the agent is told about — reported a surface the
  // analyst was no longer looking at.
  const activeSurface: SurfaceTab =
    panes.find((p) => p.id === focusedPaneId)?.kind ?? panes[0]?.kind ?? "Wiki";
  const [openPageTitle, setOpenPageTitle] = useState<string | null>(null);
  const [openPageId, setOpenPageIdState] = useState<string | null>(null);

  /**
   * Open a wiki page — which means re-keying the Wiki pane, not just storing an id.
   *
   * A pane's id IS its wire `surfaceId`, and the stream subscribes with
   * `?panes=<id>,<id>`. The server reads the page from that spec
   * (`wiki:page_id=…`) and binds it to `/open`. So an `openPageId` held only in
   * React state never reaches the server: `/open` stays null, `WikiSurface`
   * keeps rendering the index, and clicking a page does visibly nothing while
   * every layer reports success — the action POSTs 200, the navigate result
   * comes back, and the state updates. There was simply no path from that state
   * to the request.
   */
  const setOpenPageId = (id: string | null) => {
    setOpenPageIdState(id);
    setPanes((prev) => {
      const idx = prev.findIndex((p) => p.kind === "Wiki");
      if (idx === -1) return prev;
      const params = id ? { page_id: id } : undefined;
      const nextId = paneKey("Wiki", params);
      if (prev[idx].id === nextId) return prev;
      const next = [...prev];
      next[idx] = { ...next[idx], id: nextId, params };
      setFocusedPaneId(nextId);
      return next;
    });
  };
  const [selection, setSelection] = useState<WorkspaceSelection | null>(null);
  const [highlightedClaimId, setHighlightedClaimId] = useState<string | null>(null);

  const value = useMemo<WorkspaceUIState>(
    () => ({
      panes,
      focusedPaneId,
      setFocusedPaneId,
      openPane,
      closePane,
      activeSurface,
      // Setting the surface *is* opening/focusing its pane; `activeSurface`
      // then follows from the focus rather than being tracked alongside it.
      setActiveSurface: (tab: SurfaceTab) => openPane(tab),
      openPageTitle,
      setOpenPageTitle,
      openPageId,
      setOpenPageId,
      selection,
      setSelection,
      highlightedClaimId,
      setHighlightedClaimId,
    }),
    [panes, focusedPaneId, activeSurface, openPageTitle, openPageId, selection, highlightedClaimId],
  );

  return <WorkspaceUIContext.Provider value={value}>{children}</WorkspaceUIContext.Provider>;
}

export function useWorkspaceUI(): WorkspaceUIState {
  const ctx = useContext(WorkspaceUIContext);
  if (!ctx) {
    throw new Error("useWorkspaceUI must be used within a WorkspaceUIProvider");
  }
  return ctx;
}
