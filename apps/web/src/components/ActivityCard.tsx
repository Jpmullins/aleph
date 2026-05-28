import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";

interface AgentRunOut {
  id: string;
  agent_kind: string;
  status: "pending" | "running" | "succeeded" | "failed" | "cancelled";
  started_at: string | null;
  completed_at: string | null;
  correlation_id: string;
  error_text: string | null;
  created_at: string;
}

interface PhaseEvent {
  id: string;
  agent_run_id: string;
  agent_kind: string;
  event_kind: "phase_started" | "phase_completed" | "phase_failed";
  phase: string;
  duration_ms: number | null;
  payload: Record<string, unknown>;
  timestamp: string;
}

interface PhaseState {
  name: string;
  status: "running" | "completed" | "failed";
  startedAt: string;
  durationMs: number | null;
  error?: string;
}

interface Props {
  projectId: string;
}

const KIND_LABELS: Record<string, string> = {
  normalizer: "Normalizing source",
  normalize: "Normalizing source",
  chunk_embed: "Chunking + embedding",
  wiki: "Compiling wiki",
  wiki_ingest: "Compiling wiki",
  assistant: "Assistant turn",
  assistant_turn: "Assistant turn",
  aiq_deep: "Deep research",
  aiq_shallow: "Shallow research",
  mechanical_review: "Mechanical review",
  mechanical_reviewer: "Mechanical review",
  editorial_review: "Editorial review",
  editorial_reviewer: "Editorial review",
  builder: "Building artifact",
  smoke_llm: "Smoke test",
};

const PHASE_DOT: Record<PhaseState["status"], string> = {
  running: "bg-blue-500 animate-pulse",
  completed: "bg-emerald-500",
  failed: "bg-red-500",
};

