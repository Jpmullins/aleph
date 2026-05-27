import { useQuery } from "@tanstack/react-query";

import { A2UIRightPanel } from "@/components/A2UIRightPanel";
import { ChatSurface } from "@/components/ChatSurface";
import { CostBanner } from "@/components/CostBanner";
import { api, type ProjectOut } from "@/lib/api";

interface Props {
  projectId: string;
  onBack: () => void;
}

export function ProjectWorkspace({ projectId, onBack }: Props) {
  const project = useQuery<ProjectOut>({
    queryKey: ["project", projectId],
    queryFn: () => api.get<ProjectOut>(`/v1/projects/${projectId}`),
  });

  return (
    <div className="flex h-full flex-col">
      <CostBanner projectId={projectId} />
      <div className="flex min-h-0 flex-1">
        <LeftPanel onBack={onBack} title={project.data?.title ?? "—"} />
        <CenterPanel projectId={projectId} />
        <A2UIRightPanel projectId={projectId} />
      </div>
    </div>
  );
}

function LeftPanel({ onBack, title }: { onBack: () => void; title: string }) {
  return (
    <aside className="flex w-60 flex-col border-r border-slate-200 bg-white">
      <div className="border-b border-slate-200 px-4 py-3">
        <button
          type="button"
          onClick={onBack}
          className="text-xs font-medium uppercase tracking-wider text-slate-500 hover:text-slate-900"
        >
          ← Projects
        </button>
        <h2 className="mt-1 truncate text-base font-semibold text-slate-900">{title}</h2>
      </div>
      <div className="flex-1 overflow-y-auto px-4 py-3 text-sm text-slate-500">
        <p className="text-xs uppercase tracking-wider text-slate-400">Sessions</p>
        <p className="mt-2 text-slate-400">No sessions yet.</p>
      </div>
      <div className="flex justify-between border-t border-slate-200 px-4 py-3 text-slate-400">
        <button type="button" title="Settings" className="hover:text-slate-900">⚙</button>
        <button type="button" title="Logs" className="hover:text-slate-900">◐</button>
        <button type="button" title="Notifications" className="hover:text-slate-900">🔔</button>
        <button type="button" title="Profile" className="hover:text-slate-900">●</button>
      </div>
    </aside>
  );
}

function CenterPanel({ projectId }: { projectId: string }) {
  return (
    <main className="flex min-w-0 flex-1 flex-col bg-slate-50">
      <ChatSurface projectId={projectId} />
    </main>
  );
}
