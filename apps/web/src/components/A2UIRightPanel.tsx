import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { A2UIComponent } from "@/a2ui/catalog";
import { SpikePanel } from "@/a2ui/_spike/SpikePanel";
import { SurfaceProvider, renderA2UI } from "@/a2ui/register";
import { api } from "@/lib/api";
import { SURFACE_TABS, useWorkspaceUI } from "@/lib/workspace-ui";

const TABS = SURFACE_TABS;

interface Props {
  projectId: string;
}

export function A2UIRightPanel({ projectId }: Props) {
  // Wave 4 Task 1 spike: behind `?spike=1`, render the v0_9 A2uiSurface spike
  // instead of the normal panel (proves the shared-catalog -> A2uiSurface path).
  if (new URLSearchParams(window.location.search).get("spike") === "1") {
    return <SpikePanel />;
  }
  return <RealPanel projectId={projectId} />;
}

function RealPanel({ projectId }: Props) {
  const qc = useQueryClient();
  // Tab state is shared so the assistant agent can drive it (useFrontendTool).
  const { activeSurface: tab, setActiveSurface: setTab } = useWorkspaceUI();

  const surfaceQuery = useQuery<{ tab: string; surface: A2UIComponent }>({
    queryKey: ["surface", projectId, tab],
    queryFn: () =>
      api.get<{ tab: string; surface: A2UIComponent }>(
        `/v1/projects/${projectId}/surfaces/${tab.toLowerCase()}`,
      ),
    refetchInterval: tab === "Briefs" ? 10_000 : false,
  });

  const action = useMutation({
    mutationFn: async ({
      actionName,
      params,
    }: {
      actionName: string;
      params: Record<string, unknown>;
    }) =>
      api.post(`/v1/projects/${projectId}/cards/actions`, {
        surface_kind: `${tab}Surface`,
        action_kind: actionName,
        target_id: (params.target_id as string | undefined) ?? null,
        target_kind: (params.target_kind as string | undefined) ?? null,
        params,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["surface", projectId, tab] });
    },
  });

  return (
    <aside className="flex w-[28rem] flex-col border-l border-slate-200 bg-white">
      <nav className="flex border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={
              "flex-1 px-2 py-2 text-xs font-medium " +
              (t === tab
                ? "border-b-2 border-slate-900 text-slate-900"
                : "text-slate-500 hover:text-slate-900")
            }
          >
            {t}
          </button>
        ))}
      </nav>
      <div className="flex-1 overflow-y-auto">
        {surfaceQuery.isPending && (
          <div className="p-6 text-sm text-slate-500">Loading surface…</div>
        )}
        {surfaceQuery.data && (
          <SurfaceProvider projectId={projectId} surface={`${tab}Surface`}>
            {renderA2UI(surfaceQuery.data.surface, (actionName, params) =>
              action.mutate({ actionName, params }),
            )}
          </SurfaceProvider>
        )}
      </div>
    </aside>
  );
}
