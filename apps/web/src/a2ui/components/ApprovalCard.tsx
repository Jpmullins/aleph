import { useState } from "react";

import { CardShell, Pill, type RendererProps } from "./_shared";

export function ApprovalCard({ component, onAction }: RendererProps) {
  const p = component.props as {
    target_id: string;
    target_kind: string;
    title: string;
    summary: string;
    severity?: "info" | "low" | "medium" | "high";
    evidence_refs?: Array<{ kind: string; id: string; label?: string }>;
  };
  const [reason, setReason] = useState("");
  const [showReject, setShowReject] = useState(false);
  const tone = p.severity === "high" ? "red" : p.severity === "medium" ? "amber" : "slate";
  return (
    <CardShell
      title={p.title}
      subtitle={
        <span className="flex items-center gap-2">
          <Pill tone={tone}>{p.severity ?? "info"}</Pill>
          <span>{p.target_kind}</span>
        </span>
      }
    >
      <p className="mb-2 text-sm text-slate-700">{p.summary}</p>
      {p.evidence_refs && p.evidence_refs.length > 0 && (
        <ul className="mb-3 list-disc pl-5 text-xs text-slate-500">
          {p.evidence_refs.map((e) => (
            <li key={e.id}>
              {e.kind}: {e.label ?? e.id}
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() =>
            onAction("approve", { target_id: p.target_id, target_kind: p.target_kind })
          }
          className="rounded-md bg-emerald-600 px-3 py-1 text-xs font-medium text-white hover:bg-emerald-700"
        >
          Approve
        </button>
        <button
          type="button"
          onClick={() => setShowReject(true)}
          className="rounded-md border border-slate-300 px-3 py-1 text-xs font-medium hover:border-slate-500"
        >
          Reject
        </button>
        <button
          type="button"
          onClick={() =>
            onAction("open", { target_id: p.target_id, target_kind: p.target_kind })
          }
          className="ml-auto text-xs text-slate-500 hover:text-slate-900"
        >
          Open →
        </button>
      </div>
      {showReject && (
        <div className="mt-3 rounded border border-slate-200 bg-slate-50 p-2">
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={2}
            placeholder="Reason for rejection…"
            className="w-full rounded border border-slate-300 px-2 py-1 text-xs"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                setShowReject(false);
                setReason("");
              }}
              className="rounded border border-slate-300 px-2 py-1 text-xs hover:border-slate-500"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={!reason.trim()}
              onClick={() => {
                onAction("reject", {
                  target_id: p.target_id,
                  target_kind: p.target_kind,
                  reason,
                });
                setShowReject(false);
                setReason("");
              }}
              className="rounded bg-red-600 px-2 py-1 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50"
            >
              Confirm reject
            </button>
          </div>
        </div>
      )}
    </CardShell>
  );
}
