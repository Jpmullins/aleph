/**
 * The assistant, docked.
 *
 * Chat used to own 52% of the workspace, which said it was the product. It
 * isn't — it is one tool in the analyst's loop (feed → read → interrogate →
 * compare → decide). So it lives in a dock: present, collapsible, and it gives
 * the space back to the reading region when you are not talking to it.
 *
 * Session switching moved in here too. Sessions are a property of the
 * conversation, not of the project, so they belong next to the conversation
 * rather than in a dedicated panel of their own.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { CopilotChatSurface } from "@/components/CopilotChatSurface";
import { Icons } from "@/components/Icons";
import { api } from "@/lib/api";

export interface SessionInfo {
  id: string;
  title: string;
  last_activity_at: string;
}

interface Props {
  projectId: string;
  threadId: string | null;
  sessionId: string | null;
  onSelectSession: (sessionId: string) => void;
}

export function AssistantDock({ projectId, threadId, sessionId, onSelectSession }: Props) {
  const qc = useQueryClient();

  const sessions = useQuery<SessionInfo[]>({
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

  const active = sessions.data?.find((s) => s.id === sessionId);

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="assistant-dock">
      <header className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span className="text-[11px] font-bold uppercase tracking-[0.12em] text-ink-muted">
          Assistant
        </span>
        <span
          className="min-w-0 flex-1 truncate text-xs text-ink-muted"
          title={active?.title}
        >
          {active?.title ?? "no session"}
        </span>
        <select
          aria-label="Session"
          data-testid="dock-session-select"
          className="max-w-[7rem] truncate  border border-line bg-sunken px-1.5 py-0.5 text-xs text-ink-soft"
          value={sessionId ?? ""}
          onChange={(e) => onSelectSession(e.target.value)}
        >
          {!sessionId && <option value="">—</option>}
          {sessions.data?.map((s) => (
            <option key={s.id} value={s.id}>
              {s.title}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => createSession.mutate()}
          disabled={createSession.isPending}
          aria-label="New session"
          title="New session"
          data-testid="dock-new-session"
          className="grid h-7 w-7 place-items-center  text-ink-muted hover:bg-sunken hover:text-ink disabled:opacity-50"
        >
          <Icons.plus size={15} />
        </button>
      </header>

      <div className="min-h-0 flex-1">
        <CopilotChatSurface projectId={projectId} threadId={threadId} />
      </div>
    </div>
  );
}
