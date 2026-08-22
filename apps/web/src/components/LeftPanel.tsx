import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

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
    <aside className="flex h-full flex-col border-r border-line bg-surface">
      <div className="border-b border-line px-4 py-3">
        <button
          type="button"
          onClick={onBack}
          className="text-xs font-medium uppercase tracking-wider text-ink-muted hover:text-ink"
        >
          ← Projects
        </button>
        <h2 className="mt-1 truncate text-base font-semibold text-ink" title={projectTitle}>
          {projectTitle}
        </h2>
      </div>

      <div className="flex items-center justify-between px-4 pt-3 pb-1">
        <p className="text-xs uppercase tracking-wider text-ink-muted">Sessions</p>
        <button
          type="button"
          onClick={() => createSession.mutate()}
          disabled={createSession.isPending}
          className="text-xs font-medium text-ink-soft hover:text-ink disabled:opacity-50"
          title="New session"
        >
          + New
        </button>
      </div>
      <ul className="flex-1 overflow-y-auto px-2 pb-3">
        {sessionsQuery.isPending && (
          <li className="px-2 py-2 text-xs text-ink-muted">Loading…</li>
        )}
        {sessionsQuery.isSuccess && sessionsQuery.data.length === 0 && (
          <li className="px-2 py-2 text-xs text-ink-muted">No sessions. Click + New.</li>
        )}
        {sessionsQuery.data?.map((s) => (
          <li key={s.id}>
            <button
              type="button"
              onClick={() => onSelectSession(s.id)}
              className={
                "block w-full truncate px-2 py-1.5 text-left text-sm " +
                (s.id === sessionId
                  ? "bg-ink text-ink-inverse"
                  : "text-ink-soft hover:bg-elevated")
              }
              title={s.title}
            >
              {s.title}
            </button>
          </li>
        ))}
      </ul>

      <div className="border-t border-line px-3 py-2">
        <button
          type="button"
          onClick={() => setShowUpload(true)}
          className="w-full border border-line-strong px-2 py-1.5 text-xs font-medium text-ink-soft hover:bg-sunken"
        >
          + Upload source
        </button>
      </div>

      <div className="flex justify-between border-t border-line px-4 py-3 text-ink-muted">
        <IconButton title="Settings" onClick={() => onOpenDrawer("settings")} label="⚙" />
        <IconButton title="Logs" onClick={() => onOpenDrawer("logs")} label="🗒" />
        <IconButton
          title="Notifications"
          onClick={() => onOpenDrawer("notifications")}
          label={
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-4 w-4"
              aria-hidden="true"
            >
              <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
              <path d="M13.73 21a2 2 0 0 1-3.46 0" />
            </svg>
          }
        />
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
  label: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className="inline-flex items-center justify-center px-2 py-1 text-base hover:bg-elevated hover:text-ink"
    >
      {label}
    </button>
  );
}
