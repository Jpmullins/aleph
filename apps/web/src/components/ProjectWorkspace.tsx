import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";

import { AssistantDock } from "@/components/AssistantDock";
import { ContextBar } from "@/components/ContextBar";
import { PipelineStrip } from "@/components/PipelineStrip";
import { Drawer } from "@/components/Drawers";
import type { DrawerKind } from "@/components/LeftPanel";
import { Rail } from "@/components/Rail";
import { ReadingRegion } from "@/components/ReadingRegion";
import { SourceUploadModal } from "@/components/SourceUploadModal";
import { LiveSignalsProvider } from "@/hooks/live-signals";
import { api, type ProjectOut } from "@/lib/api";
import { getAccessToken } from "@/lib/auth";
import { WorkspaceUIProvider } from "@/lib/workspace-ui";

interface Props {
  projectId: string;
  onBack: () => void;
}

interface ThreadInfo {
  id: string;
  session_id: string;
  parent_thread_id: string | null;
  title: string | null;
}

export function ProjectWorkspace({ projectId, onBack }: Props) {
  const project = useQuery<ProjectOut>({
    queryKey: ["project", projectId],
    queryFn: () => api.get<ProjectOut>(`/v1/projects/${projectId}`),
  });
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<DrawerKind | null>(null);
  const [showUpload, setShowUpload] = useState(false);

  // When the session changes, resolve its primary thread.
  useEffect(() => {
    let cancelled = false;
    if (!sessionId) {
      setThreadId(null);
      return;
    }
    void fetchThreadForSession(sessionId, projectId).then((t) => {
      if (!cancelled) setThreadId(t?.id ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId, projectId]);

  return (
    <WorkspaceUIProvider>
      <LiveSignalsProvider projectId={projectId}>
      {/*
        The workspace is a reading-and-adjudication environment, not a chat app
        with a wiki attached. The old shell said otherwise: chat took 52% and
        the wiki — the stated primary retrieval surface — sat in a 30% strip
        behind a tab bar that could not grow past ~6 tabs.

          rail (objects) │ reading region (the stage) │ assistant (a dock)

        The reading region is now the largest thing on screen and splittable,
        because belief work is comparative: a claim against its source, a brief
        against the page it drifted from. The assistant is a tool you reach for,
        so it collapses to a strip and gives the space back.
      */}
      <div className="flex h-full">
        <Rail onBack={onBack} onOpenDrawer={setDrawer} />
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <ContextBar projectTitle={project.data?.title} onUpload={() => setShowUpload(true)} />
          {/* Corpus-level progress. Sits under the context bar because it
              answers a question about the whole project ("is my library ready
              to ask questions of?"), not about the focused pane. */}
          <PipelineStrip projectId={projectId} />
          <div className="flex min-h-0 flex-1">
            <PanelGroup
              direction="horizontal"
              autoSaveId="aleph-workspace-v2"
              className="flex-1"
            >
              <Panel defaultSize={72} minSize={40} className="min-w-0">
                <main className="flex h-full min-w-0 flex-col bg-canvas">
                  <ReadingRegion projectId={projectId} />
                </main>
              </Panel>
              <PanelResizeHandle className="w-1 cursor-col-resize bg-[var(--border-muted)] transition-colors hover:bg-accent" />
              <Panel
                defaultSize={28}
                minSize={0}
                collapsible
                collapsedSize={0}
                className="min-w-0"
              >
                <aside className="flex h-full min-w-0 flex-col border-l border-line bg-surface">
                  <AssistantDock
                    projectId={projectId}
                    threadId={threadId}
                    sessionId={sessionId}
                    onSelectSession={setSessionId}
                  />
                </aside>
              </Panel>
            </PanelGroup>
          </div>
        </div>
        {drawer && <Drawer kind={drawer} projectId={projectId} onClose={() => setDrawer(null)} />}
        {showUpload && (
          <SourceUploadModal
            projectId={projectId}
            onClose={() => setShowUpload(false)}
            onUploaded={() => setShowUpload(false)}
          />
        )}
      </div>
      </LiveSignalsProvider>
    </WorkspaceUIProvider>
  );
}

async function fetchThreadForSession(
  sessionId: string,
  projectId: string,
): Promise<ThreadInfo | null> {
  const token = await getAccessToken();
  const baseUrl =
    (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
  const resp = await fetch(
    `${baseUrl}/v1/projects/${projectId}/sessions/${sessionId}/threads`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  );
  if (!resp.ok) return null;
  const ts = (await resp.json()) as ThreadInfo[];
  return ts.length > 0 ? ts[0] : null;
}
