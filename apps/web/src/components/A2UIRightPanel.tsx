import { A2UIStreamSurfaceView } from "@/a2ui/A2UISurfaceView";
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

  // Wave 4 T6: every tab is rendered through the delta SurfaceStreamer. The
  // `…/surfaces/{tab}/stream` SSE endpoint emits the full v0.9 surface on
  // connect (createSurface + updateComponents + root updateDataModel), then
  // incremental `updateDataModel` deltas as the underlying data changes. A
  // persistent MessageProcessor (one per connection, keyed by `tab`) applies
  // those deltas in place — so e.g. a new hypothesis appears via an
  // `add`/`updateComponents` delta without re-mounting existing card DOM. The
  // four self-fetching tabs (Wiki/Artifacts/Notes/Briefs) carry no bound data
  // model, so their stream emits the structural surface once and then idles;
  // they self-refresh via react-query inside their own surface views.
  const baseUrl =
    (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
  const streamUrl = `${baseUrl}/v1/projects/${projectId}/surfaces/${tab.toLowerCase()}/stream`;

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
        <A2UIStreamSurfaceView
          key={tab}
          streamUrl={streamUrl}
          projectId={projectId}
          surface={`${tab}Surface`}
        />
      </div>
    </aside>
  );
}
