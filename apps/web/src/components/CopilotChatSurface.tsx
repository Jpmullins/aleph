/**
 * CopilotKit v2 chat surface (Wave 2) — the AG-UI + A2UI center panel.
 *
 * Talks to the `assistant` Deep Agent through the Node CopilotRuntime. Unlike
 * the legacy enqueue+poll `ChatSurface`, this streams tokens, tool calls, and
 * A2UI surfaces live over AG-UI, and shares state with the workspace:
 *
 *   - `useAgentContext` tells the agent which right-panel tab / wiki page the
 *     analyst is viewing, so it can answer "what am I looking at?".
 *   - `useFrontendTool` (`open_surface`) lets the agent drive the right panel.
 *   - Project scope rides on the thread id (`proj:<projectId>:<thread>`), the
 *     only channel `ag-ui-langgraph` threads into the graph config.
 *
 * `SurfaceProvider` supplies `projectId`/`surface` to any Aleph cards the agent
 * emits inline, so their actions route through Aleph's ActionRouter
 * (`POST /cards/actions`). The cards come from the shared v0_9 catalog
 * (`a2ui/aleph-catalog-v09.tsx` via `buildAlephCatalog`) — the same one the
 * right panel renders.
 */
import { CopilotChat, useAgentContext, useFrontendTool } from "@copilotkit/react-core/v2";
import { z } from "zod";

import { SurfaceProvider } from "@/a2ui/surface-context";
import { SURFACE_TABS, useWorkspaceUI } from "@/lib/workspace-ui";

interface Props {
  projectId: string;
  threadId: string | null;
}

export function CopilotChatSurface({ projectId, threadId }: Props) {
  const { activeSurface, setActiveSurface, openPageTitle } = useWorkspaceUI();
  const copilotThreadId = `proj:${projectId}:${threadId ?? "default"}`;

  // Share the analyst's current view with the agent (shared state, UI → agent).
  useAgentContext({
    description:
      "The analyst's current workspace view: which right-panel surface tab is " +
      "active and which wiki page (if any) they have open.",
    value: { activeSurface, openPageTitle: openPageTitle ?? "(none)" },
  });

  // Let the agent steer the right panel (shared state, agent → UI).
  useFrontendTool({
    name: "open_surface",
    description:
      "Switch the analyst's right-hand panel to one of the surface tabs: " +
      `${SURFACE_TABS.join(", ")}. Call this when your answer is best explored ` +
      "in a specific panel (e.g. open Hypotheses to show the ACH matrix, or " +
      "Artifacts to show a generated chart).",
    parameters: z.object({ tab: z.enum(SURFACE_TABS) }),
    handler: async ({ tab }) => {
      setActiveSurface(tab);
      return `Opened the ${tab} panel for the analyst.`;
    },
  });

  return (
    <SurfaceProvider projectId={projectId} surface="ChatSurface">
      <div className="aleph-live-chat flex h-full min-h-0 flex-col">
        <CopilotChat
          agentId="assistant"
          threadId={copilotThreadId}
          style={{ flex: 1, minHeight: 0 }}
        />
      </div>
    </SurfaceProvider>
  );
}
