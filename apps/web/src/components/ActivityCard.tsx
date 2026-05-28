import { useQuery } from "@tanstack/react-query";

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
  aiq_deep: "Deep research (AIQ)",
  aiq_shallow: "Shallow research (AIQ)",
  mechanical_review: "Mechanical review",
  mechanical_reviewer: "Mechanical review",
  editorial_review: "Editorial review",
  editorial_reviewer: "Editorial review",
  builder: "Building artifact",
  smoke_llm: "Smoke test",
};

const STATUS_TONES: Record<string, string> = {
  pending: "bg-slate-100 text-slate-600",
  running: "bg-blue-100 text-blue-800",
  succeeded: "bg-emerald-100 text-emerald-800",
  failed: "bg-red-100 text-red-800",
  cancelled: "bg-slate-100 text-slate-500",
};

export function ActivityCard({ projectId }: Props) {
  const runs = useQuery<AgentRunOut[]>({
    queryKey: ["agent-runs", projectId],
    queryFn: () => api.get<AgentRunOut[]>(`/v1/projects/${projectId}/agent-runs?limit=10`),
    refetchInterval: (q) => {
      const data = q.state.data;
      if (!Array.isArray(data)) return 3_000;
      const live = data.some((r) => r.status === "pending" || r.status === "running");
      return live ? 1_500 : 6_000;
    },
  });

  const list = runs.data ?? [];
  const running = list.filter((r) => r.status === "running" || r.status === "pending");
  const recent = list.filter((r) => r.status !== "running" && r.status !== "pending").slice(0, 3);

  return (
    <div className="border-t border-slate-200 bg-white px-4 py-2 text-xs">
      <div className="mb-1 flex items-center justify-between">
        <span className="font-medium text-slate-700">Activity</span>
        <span className="text-slate-400">
          {running.length > 0 ? `${running.length} running` : "idle"}
        </span>
      </div>
      <ul className="space-y-1">
        {running.map((r) => (
          <RunRow key={r.id} run={r} />
        ))}
        {running.length === 0 && recent.length === 0 && (
          <li className="text-slate-400">No recent activity.</li>
        )}
        {recent.map((r) => (
          <RunRow key={r.id} run={r} dim />
        ))}
      </ul>
    </div>
  );
}

function RunRow({ run, dim = false }: { run: AgentRunOut; dim?: boolean }) {
  const label = KIND_LABELS[run.agent_kind] ?? run.agent_kind;
  const tone = STATUS_TONES[run.status] ?? "bg-slate-100 text-slate-600";
  const elapsed = run.started_at
    ? Math.max(0, Math.round(
        ((run.completed_at ? new Date(run.completed_at).getTime() : Date.now()) -
          new Date(run.started_at).getTime()) /
          1000,
      ))
    : null;
  return (
    <li className={`flex items-center justify-between ${dim ? "opacity-70" : ""}`}>
      <div className="flex min-w-0 items-center gap-2">
        <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${tone}`}>
          {run.status}
        </span>
        <span className="truncate text-slate-700">{label}</span>
        {run.status === "running" && (
          <span className="inline-block h-1 w-1 animate-pulse rounded-full bg-blue-500" />
        )}
      </div>
      <span className="text-slate-400" title={run.correlation_id}>
        {elapsed !== null ? `${elapsed}s` : "—"}
      </span>
    </li>
  );
}
