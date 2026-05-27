import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { WikiBodyMarkdown } from "@/components/WikiBodyMarkdown";
import { api, type ApiError } from "@/lib/api";
import { getAccessToken } from "@/lib/auth";

interface Message {
  id: string;
  thread_id: string;
  ordinal: number;
  role: "user" | "assistant" | "system";
  body_md: string;
  status: "streaming" | "complete" | "failed" | "budget_blocked";
  retrieval_jsonb: {
    coverage_judgment?: string;
    selected_pages?: Array<{ page_id: string; title: string }>;
    descent_chunks?: Array<{ source_short_id: string; section_path: string | null }>;
  };
  cost_usd: string;
  latency_ms: number | null;
  created_at: string;
}

interface SessionInfo {
  id: string;
  title: string;
  last_activity_at: string;
}

interface ThreadInfo {
  id: string;
  session_id: string;
  parent_thread_id: string | null;
  title: string | null;
}

interface Props {
  projectId: string;
}

export function ChatSurface({ projectId }: Props) {
  const qc = useQueryClient();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [composer, setComposer] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  // Sessions list — we auto-open the most recent or create one.
  const sessions = useQuery<SessionInfo[]>({
    queryKey: ["sessions", projectId],
    queryFn: () => api.get<SessionInfo[]>(`/v1/projects/${projectId}/sessions`),
  });

  const createSession = useMutation({
    mutationFn: async () => {
      const r = await api.post<{ session_id: string; thread_id: string }>(
        `/v1/projects/${projectId}/sessions`,
        { title: "New session" },
      );
      return r;
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["sessions", projectId] });
      setSessionId(r.session_id);
      setThreadId(r.thread_id);
    },
  });

  useEffect(() => {
    if (sessionId || !sessions.data) return;
    if (sessions.data.length === 0) {
      if (!createSession.isPending) createSession.mutate();
    } else {
      // Pick the most-recent.
      const first = sessions.data[0];
      setSessionId(first.id);
      void fetchThreadForSession(first.id, projectId).then((t) => {
        if (t) setThreadId(t.id);
      });
    }
  }, [sessions.data, sessionId, projectId]);

  const messages = useQuery<Message[]>({
    queryKey: ["messages", threadId],
    queryFn: () =>
      api.get<Message[]>(`/v1/projects/${projectId}/threads/${threadId}/messages`),
    enabled: !!threadId,
    refetchInterval: (q) => {
      const data = q.state.data;
      if (!Array.isArray(data)) return false;
      const inProgress = data.some((m: Message) => m.status === "streaming");
      return inProgress ? 1_000 : false;
    },
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.data]);

  const sendMessage = useMutation({
    mutationFn: async () => {
      if (!threadId) throw new Error("no thread");
      return api.post(
        `/v1/projects/${projectId}/threads/${threadId}/messages`,
        { body_md: composer },
      );
    },
    onSuccess: () => {
      setComposer("");
      qc.invalidateQueries({ queryKey: ["messages", threadId] });
    },
  });

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto p-6">
        {!threadId && (
          <div className="text-sm text-slate-500">Setting up a new session…</div>
        )}
        {messages.data?.map((m) => (
          <MessageBubble key={m.id} m={m} />
        ))}
        <div ref={bottomRef} />
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (composer.trim()) sendMessage.mutate();
        }}
        className="border-t border-slate-200 bg-white p-4"
      >
        <div className="flex gap-2">
          <textarea
            value={composer}
            onChange={(e) => setComposer(e.target.value)}
            placeholder={threadId ? "Ask about the wiki…" : "Setting up…"}
            disabled={!threadId || sendMessage.isPending}
            rows={2}
            className="flex-1 resize-none rounded-md border border-slate-300 px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={!threadId || !composer.trim() || sendMessage.isPending}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {sendMessage.isPending ? "Sending…" : "Send"}
          </button>
        </div>
        {sendMessage.isError && (
          <p className="mt-2 text-xs text-red-600">
            {(sendMessage.error as ApiError).message}
          </p>
        )}
      </form>
    </div>
  );
}

function MessageBubble({ m }: { m: Message }) {
  const isUser = m.role === "user";
  const tone = isUser
    ? "ml-12 border-slate-200 bg-slate-50"
    : "mr-12 border-slate-200 bg-white";
  return (
    <div className={`mb-4 rounded-lg border p-4 shadow-sm ${tone}`}>
      <div className="mb-2 flex items-center justify-between text-[11px] uppercase tracking-wider text-slate-400">
        <span>{m.role}</span>
        <span className="flex items-center gap-2">
          {m.status !== "complete" && (
            <span className="inline-flex items-center rounded bg-amber-100 px-1.5 py-0.5 text-amber-900">
              {m.status}
            </span>
          )}
          {m.role === "assistant" && m.status === "complete" && (
            <>
              {m.retrieval_jsonb?.coverage_judgment && (
                <span className="text-slate-500">
                  coverage: {m.retrieval_jsonb.coverage_judgment}
                </span>
              )}
              {m.latency_ms !== null && <span>{(m.latency_ms / 1000).toFixed(1)}s</span>}
              <span>${Number(m.cost_usd).toFixed(4)}</span>
            </>
          )}
        </span>
      </div>
      {m.body_md ? (
        <WikiBodyMarkdown body={m.body_md} />
      ) : m.status === "streaming" ? (
        <p className="text-sm italic text-slate-500">Thinking…</p>
      ) : null}
      {m.role === "assistant" && (m.retrieval_jsonb?.selected_pages?.length ?? 0) > 0 && (
        <div className="mt-3 border-t border-slate-100 pt-2 text-[11px] text-slate-500">
          Cited wiki pages:{" "}
          {m.retrieval_jsonb.selected_pages?.map((p, i) => (
            <span key={p.page_id}>
              {i > 0 ? ", " : ""}
              {p.title}
            </span>
          ))}
          {(m.retrieval_jsonb.descent_chunks?.length ?? 0) > 0 && (
            <span> · descent into {m.retrieval_jsonb.descent_chunks?.length} chunks</span>
          )}
        </div>
      )}
    </div>
  );
}

async function fetchThreadForSession(
  sessionId: string,
  projectId: string,
): Promise<ThreadInfo | null> {
  const token = await getAccessToken();
  const baseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
  const resp = await fetch(
    `${baseUrl}/v1/projects/${projectId}/sessions/${sessionId}/threads`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  );
  if (!resp.ok) return null;
  const ts = (await resp.json()) as ThreadInfo[];
  return ts.length > 0 ? ts[0] : null;
}
