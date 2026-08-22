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
import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

import { api } from "@/lib/api";

/**
 * What surfaces exist — served by the API, not compiled in here.
 *
 * This was a hardcoded object listing Wiki, Library, Notes, Hypotheses, Briefs.
 * Making it a registry instead of a union fixed the TYPE and left the CONTENTS
 * exactly as wrong: those five are the research plugin suite, and a client that
 * knows their names cannot render a workbench whose abilities arrive at runtime.
 * Install something unrelated to papers and it had nowhere to appear; remove the
 * research suite and the rail still advertised it.
 *
 * `GET /v1/projects/{id}/panes` is now the source. The client renders whatever
 * it is handed and knows none of the names in advance — which is why
 * `SurfaceTab` is a plain string rather than a union. A static union over a set
 * the server decides is a lie the compiler will happily tell you.
 *
 * There is no first-paint fallback list. There used to be a one-element one
 * naming `wiki`, described as "not a second source of truth" — but a fallback
 * that names a surface IS the client deciding what a workbench opens with, and
 * `check-pane-registry.sh` could not see it because the sweep tolerated one
 * registry id per file. Until the server answers, the honest rail is empty.
 */
export interface PaneKindDef {
  readonly id: string;
  readonly title: string;
  readonly icon: string;
  readonly launchable: boolean;
  readonly params: readonly string[];
  /** Which suite contributed it. */
  readonly source?: string;
}

/** A pane kind's wire id. Not a union — the server owns the set. */
export type SurfaceTab = string;

/** Stable identity so `usePaneKinds`' memo does not churn before the fetch lands. */
const NO_PANES: readonly PaneKindDef[] = [];

/**
 * The surfaces this project can open, from the server.
 *
 * Cached for the session: the set changes when a plugin is enabled or disabled,
 * which is a deliberate act, not something to poll for. When plugin activation
 * lands it should invalidate this key rather than lowering the interval.
 */
export function usePaneKinds(projectId: string | null) {
  const q = useQuery<{ panes: PaneKindDef[] }>({
    queryKey: ["panes", projectId],
    queryFn: () => api.get<{ panes: PaneKindDef[] }>(`/v1/projects/${projectId}/panes`),
    enabled: Boolean(projectId),
    staleTime: Infinity,
  });
  const panes: readonly PaneKindDef[] = q.data?.panes ?? NO_PANES;
  return useMemo(
    () => ({
      all: panes,
      /** What the rail offers. */
      launchable: panes.filter((p) => p.launchable),
      /** A pane kind by its wire id — the rail's title for `activeSurface`. */
      byId: (id: string) => panes.find((p) => p.id === id),
      /** Narrows an incoming name against what the SERVER said exists. */
      isPaneKind: (value: string) => panes.some((p) => p.id === value),
      loading: q.isPending,
    }),
    [panes, q.isPending],
  );
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
/**
 * Kept as a runaway guard, not a layout constraint.
 *
 * It was 3 because three columns fitted, and a fourth made every pane too
 * narrow to read. The Board is a canvas — blocks have positions, not columns —
 * so that reason is gone. The transport reason is gone too: panes share one
 * multiplexed SSE connection, so N panes is one connection, not N.
 *
 * A ceiling still exists only so a stuck loop cannot open blocks forever.
 */
export const MAX_PANES = 24;

/**
 * Normalise an incoming pane name onto the server's pane id.
 *
 * Lower-casing is all this does, and all it may do. The server's ids are
 * lower-case and `_parse_pane_specs` lower-cases the tab it reads, so this
 * folds the title-cased names the action router still answers with ("Wiki",
 * "Grounding") onto the id the registry declares.
 *
 * What it must never do is DERIVE an id from a title. That was the defect: the
 * rail passed `kind.title` in here, so a pane registered as `dispute-queue`
 * titled "Dispute Queue" minted the pane id `dispute queue`, the server dropped
 * it as an unknown tab, and the pane streamed nothing — with no error anywhere,
 * because dropping one unknown pane out of a URL is the correct behaviour. Every
 * core pane happens to be a single lower-case word, so nothing noticed. The rail
 * passes `kind.id` now.
 */
function paneKindId(kind: SurfaceTab): string {
  return kind.toLowerCase();
}

function paneKey(kind: SurfaceTab, params?: Record<string, string>): string {
  const p = params ? Object.entries(params).sort().map(([k, v]) => `${k}=${v}`).join("&") : "";
  const id = paneKindId(kind);
  return p ? `${id}:${p}` : id;
}

const WorkspaceUIContext = createContext<WorkspaceUIState | null>(null);

export function WorkspaceUIProvider({ children }: { children: ReactNode }) {
  // Starts EMPTY, and the Board says so in words. Seeding a pane here means
  // naming one, and this client is not allowed to know a surface name — `Rail`
  // opens the first surface the SERVER offers, once it has been told what
  // exists.
  const [panes, setPanes] = useState<Pane[]>([]);
  const [focusedPaneId, setFocusedPaneId] = useState<string>("");

  /**
   * Open a pane, or focus it if that exact view is already open.
   *
   * Re-opening the same thing must never duplicate it — that is how a pane
   * workspace turns into clutter — so identity is (kind, params), not a fresh
   * id per click.
   */
  const openPane = (kind: SurfaceTab, opts: { title?: string; params?: Record<string, string> } = {}) => {
    const kindId = paneKindId(kind);
    const id = paneKey(kind, opts.params);
    setPanes((prev) => {
      if (prev.some((p) => p.id === id)) return prev;
      // A pane of this kind that is merely showing something (`wiki:page_id=…`)
      // is still that pane. Matching on the exact id alone opened a SECOND Wiki
      // pane every time a card's `open` action fired, because the action
      // handler re-keys the pane and then asks for the surface by name.
      if (!opts.params) {
        const existing = prev.find((p) => p.kind === kindId);
        if (existing) {
          setFocusedPaneId(existing.id);
          return prev;
        }
      }
      // `kind` is stored NORMALISED. Stored raw, `openPane("Wiki")` from the
      // action router and `openPane("wiki")` from the rail become two panes
      // claiming one id, and the `p.kind === kindId` match above finds neither.
      const next = [...prev, { id, kind: kindId, title: opts.title ?? kind, params: opts.params }];
      // Oldest unfocused pane makes way rather than refusing the open — the
      // user asked for this view and should get it.
      return next.length > MAX_PANES ? next.slice(next.length - MAX_PANES) : next;
    });
    setFocusedPaneId(id);
  };

  const closePane = (id: string) => {
    setPanes((prev) => {
      const next = prev.filter((p) => p.id !== id);
      // The "never leave an empty stage" fallback that used to sit here
      // re-created a pane BY NAME, which is the client deciding what a
      // workbench opens with. `Rail` re-seeds from the server's own list, and
      // until it does the Board has an empty state that says what to do.
      setFocusedPaneId((cur) => (cur === id ? (next.at(-1)?.id ?? "") : cur));
      return next;
    });
  };

  // DERIVED, not stored. Its own docstring calls it "the focused pane's kind",
  // and holding it separately let the two drift: the rail opened a pane and
  // focused it while `activeSurface` stayed on Wiki, so the context bar — and
  // therefore the `active_tab` the agent is told about — reported a surface the
  // analyst was no longer looking at.
  const activeSurface: SurfaceTab =
    panes.find((p) => p.id === focusedPaneId)?.kind ?? panes[0]?.kind ?? "";
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
  const setOpenPageId = (id: string | null) => setOpenPageIdState(id);
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
