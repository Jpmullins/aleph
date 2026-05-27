import { useQuery } from "@tanstack/react-query";

import { api, type CostRollupOut } from "@/lib/api";

interface Props {
  projectId: string;
}

export function CostBanner({ projectId }: Props) {
  const cost = useQuery<CostRollupOut>({
    queryKey: ["cost", projectId],
    queryFn: () => api.get<CostRollupOut>(`/v1/projects/${projectId}/cost`),
    refetchInterval: 30_000,
  });
  if (!cost.data) {
    return (
      <div className="border-b border-slate-200 bg-white px-6 py-2 text-xs text-slate-500">
        Loading cost…
      </div>
    );
  }
  const cap = Number(cost.data.cap_usd);
  const spent = Number(cost.data.spent_usd);
  const pct = cap > 0 ? Math.min(100, (spent / cap) * 100) : 0;
  const soft = Number(cost.data.soft_pct);
  const hard = Number(cost.data.hard_pct);
  const tone = pct >= hard ? "bg-red-50 text-red-800" : pct >= soft ? "bg-amber-50 text-amber-800" : "bg-white text-slate-700";
  return (
    <div className={`border-b border-slate-200 px-6 py-2 text-xs ${tone}`}>
      <span className="font-medium">Cost</span> · ${spent.toFixed(4)} / ${cap.toFixed(2)} ({pct.toFixed(1)}%)
      {pct >= hard && <span className="ml-2 font-semibold uppercase tracking-wider">hard cap reached</span>}
      {pct >= soft && pct < hard && <span className="ml-2 font-semibold uppercase tracking-wider">soft cap</span>}
    </div>
  );
}
