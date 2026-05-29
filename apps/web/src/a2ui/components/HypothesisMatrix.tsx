/**
 * ACH (Analysis of Competing Hypotheses) matrix — Heuer's method.
 *
 * Columns = hypotheses, rows = evidence items, cells = each hypothesis's
 * stance toward that evidence (supports / contradicts / contextualizes). The
 * column with the fewest *disconfirming* (contradicting) items is highlighted
 * as the leading hypothesis. Reads `GET /hypotheses/ach`.
 */
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

interface AchHypothesis {
  id: string;
  short_id: string;
  title: string;
  confidence: string;
  disconfirming_count: number;
}
interface AchTarget {
  target_id: string;
  evidence_kind: string;
  label: string;
}
interface AchCell {
  hypothesis_id: string;
  target_id: string;
  stance: string;
  weight: number;
  note: string;
}
interface AchMatrix {
  hypotheses: AchHypothesis[];
  targets: AchTarget[];
  cells: AchCell[];
  fewest_disconfirming_id: string | null;
}

const STANCE: Record<string, { glyph: string; cls: string; label: string }> = {
  supports: { glyph: "+", cls: "bg-emerald-500/15 text-emerald-500", label: "supports" },
  contradicts: { glyph: "−", cls: "bg-red-500/15 text-red-500", label: "contradicts" },
  contextualizes: { glyph: "○", cls: "bg-amber-500/15 text-amber-500", label: "contextualizes" },
};

export function HypothesisMatrix({ projectId }: { projectId: string }) {
  const q = useQuery<AchMatrix>({
    queryKey: ["ach-matrix", projectId],
    queryFn: () => api.get<AchMatrix>(`/v1/projects/${projectId}/hypotheses/ach`),
    refetchInterval: 15_000,
  });

  if (q.isPending || !q.data) return null;
  const { hypotheses, targets, cells, fewest_disconfirming_id } = q.data;
  if (hypotheses.length === 0 || targets.length === 0) return null;

  const cellFor = (hid: string, tid: string) =>
    cells.find((c) => c.hypothesis_id === hid && c.target_id === tid);

  return (
    <div className="overflow-x-auto rounded-lg border border-[var(--border-muted,#e2e8f0)] bg-[var(--surface-raised,#fff)]">
      <div className="flex items-center justify-between px-3 py-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--text-secondary,#475569)]">
          ACH matrix
        </span>
        <span className="text-[10px] text-[var(--text-muted,#94a3b8)]">
          fewest disconfirming = leading
        </span>
      </div>
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr>
            <th className="sticky left-0 z-10 bg-[var(--surface-raised,#fff)] px-2 py-1 text-left font-medium text-[var(--text-muted,#94a3b8)]">
              Evidence
            </th>
            {hypotheses.map((h) => {
              const leading = h.id === fewest_disconfirming_id;
              return (
                <th
                  key={h.id}
                  title={h.title}
                  className={
                    "px-2 py-1 text-center font-semibold " +
                    (leading
                      ? "bg-[var(--accent-muted,rgba(249,115,22,0.1))] text-[var(--accent,#f97316)]"
                      : "text-[var(--text-secondary,#475569)]")
                  }
                >
                  {h.short_id}
                  {leading && <span className="ml-1" title="fewest disconfirming">★</span>}
                  <div className="font-normal text-[9px] text-[var(--text-muted,#94a3b8)]">
                    {h.disconfirming_count}✗
                  </div>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {targets.map((t) => (
            <tr key={t.target_id} className="border-t border-[var(--border-muted,#e2e8f0)]">
              <td
                className="sticky left-0 z-10 max-w-[16rem] truncate bg-[var(--surface-raised,#fff)] px-2 py-1 text-[var(--text-primary,#0f172a)]"
                title={t.label}
              >
                {t.label}
              </td>
              {hypotheses.map((h) => {
                const c = cellFor(h.id, t.target_id);
                const s = c ? STANCE[c.stance] : undefined;
                return (
                  <td key={h.id} className="px-1 py-1 text-center">
                    {s && (
                      <span
                        title={`${s.label}${c && c.weight !== 1 ? ` ×${c.weight}` : ""}${c?.note ? ` — ${c.note}` : ""}`}
                        className={"inline-grid h-5 w-5 place-items-center rounded font-bold " + s.cls}
                      >
                        {s.glyph}
                      </span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
