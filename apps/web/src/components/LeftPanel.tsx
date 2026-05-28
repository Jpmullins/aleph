import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { SourceUploadModal } from "@/components/SourceUploadModal";
import { ThemeToggle } from "@/components/ThemeToggle";
import { api } from "@/lib/api";

export interface SessionInfo {
  id: string;
  title: string;
  last_activity_at: string;
}

export type DrawerKind = "settings" | "logs" | "notifications" | "profile";

interface Props {
  projectId: string;
  projectTitle: string;
  sessionId: string | null;
  onBack: () => void;
  onSelectSession: (sessionId: string) => void;
  onOpenDrawer: (kind: DrawerKind) => void;
}

export function LeftPanel({
  projectId,
  projectTitle,
  sessionId,
  onBack,
  onSelectSession,
  onOpenDrawer,
}: Props) {
  const qc = useQueryClient();
  const [showUpload, setShowUpload] = useState(false);

  const sessionsQuery = useQuery<SessionInfo[]>({
    queryKey: ["sessions", projectId],
    queryFn: () => api.get<SessionInfo[]>(`/v1/projects/${projectId}/sessions`),
  });

  const createSession = useMutation({
    mutationFn: async () =>
      api.post<{ session_id: string; thread_id: string }>(
        `/v1/projects/${projectId}/sessions`,
        { title: `Session ${new Date().toLocaleString()}` },
      ),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["sessions", projectId] });
      onSelectSession(r.session_id);
    },
  });

  return (
    <aside className="flex h-full flex-col border-r border-slate-200 bg-white">
      <div className="border-b border-slate-200 px-4 py-3">
        <button
          type="button"
          onClick={onBack}
          className="text-xs font-medium uppercase tracking-wider text-slate-500 hover:text-slate-900"
        >
          ← Projects
        </button>
        <h2 className="mt-1 truncate text-base font-semibold text-slate-900" title={projectTitle}>
          {projectTitle}
        </h2>
      </div>

      <div className="flex items-center justify-between px-4 pt-3 pb-1">
        <p className="text-xs uppercase tracking-wider text-slate-400">Sessions</p>
        <button
          type="button"
          onClick={() => createSession.mutate()}
          disabled={createSession.isPending}
          className="text-xs font-medium text-slate-600 hover:text-slate-900 disabled:opacity-50"
          title="New session"
        >
          + New
        </button>
      </div>
      <ul className="flex-1 overflow-y-auto px-2 pb-3">
        {sessionsQuery.isPending && (
          <li className="px-2 py-2 text-xs text-slate-400">Loading…</li>
        )}
        {sessionsQuery.isSuccess && sessionsQuery.data.length === 0 && (
          <li className="px-2 py-2 text-xs text-slate-400">No sessions. Click + New.</li>
        )}
        {sessionsQuery.data?.map((s) => (
          <li key={s.id}>
            <button
              type="button"
              onClick={() => onSelectSession(s.id)}
              className={
                "block w-full truncate rounded-md px-2 py-1.5 text-left text-sm " +
                (s.id === sessionId
                  ? "bg-slate-900 text-white"
                  : "text-slate-700 hover:bg-slate-100")
              }
              title={s.title}
            >
              {s.title}
            </button>
          </li>
        ))}
      </ul>

      <div className="border-t border-slate-200 px-3 py-2">
        <button
          type="button"
          onClick={() => setShowUpload(true)}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
        >
          + Upload source
        </button>
      </div>

      <div className="flex justify-between border-t border-slate-200 px-4 py-3 text-slate-500">
        <IconButton title="Settings" onClick={() => onOpenDrawer("settings")} label="⚙" />
        <IconButton title="Logs" onClick={() => onOpenDrawer("logs")} label="🗒" />
        <IconButton title="Notifications" onClick={() => onOpenDrawer("notifications")} label="🔔" />
        <IconButton title="Profile" onClick={() => onOpenDrawer("profile")} label="●" />
        <ThemeToggle />
      </div>

      {showUpload && (
        <SourceUploadModal
          projectId={projectId}
          onClose={() => setShowUpload(false)}
          onUploaded={() => {
            setShowUpload(false);
            qc.invalidateQueries({ queryKey: ["agent-runs", projectId] });
          }}
        />
      )}
    </aside>
  );
}

function IconButton({
  title,
  onClick,
  label,
}: {
  title: string;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className="rounded-md px-2 py-1 text-base hover:bg-slate-100 hover:text-slate-900"
    >
      {label}
    </button>
  );
}
