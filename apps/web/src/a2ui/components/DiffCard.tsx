import { useMemo } from "react";

import { CardShell, type RendererProps } from "./_shared";

/**
 * WP-4e: DiffCard renders a real line-level revision diff from BOUND props —
 * the builder (or ApprovalCard.diff_card_id producer) supplies `from_body_md`
 * and `to_body_md`. It never self-fetches revisions. Absent bodies → it renders
 * the revision-id summary only.
 */
type DiffLine = { kind: "same" | "add" | "del"; text: string };

/** Minimal LCS line diff — deterministic, no dependencies. */
function lineDiff(from: string, to: string): DiffLine[] {
  const a = from.split("\n");
  const b = to.split("\n");
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ kind: "del", text: a[i] });
      i++;
    } else {
      out.push({ kind: "add", text: b[j] });
      j++;
    }
  }
  while (i < n) out.push({ kind: "del", text: a[i++] });
  while (j < m) out.push({ kind: "add", text: b[j++] });
  return out;
}

const LINE_STYLE: Record<DiffLine["kind"], string> = {
  same: "text-ink-muted",
  add: "bg-badge-completed-bg text-badge-completed-fg",
  del: "bg-badge-failed-bg text-badge-failed-fg line-through decoration-badge-failed-fg",
};
const LINE_PREFIX: Record<DiffLine["kind"], string> = { same: " ", add: "+", del: "−" };

export function DiffCard({ component }: RendererProps) {
  const p = component.props as {
    from_revision_id: string;
    to_revision_id: string;
    page_id: string;
    from_body_md?: string | null;
    to_body_md?: string | null;
  };
  const diff = useMemo(() => {
    if (p.from_body_md == null || p.to_body_md == null) return null;
    return lineDiff(p.from_body_md, p.to_body_md);
  }, [p.from_body_md, p.to_body_md]);

  return (
    <CardShell
      title="Wiki revision diff"
      subtitle={
        <span className="text-xs">
          {p.from_revision_id.slice(0, 8)} → {p.to_revision_id.slice(0, 8)}
        </span>
      }
    >
      {diff == null ? (
        <p className="text-xs text-ink-muted">
          Revisions {p.from_revision_id.slice(0, 8)} → {p.to_revision_id.slice(0, 8)}.
        </p>
      ) : (
        <pre
          data-testid="diff-card-body"
          className="max-h-72 overflow-auto border border-line bg-sunken p-2 text-[11px] leading-snug"
        >
          {diff.map((l, idx) => (
            <div key={idx} className={`whitespace-pre-wrap ${LINE_STYLE[l.kind]}`}>
              <span className="mr-1 select-none opacity-60">{LINE_PREFIX[l.kind]}</span>
              {l.text || " "}
            </div>
          ))}
        </pre>
      )}
    </CardShell>
  );
}
