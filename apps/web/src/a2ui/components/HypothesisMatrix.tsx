/**
 * ACH (Analysis of Competing Hypotheses) matrix — Heuer's method.
 *
 * Columns = hypotheses, rows = evidence items, cells = each hypothesis's
 * stance toward that evidence (supports / contradicts / contextualizes). The
 * column with the fewest *disconfirming* (contradicting) items is highlighted
 * as the leading hypothesis.
 *
 * WP-4: bound-props only — the matrix is supplied by the Hypotheses surface's
 * data model (`ach`), never self-fetched.
 */
export interface AchHypothesis {
  id: string;
  short_id: string;
  title: string;
  confidence: string;
  disconfirming_count: number;
}
export interface AchTarget {
  target_id: string;
  evidence_kind: string;
  label: string;
}
export interface AchCell {
  hypothesis_id: string;
  target_id: string;
  stance: string;
  weight: number;
  note: string;
}
export interface AchMatrix {
  hypotheses: AchHypothesis[];
  targets: AchTarget[];
  cells: AchCell[];
  fewest_disconfirming_id: string | null;
}

const STANCE: Record<string, { glyph: string; cls: string; label: string }> = {
  supports: { glyph: "+", cls: "bg-good/15 text-good", label: "supports" },
  contradicts: { glyph: "−", cls: "bg-bad/15 text-bad", label: "contradicts" },
  contextualizes: { glyph: "○", cls: "bg-badge-warning-fg/15 text-badge-warning-fg", label: "contextualizes" },
};

export function HypothesisMatrix({ ach }: { ach: AchMatrix | null }) {
  if (!ach) return null;
  const { hypotheses, targets, cells, fewest_disconfirming_id } = ach;
  if (hypotheses.length === 0 || targets.length === 0) return null;

  const cellFor = (hid: string, tid: string) =>
    cells.find((c) => c.hypothesis_id === hid && c.target_id === tid);

  return (
    <div className="overflow-x-auto border border-line bg-surface">
      <div className="flex items-center justify-between px-3 py-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-ink-soft">
          ACH matrix
        </span>
        <span className="text-[10px] text-ink-muted">
          fewest disconfirming = leading
        </span>
      </div>
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr>
            <th className="sticky left-0 z-10 bg-surface px-2 py-1 text-left font-medium text-ink-muted">
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
                      ? "bg-accent-muted text-accent"
                      : "text-ink-soft")
                  }
                >
                  {h.short_id}
                  {leading && <span className="ml-1" title="fewest disconfirming">★</span>}
                  <div className="font-normal text-[9px] text-ink-muted">
                    {h.disconfirming_count}✗
                  </div>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {targets.map((t) => (
            <tr key={t.target_id} className="border-t border-line">
              <td
                className="sticky left-0 z-10 max-w-[12rem] truncate bg-surface px-2 py-1 text-ink"
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
                        className={"inline-grid h-5 w-5 place-items-center font-bold " + s.cls}
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
