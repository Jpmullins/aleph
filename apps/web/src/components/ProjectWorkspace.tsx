import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";

import { ActivityCard } from "@/components/ActivityCard";
import { A2UIRightPanel } from "@/components/A2UIRightPanel";
import { ChatSurface } from "@/components/ChatSurface";
import { CopilotChatSurface } from "@/components/CopilotChatSurface";
import { CostBanner } from "@/components/CostBanner";
import { Drawer } from "@/components/Drawers";
import { LeftPanel, type DrawerKind } from "@/components/LeftPanel";
import { api, type ProjectOut } from "@/lib/api";
import { getAccessToken } from "@/lib/auth";
import { WorkspaceUIProvider } from "@/lib/workspace-ui";

type ChatMode = "live" | "classic";

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
  // "classic" = legacy enqueue+poll chat (default until the live path reaches
  // parity); "live" = CopilotKit v2 / AG-UI streaming Deep Agent with
  // generative A2UI cards. Additive — both are reachable via the toggle.
  const [chatMode, setChatMode] = useState<ChatMode>("classic");

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
      <div className="flex h-full flex-col">
        <CostBanner projectId={projectId} />
        <div className="flex min-h-0 flex-1">
          <PanelGroup direction="horizontal" autoSaveId="aleph-workspace-layout" className="flex-1">
            <Panel defaultSize={18} minSize={14} maxSize={30} className="min-w-0">
              <LeftPanel
                projectId={projectId}
                projectTitle={project.data?.title ?? "—"}
                sessionId={sessionId}
                onBack={onBack}
                onSelectSession={setSessionId}
                onOpenDrawer={setDrawer}
              />
            </Panel>
            <PanelResizeHandle className="w-1 cursor-col-resize bg-slate-200 transition-colors hover:bg-slate-400" />
            <Panel defaultSize={52} minSize={30} className="min-w-0">
              <main className="flex h-full min-w-0 flex-col bg-slate-50">
                <div className="flex items-center justify-end gap-1 border-b border-slate-200 bg-white px-3 py-1.5">
                  <ChatModeToggle mode={chatMode} onChange={setChatMode} />
                </div>
                <ActivityCard projectId={projectId} />
                <div className="min-h-0 flex-1">
                  {chatMode === "live" ? (
                    <CopilotChatSurface projectId={projectId} threadId={threadId} />
                  ) : (
                    <ChatSurface projectId={projectId} threadId={threadId} />
                  )}
                </div>
              </main>
            </Panel>
            <PanelResizeHandle className="w-1 cursor-col-resize bg-slate-200 transition-colors hover:bg-slate-400" />
            <Panel defaultSize={30} minSize={22} maxSize={50} className="min-w-0">
              <A2UIRightPanel projectId={projectId} />
            </Panel>
          </PanelGroup>
        </div>
        {drawer && (
          <Drawer kind={drawer} projectId={projectId} onClose={() => setDrawer(null)} />
        )}
      </div>
    </WorkspaceUIProvider>
  );
}

function ChatModeToggle({
  mode,
  onChange,
}: {
  mode: ChatMode;
  onChange: (m: ChatMode) => void;
}) {
  const opts: Array<{ value: ChatMode; label: string; title: string }> = [
    { value: "live", label: "Live", title: "Streaming AG-UI agent with generative cards" },
    { value: "classic", label: "Classic", title: "Legacy enqueue + poll chat" },
  ];
  return (
    <div
      className="inline-flex rounded-md border border-slate-200 p-0.5 text-[11px]"
      data-testid="chat-mode-toggle"
    >
      {opts.map((o) => (
        <button
          key={o.value}
          type="button"
          title={o.title}
          onClick={() => onChange(o.value)}
          data-testid={`chat-mode-${o.value}`}
          className={
            "rounded px-2 py-0.5 font-medium transition-colors " +
            (mode === o.value
              ? "bg-slate-900 text-white"
              : "text-slate-500 hover:text-slate-900")
          }
        >
          {o.label}
        </button>
      ))}
    </div>
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
