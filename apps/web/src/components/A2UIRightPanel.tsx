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
  const { activeSurface: tab, setActiveSurface: setTab, openPageId } = useWorkspaceUI();

  // WP-4: every canonical tab is server-built + data-bound and rendered through
  // the delta SurfaceStreamer. The `…/surfaces/{tab}/stream` SSE endpoint emits
  // the full v0.9 surface on connect (createSurface + updateComponents + root
  // updateDataModel), then incremental `updateDataModel` deltas woken by
  // LISTEN/NOTIFY. A persistent MessageProcessor applies those deltas in place,
  // so a hypothesis edit / new note patches the bound prop without re-mounting.
  //
  // Opening a wiki page is an `open` action that sets `openPageId`; we thread it
  // into the Wiki stream as `?page_id=`, so the surface builder populates the
  // bound `open` reader payload. The `key` includes it so switching pages
  // re-streams cleanly (a fresh connection = fresh full snapshot).
  const baseUrl =
    (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
  const tabLc = tab.toLowerCase();
  const pageQuery = tabLc === "wiki" && openPageId ? `?page_id=${encodeURIComponent(openPageId)}` : "";
  const streamUrl = `${baseUrl}/v1/projects/${projectId}/surfaces/${tabLc}/stream${pageQuery}`;
  const streamKey = tabLc === "wiki" ? `${tab}:${openPageId ?? ""}` : tab;

  return (
    <aside className="flex h-full min-h-0 w-full min-w-0 flex-col border-l border-slate-200 bg-white">
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
      <div className="min-h-0 flex-1 overflow-y-auto">
        <A2UIStreamSurfaceView
          key={streamKey}
          streamUrl={streamUrl}
          projectId={projectId}
          surface={`${tab}Surface`}
        />
      </div>
    </aside>
  );
}
