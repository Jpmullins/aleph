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

export const SURFACE_TABS = ["Wiki", "Artifacts", "Notes", "Hypotheses", "Briefs"] as const;
export type SurfaceTab = (typeof SURFACE_TABS)[number];

export interface WorkspaceUIState {
  /** Which right-panel surface tab is active. */
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
}

const WorkspaceUIContext = createContext<WorkspaceUIState | null>(null);

export function WorkspaceUIProvider({ children }: { children: ReactNode }) {
  const [activeSurface, setActiveSurface] = useState<SurfaceTab>("Wiki");
  const [openPageTitle, setOpenPageTitle] = useState<string | null>(null);
  const [openPageId, setOpenPageId] = useState<string | null>(null);

  const value = useMemo<WorkspaceUIState>(
    () => ({
      activeSurface,
      setActiveSurface,
      openPageTitle,
      setOpenPageTitle,
      openPageId,
      setOpenPageId,
    }),
    [activeSurface, openPageTitle, openPageId],
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
