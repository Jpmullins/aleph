/**
 * CopilotKit v2 chat surface (Wave 2) — the AG-UI + A2UI center panel.
 *
 * Talks to the `assistant` Deep Agent through the Node CopilotRuntime. Unlike
 * the legacy enqueue+poll `ChatSurface`, this streams tokens, tool calls, and
 * A2UI surfaces live over AG-UI, and shares state with the workspace:
 *
 *   - `useAgentContext` tells the agent which right-panel tab / wiki page the
 *     analyst is viewing (plus the current claim/text selection), so it can
 *     answer "what am I looking at?" / "summarize this page" with nothing named.
 *   - `useFrontendTool` (`focus_tab` / `open_page` / `highlight_claim`) lets the
 *     agent drive the workspace; each dispatches through the ledger-audited
 *     action router (`POST /cards/actions`) before applying the UI change.
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
import { useQuery } from "@tanstack/react-query";
import { z } from "zod";

import { SurfaceProvider } from "@/a2ui/surface-context";
import { api } from "@/lib/api";
import { usePaneKinds, useWorkspaceUI } from "@/lib/workspace-ui";

interface CardActionRow {
  id: string;
  surface_kind: string;
  action_kind: string;
  target_kind: string | null;
  result: Record<string, unknown>;
  actor_kind: string;
  created_at: string;
}

interface Props {
  projectId: string;
  threadId: string | null;
}

interface CardActionResult {
  action_id: string;
  ok: boolean;
  result: Record<string, unknown>;
}

export function CopilotChatSurface({ projectId, threadId }: Props) {
  // The agent is told what surfaces exist right now, not a compiled-in list.
  const paneTitles = usePaneKinds(projectId).launchable.map((k) => k.title);
  const {
    activeSurface,
    setActiveSurface,
    openPageTitle,
    openPageId,
    setOpenPageId,
    selection,
    setHighlightedClaimId,
  } = useWorkspaceUI();
  const copilotThreadId = `proj:${projectId}:${threadId ?? "default"}`;

  // Share the analyst's current view with the agent (shared state, UI → agent).
  // Shape: {active_tab, open_page_id, open_page_title, selection}. `open_page_id`
  // being in shared state is what lets "summarize this page" work unnamed.
  useAgentContext({
    description:
      "The analyst's current workspace view: which right-panel surface tab is " +
      "active, which wiki page (if any) they have open, and the claim/text they " +
      "currently have selected in the reader. Use open_page_id/selection to act " +
      'on "this page" / "this claim" without asking which one.',
    value: {
      active_tab: activeSurface,
      open_page_id: openPageId ?? null,
      open_page_title: openPageTitle ?? null,
      selection: selection
        ? { claim_id: selection.claim_id, text: selection.text, page_id: selection.page_id }
        : null,
    },
  });

  // What to tell the agent when a dispatch fails. The agent reads this text, so
  // it has to name the failure rather than say "an error occurred" — the same
  // rule the server-side tool guard follows.
  const describeError = (err: unknown): string =>
    err instanceof Error ? `${err.name}: ${err.message}` : String(err);

  // Dispatch a frontend tool through the ledger-audited action router before
  // applying the local UI change, so every agent-driven workspace move is
  // audited exactly like an analyst's card click.
  const dispatchAction = (
    action_kind: string,
    params: Record<string, unknown>,
  ): Promise<CardActionResult> =>
    api.post<CardActionResult>(`/v1/projects/${projectId}/cards/actions`, {
      surface_kind: "ChatSurface",
      action_kind,
      params,
    });

  // Close the action loop: tell the agent what the analyst recently did on the
  // surfaces (approved/rejected proposals, resolved/dismissed findings, unpins)
  // so it doesn't fire-and-forget its own cards.
  const recentActions = useQuery<CardActionRow[]>({
    queryKey: ["card-actions", projectId],
    queryFn: () => api.get<CardActionRow[]>(`/v1/projects/${projectId}/cards/actions?limit=10`),
    refetchInterval: 15_000,
  });
  useAgentContext({
    description:
      "The analyst's recent card actions on the workspace surfaces (newest " +
      "first): approvals/rejections of proposals, resolved/dismissed review " +
      "findings, unpinned cards. Use this to know the outcome of cards you " +
      "rendered — do not re-ask about decisions already made here.",
    value: (recentActions.data ?? []).map((a) => ({
      action: a.action_kind,
      surface: a.surface_kind,
      target_kind: a.target_kind ?? "",
      outcome: JSON.stringify(a.result),
      at: a.created_at,
    })),
  });

  // Let the agent steer the right panel (agent → UI). `focus_tab` folds the old
  // `open_surface` tool — one canonical tab-switch tool, now ledger-audited.
  useFrontendTool({
    name: "focus_tab",
    description:
      "Switch the analyst's right-hand panel to one of the surface tabs: " +
      `${paneTitles.join(", ")}. Call this when your answer is best explored ` +
      "in a specific panel (e.g. open Hypotheses to show the ACH matrix, or " +
      "Library to show ingested sources and built artifacts).",
    // z.string(), not z.enum: the valid set is whatever is loaded, and a
    // compiled-in enum would refuse a surface a plugin just added.
    parameters: z.object({ tab: z.string() }),
    handler: async ({ tab }) => {
      // Guarded, like every server-side tool call: a frontend tool handler that
      // rejects throws into the AG-UI stream and kills the run. The agent
      // should be told the panel did not open and carry on talking.
      try {
        await dispatchAction("focus_tab", { tab });
      } catch (err) {
        return `Could not open the ${tab} panel: ${describeError(err)}`;
      }
      setActiveSurface(tab);
      return `Opened the ${tab} panel for the analyst.`;
    },
  });

  // Open a wiki page in the reader — by page_id or human-readable slug (the
  // router resolves the slug). Drives the Wiki tab + open page.
  useFrontendTool({
    name: "open_page",
    description:
      "Open a wiki page in the reader on the analyst's Wiki tab. Pass the " +
      "page's UUID as `page_id`, or its `slug` if that's all you have. Use this " +
      "to land the analyst on the exact page you're discussing.",
    parameters: z.object({
      page_id: z.string().optional(),
      slug: z.string().optional(),
    }),
    handler: async ({ page_id, slug }) => {
      const params: Record<string, unknown> = {};
      if (page_id) params.page_id = page_id;
      if (slug) params.slug = slug;
      let res: CardActionResult;
      try {
        res = await dispatchAction("navigate_wiki", params);
      } catch (err) {
        return `Could not open that page: ${describeError(err)}`;
      }
      const nav = res.result?.navigate as { page_id?: string } | undefined;
      const resolvedId = nav?.page_id ?? page_id ?? null;
      setActiveSurface("Wiki");
      if (resolvedId) setOpenPageId(resolvedId);
      return resolvedId
        ? `Opened wiki page ${resolvedId} in the reader.`
        : "Could not resolve that page.";
    },
  });

  // Highlight a claim in the open reader (agent → UI). WikiPageCard rings it.
  useFrontendTool({
    name: "highlight_claim",
    description:
      "Highlight a specific claim in the wiki page open in the reader, so the " +
      "analyst's eye lands on it. Pass the claim's UUID as `claim_id`.",
    parameters: z.object({ claim_id: z.string() }),
    handler: async ({ claim_id }) => {
      try {
        await dispatchAction("highlight_claim", { claim_id });
      } catch (err) {
        return `Could not highlight claim ${claim_id}: ${describeError(err)}`;
      }
      setActiveSurface("Wiki");
      setHighlightedClaimId(claim_id);
      return `Highlighted claim ${claim_id} in the reader.`;
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
