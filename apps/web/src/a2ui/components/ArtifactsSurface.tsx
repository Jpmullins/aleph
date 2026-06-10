import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useSurface } from "../surface-context";
import { api } from "@/lib/api";
import { CardShell, Pill, SurfaceHeader, type RendererProps } from "./_shared";

interface ArtifactOut {
  id: string;
  short_id: string;
  title: string;
  artifact_kind: string;
  description: string;
  current_version_id: string | null;
  created_at: string;
}

const KIND_TONE: Record<string, "sky" | "emerald" | "amber" | "slate"> = {
  report_pdf: "sky",
  report_docx: "sky",
  report_markdown_bundle: "sky",
  source_pack: "emerald",
  deck_pdf: "amber",
};

export function ArtifactsSurface(_: RendererProps) {
  const { projectId } = useSurface();
  const qc = useQueryClient();
  const [showBuild, setShowBuild] = useState(false);

  const artifacts = useQuery<ArtifactOut[]>({
    queryKey: ["artifacts", projectId],
    queryFn: () => api.get<ArtifactOut[]>(`/v1/projects/${projectId}/artifacts`),
    refetchInterval: 5_000,
  });

  return (
    <div className="flex h-full flex-col">
      <SurfaceHeader
        title="Artifacts"
        subtitle={artifacts.data ? `${artifacts.data.length} built` : undefined}
        actions={
          <button
            type="button"
            onClick={() => setShowBuild(true)}
            className="rounded-md bg-slate-900 px-3 py-1 text-xs font-medium text-white hover:bg-slate-700"
            data-testid="build-artifact"
          >
            + Build
          </button>
        }
      />
      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        {artifacts.isPending && <p className="text-sm text-slate-400">Loading…</p>}
        {artifacts.isSuccess && artifacts.data.length === 0 && (
          <div className="rounded-lg border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500">
            No artifacts yet. Click <strong>+ Build</strong> to compose a report from the wiki.
          </div>
        )}
        {artifacts.data?.map((a) => (
          <ArtifactRow key={a.id} a={a} projectId={projectId} />
        ))}
      </div>
      {showBuild && (
        <BuildArtifactModal
          projectId={projectId}
          onClose={() => setShowBuild(false)}
          onBuilt={() => {
            setShowBuild(false);
            qc.invalidateQueries({ queryKey: ["artifacts", projectId] });
            qc.invalidateQueries({ queryKey: ["agent-runs", projectId] });
          }}
        />
      )}
    </div>
  );
}

function ArtifactRow({ a, projectId }: { a: ArtifactOut; projectId: string }) {
  const ready = !!a.current_version_id;
  return (
    <CardShell
      title={`${a.short_id} · ${a.title}`}
      subtitle={
        <span className="flex items-center gap-2">
          <Pill tone={KIND_TONE[a.artifact_kind] ?? "slate"}>
            {a.artifact_kind.replace(/_/g, " ")}
          </Pill>
          <Pill tone={ready ? "emerald" : "amber"}>{ready ? "ready" : "building"}</Pill>
        </span>
      }
      footer={`created ${new Date(a.created_at).toLocaleString()}`}
    >
      {a.description && <p className="text-xs text-slate-600">{a.description}</p>}
      {ready && (
        <a
          href={`/v1/projects/${projectId}/artifacts/${a.id}/versions/${a.current_version_id}/download`}
          download
          className="mt-1 inline-block text-xs font-medium text-slate-700 hover:text-slate-900"
        >
          ⬇ Download latest
        </a>
      )}
    </CardShell>
  );
}

function BuildArtifactModal({
  projectId,
  onClose,
  onBuilt,
}: {
  projectId: string;
  onClose: () => void;
  onBuilt: () => void;
}) {
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<
    "report_pdf" | "report_markdown_bundle" | "source_pack" | "deck_pdf"
  >("report_markdown_bundle");
  const [description, setDescription] = useState("");

  const build = useMutation({
    mutationFn: async () => {
      const out = await api.post<{ dispatched: boolean }>(
        `/v1/projects/${projectId}/artifacts/build`,
        {
          title,
          artifact_kind: kind,
          description,
          template_name: kind,
          csl_style: "apa-7",
        },
      );
      if (!out.dispatched) {
        throw new Error("build accepted but could not be dispatched to the worker queue");
      }
      return out;
    },
    onSuccess: onBuilt,
  });

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/40 px-4">
      <div className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl">
        <h3 className="mb-3 text-base font-semibold">Build artifact</h3>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (title) build.mutate();
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
            <span className="text-xs font-medium text-slate-700">Kind</span>
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value as typeof kind)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            >
              <option value="report_markdown_bundle">Report (markdown bundle)</option>
              <option value="report_pdf">Report (PDF)</option>
              <option value="source_pack">Source pack</option>
              <option value="deck_pdf">Deck (PDF)</option>
            </select>
          </label>
          <label className="block">
            <span className="text-xs font-medium text-slate-700">Description</span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </label>
          {build.isError && <p className="text-xs text-red-600">Failed to dispatch build.</p>}
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
              disabled={build.isPending || !title}
              className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-50"
            >
              {build.isPending ? "Dispatching…" : "Build"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
