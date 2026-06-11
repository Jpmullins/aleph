/**
 * Workspace-level live-signals context.
 *
 * One `useWikiLiveSignals` subscription (one EventSource on the changes
 * stream) per open project, mounted by ProjectWorkspace — regardless of which
 * right-panel tab is active, every tab's react-query data is invalidated the
 * instant something writes. Surface views read the wiki presence state
 * (editing/updated pulses) via `useLiveSignals()`.
 */
import { createContext, useContext, type ReactNode } from "react";

import { useWikiLiveSignals, type WikiLiveSignals } from "./useWikiLiveSignals";

const LiveSignalsContext = createContext<WikiLiveSignals | null>(null);

export function LiveSignalsProvider({
  projectId,
  children,
}: {
  projectId: string;
  children: ReactNode;
}) {
  const signals = useWikiLiveSignals(projectId);
  return <LiveSignalsContext.Provider value={signals}>{children}</LiveSignalsContext.Provider>;
}

export function useLiveSignals(): WikiLiveSignals {
  const ctx = useContext(LiveSignalsContext);
  if (!ctx) {
    throw new Error("useLiveSignals must be used within a LiveSignalsProvider");
  }
  return ctx;
}
