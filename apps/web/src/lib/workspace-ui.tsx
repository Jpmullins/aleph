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

export const SURFACE_TABS = ["Wiki", "Library", "Notes", "Hypotheses", "Briefs"] as const;
export type SurfaceTab = (typeof SURFACE_TABS)[number];

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
