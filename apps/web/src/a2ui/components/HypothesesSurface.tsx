import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useSurface } from "../surface-context";
import { api } from "@/lib/api";
import { HypothesisMatrix } from "./HypothesisMatrix";
import { CardShell, FeedbackButton, Pill, SurfaceHeader, type RendererProps } from "./_shared";

interface HypothesisOut {
  id: string;
  short_id: string;
  title: string;
  statement: string;
  confidence: string;
  status: string;
  current_version_id: string | null;
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

export function HypothesesSurface(_: RendererProps) {
  const { projectId } = useSurface();
  const qc = useQueryClient();
  const [showCreate, setShowCreate] = useState(false);

  const hypotheses = useQuery<HypothesisOut[]>({
    queryKey: ["hypotheses", projectId],
    queryFn: () => api.get<HypothesisOut[]>(`/v1/projects/${projectId}/hypotheses`),
  });

  return (
    <div className="flex h-full flex-col">
      <SurfaceHeader
        title="Hypotheses"
        subtitle={hypotheses.data ? `${hypotheses.data.length} tracked` : undefined}
        actions={
          <button
            type="button"
            onClick={() => setShowCreate(true)}
            className="rounded-md bg-slate-900 px-3 py-1 text-xs font-medium text-white hover:bg-slate-700"
            data-testid="new-hypothesis"
          >
            + New
          </button>
        }
      />
      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        <HypothesisMatrix projectId={projectId} />
        {hypotheses.isPending && <p className="text-sm text-slate-400">Loading…</p>}
        {hypotheses.isSuccess && hypotheses.data.length === 0 && (
          <div className="rounded-lg border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500">
            No hypotheses yet. Click <strong>+ New</strong> to add your first.
          </div>
        )}
        {hypotheses.data?.map((h) => (
          <HypothesisRow key={h.id} h={h} projectId={projectId} />
        ))}
      </div>
      {showCreate && (
        <NewHypothesisModal
          projectId={projectId}
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            qc.invalidateQueries({ queryKey: ["hypotheses", projectId] });
          }}
        />
      )}
    </div>
  );
}

function HypothesisRow({ h, projectId }: { h: HypothesisOut; projectId: string }) {
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
          projectId={projectId}
          targetKind="hypothesis"
          targetId={h.id}
          surface="HypothesesSurface"
        />
      }
      footer={`updated ${new Date(updated).toLocaleString()}`}
    >
      <p className="text-sm text-slate-700">{h.statement}</p>
    </CardShell>
  );
}

function NewHypothesisModal({
  projectId,
  onClose,
  onCreated,
}: {
  projectId: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [title, setTitle] = useState("");
  const [statement, setStatement] = useState("");
  const create = useMutation({
    mutationFn: async () =>
      api.post(`/v1/projects/${projectId}/hypotheses`, {
        title,
        statement,
        confidence: "initial",
      }),
    onSuccess: onCreated,
  });
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/40 px-4">
      <div className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl">
        <h3 className="mb-3 text-base font-semibold">New hypothesis</h3>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (title && statement) create.mutate();
          }}
          className="space-y-3"
        >
          <label className="block">
            <span className="text-xs font-medium text-slate-700">Title</span>
            <input
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </label>
          <label className="block">
            <span className="text-xs font-medium text-slate-700">Statement</span>
            <textarea
              required
              value={statement}
              onChange={(e) => setStatement(e.target.value)}
              rows={4}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              placeholder="A precise, falsifiable claim. e.g. 'CoT prompting improves GSM8K accuracy by >5 pts on models ≥7B params.'"
            />
          </label>
          {create.isError && (
            <p className="text-xs text-red-600">Failed to create hypothesis.</p>
          )}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-xs hover:border-slate-500"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={create.isPending || !title || !statement}
              className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-50"
            >
              {create.isPending ? "Creating…" : "Create"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