export function ActivityCard({ projectId }: Props) {
  const [expanded, setExpanded] = useState(true);
  const [phasesByRun, setPhasesByRun] = useState<Map<string, PhaseState[]>>(new Map());
  const sinceRef = useRef<string>(new Date().toISOString());

  // 1) Top-level runs — we still poll the agent-runs endpoint for the run rollup.
  const runs = useQuery<AgentRunOut[]>({
    queryKey: ["agent-runs", projectId],
    queryFn: () => api.get<AgentRunOut[]>(`/v1/projects/${projectId}/agent-runs?limit=8`),
    refetchInterval: (q) => {
      const data = q.state.data;
      if (!Array.isArray(data)) return 3_000;
      const live = data.some((r) => r.status === "pending" || r.status === "running");
      return live ? 1_500 : 6_000;
    },
  });

  // 2) Subscribe to the phase-event SSE stream.
  useEffect(() => {
    const baseUrl =
      (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
    const url = new URL(
      `${baseUrl}/v1/projects/${projectId}/agent-events/stream`,
    );
    url.searchParams.set("since", sinceRef.current);
    const es = new EventSource(url.toString(), { withCredentials: false });

    es.addEventListener("phase", (ev) => {
      try {
        const event = JSON.parse((ev as MessageEvent).data) as PhaseEvent;
        sinceRef.current = event.timestamp;
        setPhasesByRun((prev) => {
          const next = new Map(prev);
          const list = [...(next.get(event.agent_run_id) ?? [])];
          const idx = list.findIndex((p) => p.name === event.phase);
          if (event.event_kind === "phase_started") {
            if (idx === -1) {
              list.push({
                name: event.phase,
                status: "running",
                startedAt: event.timestamp,
                durationMs: null,
              });
            }
          } else if (event.event_kind === "phase_completed") {
            if (idx >= 0) {
              list[idx] = {
                ...list[idx],
                status: "completed",
                durationMs: event.duration_ms,
              };
            } else {
              list.push({
                name: event.phase,
                status: "completed",
                startedAt: event.timestamp,
                durationMs: event.duration_ms,
              });
            }
          } else if (event.event_kind === "phase_failed") {
            const err = String(event.payload?.error ?? "failed");
            if (idx >= 0) {
              list[idx] = { ...list[idx], status: "failed", error: err };
            } else {
              list.push({
                name: event.phase,
                status: "failed",
                startedAt: event.timestamp,
                durationMs: null,
                error: err,
              });
            }
          }
          next.set(event.agent_run_id, list);
          return next;
        });
      } catch {
        /* ignore malformed */
      }
    });

    es.onerror = () => {
      // EventSource auto-reconnects with backoff; nothing to do here.
    };

    return () => es.close();
  }, [projectId]);

  const list = runs.data ?? [];
  const running = list.filter((r) => r.status === "running" || r.status === "pending");
  const recent = list
    .filter((r) => r.status !== "running" && r.status !== "pending")
    .slice(0, 2);

  const summary = useMemo(() => {
    if (running.length === 0) return "idle";
    if (running.length === 1) {
      const r = running[0];
      const phases = phasesByRun.get(r.id) ?? [];
      const total = phases.length || 0;
      const done = phases.filter((p) => p.status === "completed").length;
      const label = KIND_LABELS[r.agent_kind] ?? r.agent_kind;
      if (total) return `${label} · ${done} / ${total} phases`;
      return `${label} · starting`;
    }
    return `${running.length} agents running`;
  }, [running, phasesByRun]);

  return (
    <div className="border-b border-slate-200 bg-white">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-2 text-left text-xs hover:bg-slate-50"
        data-testid="activity-card-toggle"
      >
        <div className="flex items-center gap-2">
          <span className="font-medium uppercase tracking-wider text-slate-500">Activity</span>
          <span className="text-slate-700">{summary}</span>
        </div>
        <span className="text-slate-400">{expanded ? "▲" : "▼"}</span>
      </button>
      {expanded && (
        <div className="space-y-2 px-4 pb-3">
          {running.length === 0 && recent.length === 0 && (
            <p className="py-2 text-xs text-slate-400">No recent activity.</p>
          )}
          {running.map((r) => (
            <RunRow key={r.id} run={r} phases={phasesByRun.get(r.id) ?? []} live />
          ))}
          {recent.map((r) => (
            <RunRow key={r.id} run={r} phases={phasesByRun.get(r.id) ?? []} />
          ))}
        </div>
      )}
    </div>
  );
}

function RunRow({
  run,
  phases,
  live = false,
}: {
  run: AgentRunOut;
  phases: PhaseState[];
  live?: boolean;
}) {
  const label = KIND_LABELS[run.agent_kind] ?? run.agent_kind;
  const elapsed = run.started_at
    ? Math.max(
        0,
        Math.round(
          ((run.completed_at ? new Date(run.completed_at).getTime() : Date.now()) -
            new Date(run.started_at).getTime()) /
            1000,
        ),
      )
    : null;

  return (
    <div className={`rounded-md border border-slate-200 px-3 py-2 ${live ? "" : "opacity-70"}`}>
      <div className="flex items-center justify-between text-xs">
        <span className="font-medium text-slate-800">{label}</span>
        <span className="text-slate-500">
          <StatusBadge status={run.status} /> {elapsed !== null ? `${elapsed}s` : "—"}
        </span>
      </div>
      {phases.length > 0 && (
        <ul className="mt-1.5 space-y-0.5">
          {phases.map((p) => (
            <li key={p.name} className="flex items-center gap-2 text-[11px]">
              <span className={`inline-block h-2 w-2 rounded-full ${PHASE_DOT[p.status]}`} />
              <span
                className={
                  p.status === "failed"
                    ? "text-red-700"
                    : p.status === "completed"
                      ? "text-slate-600"
                      : "text-slate-900"
                }
              >
                {p.name}
              </span>
              {p.durationMs !== null && p.status === "completed" && (
                <span className="text-slate-400">· {Math.round(p.durationMs / 100) / 10}s</span>
              )}
              {p.error && <span className="ml-2 text-red-700">{p.error}</span>}
            </li>
          ))}
        </ul>
      )}
      {run.error_text && <p className="mt-1 text-[11px] text-red-700">{run.error_text}</p>}
    </div>
  );
}

function StatusBadge({ status }: { status: AgentRunOut["status"] }) {
  const cls: Record<string, string> = {
    pending: "bg-slate-100 text-slate-600",
    running: "bg-blue-100 text-blue-800",
    succeeded: "bg-emerald-100 text-emerald-800",
    failed: "bg-red-100 text-red-800",
    cancelled: "bg-slate-100 text-slate-500",
  };
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${cls[status] ?? cls.pending}`}
    >
      {status}
    </span>
  );
}
