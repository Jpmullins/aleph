import { useQuery } from "@tanstack/react-query";

import { A2UISurfaceView } from "@/a2ui/A2UISurfaceView";
import { api } from "@/lib/api";
import { SURFACE_TABS, useWorkspaceUI } from "@/lib/workspace-ui";

const TABS = SURFACE_TABS;

interface Props {
  projectId: string;
}

export function A2UIRightPanel({ projectId }: Props) {
  return <RealPanel projectId={projectId} />;
}

function RealPanel({ projectId }: Props) {
  // Tab state is shared so the assistant agent can drive it (useFrontendTool).
  const { activeSurface: tab, setActiveSurface: setTab } = useWorkspaceUI();

  // Wave 4 T3: EVERY tab is rendered through the upstream v0.9
  // MessageProcessor + <A2uiSurface> against the shared `aleph://v1` catalog.
  // The endpoint returns `{ tab, messages: [...] }` (a `createSurface` +
  // `updateComponents` for that tab's single surface component). The legacy
  // `renderA2UI` path is retired from the panel; `register.tsx` remains only
  // to host the `SurfaceProvider` context (Task 7 re-homes it).
  const messagesQuery = useQuery<{ tab: string; messages: unknown[] }>({
    queryKey: ["surface-v09", projectId, tab],
    queryFn: () =>
      api.get<{ tab: string; messages: unknown[] }>(
        `/v1/projects/${projectId}/surfaces/${tab.toLowerCase()}`,
      ),
    // Briefs polls so freshly-promoted notes / synthesis proposals appear.
    refetchInterval: tab === "Briefs" ? 10_000 : false,
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
        {messagesQuery.isPending && (
          <div className="p-6 text-sm text-slate-500">Loading surface…</div>
        )}
        {messagesQuery.data && (
          <A2UISurfaceView
            key={tab}
            messages={messagesQuery.data.messages}
            projectId={projectId}
            surface={`${tab}Surface`}
          />
        )}
      </div>
    </aside>
  );
}
