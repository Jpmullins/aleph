import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ChatSurface } from "@/components/ChatSurface";
import { CostBanner } from "@/components/CostBanner";
import { SourcesTab } from "@/components/SourcesTab";
import { WikiTab } from "@/components/WikiTab";
import { api, type ProjectOut } from "@/lib/api";

interface Props {
  projectId: string;
  onBack: () => void;
}

// Spec keeps the right panel at 5 tabs (Wiki | Artifacts | Notes | Hypotheses |
// Briefs). Sources are exposed as a debug/management view nested under the
// Wiki tab via a small toggle since `SourcePage`s live in the wiki.
const TABS = ["Wiki", "Sources", "Artifacts", "Notes", "Hypotheses", "Briefs"] as const;
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
        <CenterPanel projectId={projectId} />
        <RightPanel projectId={projectId} tab={tab} setTab={setTab} />
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

function RightPanel({
  projectId,
  tab,
  setTab,
}: {
  projectId: string;
  tab: Tab;
  setTab: (t: Tab) => void;
}) {
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
      <div className="flex-1 overflow-hidden">
        {tab === "Wiki" && <WikiTab projectId={projectId} />}
        {tab === "Sources" && (
          <SourcesTab
            projectId={projectId}
            onOpenSource={(_id) => {
              // Inc 1 ships source detail behind /sources/$id navigation;
              // for now we keep the analyst inside the tab and a future
              // commit adds inline detail.
            }}
          />
        )}
        {tab === "Artifacts" && <Placeholder name="Artifacts" inc={7} />}
        {tab === "Notes" && <Placeholder name="Notes" inc={4} />}
        {tab === "Hypotheses" && <Placeholder name="Hypotheses" inc={5} />}
        {tab === "Briefs" && <Placeholder name="Briefs" inc={5} />}
      </div>
    </aside>
  );
}

function Placeholder({ name, inc }: { name: string; inc: number }) {
  return (
    <div className="flex-1 overflow-y-auto p-6 text-sm text-slate-500">
      <p className="mb-2 text-xs uppercase tracking-wider text-slate-400">{name} surface</p>
      <p>The {name} surface lands in Increment {inc}.</p>
    </div>
  );
}
