import { useState } from "react";

import { HypothesisMatrix, type AchMatrix } from "./HypothesisMatrix";
import { CardShell, FeedbackButton, Pill, SurfaceHeader, type RendererProps } from "./_shared";

interface HypothesisItem {
  id: string;
  short_id: string;
  title: string;
  statement: string;
  confidence: string;
  status: string;
  last_evidence_change_at: string | null;
  created_at: string;
}

const TONE: Record<string, "emerald" | "sky" | "amber" | "red" | "slate" | "violet"> = {
  supported: "emerald",
  weakly_supported: "sky",
  under_investigation: "violet",
  contested: "amber",
  refuted: "red",
  initial: "slate",
};

/**
 * WP-4: the Hypotheses tab renders ONLY from the surface data model
 * (`{items, ach}`) streamed by the backend builder — no `useQuery`, no
 * polling. Mutations (create, feedback) go through `onAction` → the
 * ledger-audited action router; the resulting change comes back as an
 * `updateDataModel` delta over the same SSE stream.
 */
export function HypothesesSurface({ component, onAction }: RendererProps) {
  const items = (component.props.items as HypothesisItem[] | undefined) ?? [];
  const ach = (component.props.ach as AchMatrix | null | undefined) ?? null;
  const [showCreate, setShowCreate] = useState(false);

  return (
    <div className="flex h-full flex-col">
      <SurfaceHeader
        title="Hypotheses"
        subtitle={`${items.length} tracked`}
        actions={
          <button
            type="button"
            onClick={() => setShowCreate(true)}
            className="rounded-md bg-ink px-3 py-1 text-xs font-medium text-ink-inverse hover:bg-ink-soft"
            data-testid="new-hypothesis"
          >
            + New
          </button>
        }
      />
      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        <HypothesisMatrix ach={ach} />
        {items.length === 0 && (
          <div className="rounded-lg border border-dashed border-line-strong p-6 text-center text-sm text-ink-muted">
            No hypotheses yet. Click <strong>+ New</strong> to add your first.
          </div>
        )}
        {items.map((h) => (
          <HypothesisRow key={h.id} h={h} onAction={onAction} />
        ))}
      </div>
      {showCreate && (
        <NewHypothesisModal
          onClose={() => setShowCreate(false)}
          onCreate={(title, statement) => {
            onAction("create_hypothesis", { title, statement });
            setShowCreate(false);
          }}
        />
      )}
    </div>
  );
}

function HypothesisRow({
  h,
  onAction,
}: {
  h: HypothesisItem;
  onAction: RendererProps["onAction"];
}) {
  const updated = h.last_evidence_change_at ?? h.created_at;
  return (
    <CardShell
      title={`${h.short_id} · ${h.title}`}
      subtitle={
        <span className="flex items-center gap-2">
          <Pill tone={TONE[h.confidence] ?? "slate"}>{h.confidence.replace(/_/g, " ")}</Pill>
          <Pill tone="slate">{h.status}</Pill>
        </span>
      }
      actions={
        <FeedbackButton
          targetKind="hypothesis"
          targetId={h.id}
          surface="HypothesesSurface"
          onAction={onAction}
        />
      }
      footer={updated ? `updated ${new Date(updated).toLocaleString()}` : undefined}
    >
      <p className="text-sm text-ink-soft">{h.statement}</p>
    </CardShell>
  );
}

function NewHypothesisModal({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (title: string, statement: string) => void;
}) {
  const [title, setTitle] = useState("");
  const [statement, setStatement] = useState("");
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-ink/40 px-4">
      <div className="w-full max-w-md rounded-lg bg-surface p-5 shadow-xl">
        <h3 className="mb-3 text-base font-semibold">New hypothesis</h3>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (title && statement) onCreate(title, statement);
          }}
          className="space-y-3"
        >
          <label className="block">
            <span className="text-xs font-medium text-ink-soft">Title</span>
            <input
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="mt-1 w-full rounded-md border border-line-strong px-3 py-2 text-sm"
            />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-ink-soft">Statement</span>
            <textarea
              required
              value={statement}
              onChange={(e) => setStatement(e.target.value)}
              rows={4}
              className="mt-1 w-full rounded-md border border-line-strong px-3 py-2 text-sm"
              placeholder="A precise, falsifiable claim. e.g. 'CoT prompting improves GSM8K accuracy by >5 pts on models ≥7B params.'"
            />
          </label>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-line-strong px-3 py-1.5 text-xs hover:border-line-strong"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!title || !statement}
              className="rounded-md bg-ink px-3 py-1.5 text-xs font-medium text-ink-inverse hover:bg-ink-soft disabled:opacity-50"
            >
              Create
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
