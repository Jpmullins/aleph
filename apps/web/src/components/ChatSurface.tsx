import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { WikiBodyMarkdown } from "@/components/WikiBodyMarkdown";
import { api, type ApiError } from "@/lib/api";

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

interface Props {
  projectId: string;
  threadId: string | null;
  emptyHint?: string;
}

export function ChatSurface({ projectId, threadId, emptyHint }: Props) {
  const qc = useQueryClient();
  const [composer, setComposer] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

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

  useEffect(() => {
    if (threadId) inputRef.current?.focus();
  }, [threadId]);

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
      qc.invalidateQueries({ queryKey: ["agent-runs", projectId] });
    },
  });

  const trySend = () => {
    if (composer.trim() && threadId && !sendMessage.isPending) sendMessage.mutate();
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto p-6">
        {!threadId && (
          <div className="rounded-lg border border-dashed border-slate-300 p-8 text-center text-sm text-slate-500">
            {emptyHint ?? "Select or create a session in the left panel."}
          </div>
        )}
        {threadId && messages.data?.length === 0 && (
          <div className="rounded-lg border border-dashed border-slate-300 p-8 text-center text-sm text-slate-500">
            Ask anything about the project wiki. Use <kbd className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[10px] font-mono">Enter</kbd> to send, <kbd className="rounded border border-slate-300 bg-white px-1.5 py-0.5 text-[10px] font-mono">Shift+Enter</kbd> for a newline.
          </div>
        )}
        {messages.data?.map((m) => (
          <MessageBubble key={m.id} m={m} />
        ))}
        <div ref={bottomRef} />
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          trySend();
        }}
        className="border-t border-slate-200 bg-white p-4"
      >
        <div className="flex gap-2">
          <textarea
            ref={inputRef}
            value={composer}
            onChange={(e) => setComposer(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                trySend();
              }
            }}
            placeholder={threadId ? "Ask about the wiki…  (Enter to send, Shift+Enter for newline)" : "Create a session to start chatting"}
            disabled={!threadId || sendMessage.isPending}
            rows={2}
            className="flex-1 resize-none rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-500"
            data-testid="chat-composer"
          />
          <button
            type="submit"
            disabled={!threadId || !composer.trim() || sendMessage.isPending}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
            data-testid="chat-send"
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
    <div className={`mb-4 rounded-lg border p-4 shadow-sm ${tone}`} data-testid={`message-${m.role}`}>
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
