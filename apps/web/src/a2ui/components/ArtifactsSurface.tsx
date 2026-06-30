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

interface SourceOut {
  id: string;
  short_id: string;
  title: string;
  connector_kind: string;
  url: string | null;
  status: string;
  created_at: string;
}

const KIND_TONE: Record<string, "sky" | "emerald" | "amber" | "slate"> = {
  report_pdf: "sky",
  report_docx: "sky",
  report_markdown_bundle: "sky",
  source_pack: "emerald",
  deck_pdf: "amber",
};

/**
 * The Library tab (formerly "Artifacts"): ingested **Sources** (raw PDFs /
 * webpages / documents — viewable in their own card) and built **Artifacts**
 * (reports / decks / source-packs).
 */
export function ArtifactsSurface(_: RendererProps) {
  const { projectId } = useSurface();
  const qc = useQueryClient();
  const [showBuild, setShowBuild] = useState(false);
  const [viewing, setViewing] = useState<SourceOut | null>(null);

  const sources = useQuery<SourceOut[]>({
    queryKey: ["sources", projectId],
    queryFn: () => api.get<SourceOut[]>(`/v1/projects/${projectId}/sources`),
    refetchInterval: 5_000,
  });
  const artifacts = useQuery<ArtifactOut[]>({
    queryKey: ["artifacts", projectId],
    queryFn: () => api.get<ArtifactOut[]>(`/v1/projects/${projectId}/artifacts`),
    refetchInterval: 5_000,
  });

  return (
    <div className="flex h-full flex-col">
      <SurfaceHeader
        title="Library"
        subtitle={
          sources.data || artifacts.data
            ? `${sources.data?.length ?? 0} sources · ${artifacts.data?.length ?? 0} artifacts`
            : undefined
        }
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
      <div className="flex-1 space-y-4 overflow-y-auto p-3">
        <section data-testid="library-sources">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Sources
          </h3>
          {sources.isPending && <p className="text-sm text-slate-400">Loading…</p>}
          {sources.isSuccess && sources.data.length === 0 && (
            <div className="rounded-lg border border-dashed border-slate-300 p-4 text-center text-xs text-slate-500">
              No sources yet. Upload a document or ingest a URL, or run research.
            </div>
          )}
          {sources.data?.map((s) => (
            <SourceRow key={s.id} s={s} onView={() => setViewing(s)} />
          ))}
        </section>

        <section data-testid="library-artifacts">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Artifacts
          </h3>
          {artifacts.isSuccess && artifacts.data.length === 0 && (
            <div className="rounded-lg border border-dashed border-slate-300 p-4 text-center text-xs text-slate-500">
              No artifacts yet. Click <strong>+ Build</strong> to compose a report from the wiki.
            </div>
          )}
          {artifacts.data?.map((a) => (
            <ArtifactRow key={a.id} a={a} projectId={projectId} />
          ))}
        </section>
      </div>
      {viewing && (
        <SourceViewer projectId={projectId} source={viewing} onClose={() => setViewing(null)} />
      )}
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

function SourceRow({ s, onView }: { s: SourceOut; onView: () => void }) {
  const ready = s.status === "indexed" || s.status === "normalized";
  return (
    <CardShell
      title={`${s.short_id} · ${s.title}`}
      subtitle={
        <span className="flex items-center gap-2">
          <Pill tone="slate">{s.connector_kind}</Pill>
          <Pill tone={ready ? "emerald" : s.status === "failed" ? "amber" : "slate"}>
            {s.status}
          </Pill>
        </span>
      }
      footer={`ingested ${new Date(s.created_at).toLocaleString()}`}
    >
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={onView}
          data-testid="source-view"
          className="text-xs font-medium text-slate-700 hover:text-slate-900"
        >
          Open ↗
        </button>
        {s.url && (
          <a
            href={s.url}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-slate-500 hover:text-slate-900"
          >
            Original link
          </a>
        )}
      </div>
    </CardShell>
  );
}

/** Renders a raw source asset in its own card: PDF / webpage / document via the
 * presigned object URL (browsers render PDF + HTML inline in an iframe), with a
 * fallback to the normalized text. */
function SourceViewer({
  projectId,
  source,
  onClose,
}: {
  projectId: string;
  source: SourceOut;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<"raw" | "text">("raw");
  const asset = useQuery<{ url: string; ttl_seconds: number }>({
    queryKey: ["source-asset", projectId, source.id],
    queryFn: () => api.get(`/v1/projects/${projectId}/sources/${source.id}/asset`),
    enabled: tab === "raw",
  });
  const normalized = useQuery<{ markdown: string }>({
    queryKey: ["source-normalized", projectId, source.id],
    queryFn: () => api.get(`/v1/projects/${projectId}/sources/${source.id}/normalized`),
    enabled: tab === "text",
  });
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
      <div
        className="flex h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-lg bg-white shadow-xl"
        data-testid="source-viewer"
      >
        <div className="flex items-center gap-3 border-b border-slate-200 p-3">
          <h3 className="truncate text-sm font-semibold">{source.title}</h3>
          <div className="ml-auto flex gap-1 text-xs">
            <button
              type="button"
              onClick={() => setTab("raw")}
              className={`rounded px-2 py-1 ${tab === "raw" ? "bg-slate-900 text-white" : "text-slate-500"}`}
            >
              Raw
            </button>
            <button
              type="button"
              onClick={() => setTab("text")}
              className={`rounded px-2 py-1 ${tab === "text" ? "bg-slate-900 text-white" : "text-slate-500"}`}
            >
              Text
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded px-2 py-1 text-slate-500 hover:text-slate-900"
            >
              ✕
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-auto bg-slate-50">
          {tab === "raw" &&
            (asset.isPending ? (
              <p className="p-4 text-sm text-slate-400">Loading asset…</p>
            ) : asset.data ? (
              <iframe
                title={source.title}
                src={asset.data.url}
                className="h-full w-full"
                data-testid="source-viewer-frame"
              />
            ) : (
              <p className="p-4 text-sm text-slate-500">No raw asset available for this source.</p>
            ))}
          {tab === "text" &&
            (normalized.isPending ? (
              <p className="p-4 text-sm text-slate-400">Loading text…</p>
            ) : (
              <pre className="whitespace-pre-wrap p-4 text-xs text-slate-700">
                {normalized.data?.markdown ?? "No normalized text available yet."}
              </pre>
            ))}
        </div>
      </div>
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
