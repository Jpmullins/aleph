import { useMutation } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import type { A2UIComponent } from "../catalog";
import { api } from "@/lib/api";

export interface RendererProps {
  component: A2UIComponent;
  onAction: (action: string, params: Record<string, unknown>) => void;
}

export function Pill({
  tone = "slate",
  children,
}: {
  tone?: "slate" | "amber" | "emerald" | "red" | "sky" | "violet";
  children: ReactNode;
}) {
  const cls: Record<string, string> = {
    amber: "bg-[var(--badge-warning-bg,#fef3c7)] text-[var(--badge-warning-fg,#854d0e)]",
    emerald: "bg-[var(--badge-completed-bg,#d1fae5)] text-[var(--badge-completed-fg,#065f46)]",
    red: "bg-[var(--badge-failed-bg,#fee2e2)] text-[var(--badge-failed-fg,#991b1b)]",
    sky: "bg-[var(--badge-running-bg,#dbeafe)] text-[var(--badge-running-fg,#1e3a8a)]",
    violet: "bg-violet-100 text-violet-900 dark:bg-violet-500/20 dark:text-violet-300",
    slate: "bg-[var(--badge-idle-bg,#f1f5f9)] text-[var(--badge-idle-fg,#475569)]",
  };
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${cls[tone] ?? cls.slate}`}
    >
      {children}
    </span>
  );
}

export function CardShell({
  title,
  subtitle,
  children,
  actions,
  footer,
}: {
  title?: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-[var(--border-muted,#e2e8f0)] bg-[var(--surface-raised,#fff)] p-3 shadow-sm">
      {(title || actions) && (
        <div className="mb-1 flex items-start justify-between gap-2">
          {title && (
            <div className="text-sm font-semibold text-[var(--text-primary,#0f172a)]">{title}</div>
          )}
          {actions && <div className="flex items-center gap-1">{actions}</div>}
        </div>
      )}
      {subtitle && <div className="mb-2 text-xs text-[var(--text-muted,#94a3b8)]">{subtitle}</div>}
      {children}
      {footer && (
        <div className="mt-2 border-t border-[var(--border-muted,#e2e8f0)] pt-2 text-[11px] text-[var(--text-muted,#94a3b8)]">
          {footer}
        </div>
      )}
    </div>
  );
}

export function SurfaceHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between border-b border-[var(--border-muted,#e2e8f0)] px-4 py-3">
      <div>
        <h3 className="text-sm font-semibold uppercase tracking-wider text-[var(--text-secondary,#475569)]">
          {title}
        </h3>
        {subtitle && <p className="mt-0.5 text-xs text-[var(--text-muted,#94a3b8)]">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export type FeedbackSignal =
  | "thumbs_up"
  | "thumbs_down"
  | "marked_wrong"
  | "misleading"
  | "false_positive"
  | "excellent"
  | "note_only";

export type FeedbackTargetKind =
  | "claim"
  | "source"
  | "chart"
  | "finding"
  | "hypothesis"
  | "assistant_message"
  | "wiki_page";

interface FeedbackButtonProps {
  projectId: string;
  targetKind: FeedbackTargetKind;
  targetId: string;
  surface?: string;
}

const FEEDBACK_OPTIONS: { signal: FeedbackSignal; label: string }[] = [
  { signal: "marked_wrong", label: "Mark wrong" },
  { signal: "misleading", label: "Misleading" },
  { signal: "false_positive", label: "False positive" },
  { signal: "thumbs_down", label: "Just thumbs down" },
];

export function FeedbackButton({ projectId, targetKind, targetId, surface }: FeedbackButtonProps) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [submitted, setSubmitted] = useState<FeedbackSignal | null>(null);

  const submit = useMutation({
    mutationFn: async (signal: FeedbackSignal) =>
      api.post(`/v1/projects/${projectId}/feedback`, {
        target_kind: targetKind,
        target_id: targetId,
        signal,
        note,
        context: surface ? { surface } : {},
      }),
    onSuccess: (_d, signal) => {
      setSubmitted(signal);
      setOpen(false);
      setNote("");
    },
  });

  if (submitted) {
    return (
      <span className="inline-flex items-center rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
        Thanks — flagged
      </span>
    );
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="rounded p-1 text-xs text-slate-400 hover:bg-slate-100 hover:text-slate-700"
        title="Flag a problem with this card"
        data-testid={`feedback-${targetKind}-${targetId}`}
      >
        👎
      </button>
      {open && (
        <div
          className="absolute right-0 z-10 mt-1 w-56 rounded-md border border-[var(--border-muted,#e2e8f0)] bg-[var(--surface-elevated,#fff)] p-2 text-xs shadow-lg"
          onClick={(e) => e.stopPropagation()}
        >
          <p className="mb-2 font-medium text-[var(--text-secondary,#475569)]">What's wrong?</p>
          <ul className="mb-2 space-y-1">
            {FEEDBACK_OPTIONS.map((opt) => (
              <li key={opt.signal}>
                <button
                  type="button"
                  onClick={() => submit.mutate(opt.signal)}
                  disabled={submit.isPending}
                  className="w-full rounded px-2 py-1 text-left text-slate-700 hover:bg-slate-100 disabled:opacity-50"
                >
                  {opt.label}
                </button>
              </li>
            ))}
          </ul>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Optional note (e.g. 'cited chunk doesn't say this')"
            rows={2}
            className="w-full resize-none rounded border border-slate-200 px-2 py-1 text-xs focus:border-slate-500 focus:outline-none"
          />
          <div className="mt-2 flex justify-end">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-[10px] text-slate-500 hover:text-slate-900"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
