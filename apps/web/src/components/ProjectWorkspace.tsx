import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { CostBanner } from "@/components/CostBanner";
import { api, type ProjectOut } from "@/lib/api";

interface Props {
  projectId: string;
  onBack: () => void;
}

const TABS = ["Wiki", "Artifacts", "Notes", "Hypotheses", "Briefs"] as const;
type Tab = (typeof TABS)[number];

export function ProjectWorkspace({ projectId, onBack }: Props) {
  const [tab, setTab] = useState<Tab>("Wiki");
  const project = useQuery<ProjectOut>({
    queryKey: ["project", projectId],
    queryFn: () => api.get<ProjectOut>(`/v1/projects/${projectId}`),
  });

  return (
    <div className="flex h-full flex-col">
      <CostBanner projectId={projectId} />
      <div className="flex min-h-0 flex-1">
        <LeftPanel onBack={onBack} title={project.data?.title ?? "—"} />
        <CenterPanel />
        <RightPanel tab={tab} setTab={setTab} />
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

function CenterPanel() {
  return (
    <main className="flex min-w-0 flex-1 flex-col bg-slate-50">
      <div className="flex-1 overflow-y-auto p-6">
        <div className="rounded-lg border border-dashed border-slate-300 p-8 text-center text-slate-500">
          Chat lands in Increment 2.
        </div>
      </div>
      <div className="border-t border-slate-200 bg-white p-4 text-sm text-slate-500">
        <span className="font-medium text-slate-700">Activity</span> — no agents running.
      </div>
    </main>
  );
}

function RightPanel({ tab, setTab }: { tab: Tab; setTab: (t: Tab) => void }) {
  return (
    <aside className="flex w-96 flex-col border-l border-slate-200 bg-white">
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
      <div className="flex-1 overflow-y-auto p-6 text-sm text-slate-500">
        <p className="mb-2 text-xs uppercase tracking-wider text-slate-400">{tab} surface</p>
        <p>The {tab} surface lands in a later increment (A2UI surfaces — Inc 4).</p>
      </div>
    </aside>
  );
}
