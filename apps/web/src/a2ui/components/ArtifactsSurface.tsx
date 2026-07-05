import { useState } from "react";

import { apiUrl } from "@/lib/api";
import { useSurface } from "../surface-context";
import { CardShell, Pill, SurfaceHeader, type RendererProps } from "./_shared";

interface ArtifactOut {
  id: string;
  short_id: string;
  title: string;
  artifact_kind: string;
  description: string;
  current_version_id: string | null;
  created_at: string;
  drifted?: boolean;
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
 * WP-4: the Library tab (ingested **Sources** + built **Artifacts**) renders
 * ONLY from the surface data model (`{sources, artifacts}`) — no `useQuery`, no
 * polling. Raw sources open in an authenticated asset iframe (a URL, not a
 * fetch). The normalized-text preview (SourceCard) and the "+ Build" flow are
 * WP-4b/e; they are intentionally not re-introduced as component fetches here.
 */
export function ArtifactsSurface({ component }: RendererProps) {
  const { projectId } = useSurface();
  const sources = (component.props.sources as SourceOut[] | undefined) ?? [];
  const artifacts = (component.props.artifacts as ArtifactOut[] | undefined) ?? [];
  const [viewing, setViewing] = useState<SourceOut | null>(null);

  return (
    <div className="flex h-full flex-col">
      <SurfaceHeader
        title="Library"
        subtitle={`${sources.length} sources · ${artifacts.length} artifacts`}
      />
      <div className="flex-1 space-y-4 overflow-y-auto p-3">
        <section data-testid="library-sources">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Sources
          </h3>
          {sources.length === 0 && (
            <div className="rounded-lg border border-dashed border-slate-300 p-4 text-center text-xs text-slate-500">
              No sources yet. Upload a document or ingest a URL, or run research.
            </div>
          )}
          {sources.map((s) => (
            <SourceRow key={s.id} s={s} onView={() => setViewing(s)} />
          ))}
        </section>

        <section data-testid="library-artifacts">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Artifacts
          </h3>
          {artifacts.length === 0 && (
            <div className="rounded-lg border border-dashed border-slate-300 p-4 text-center text-xs text-slate-500">
              No artifacts yet.
            </div>
          )}
          {artifacts.map((a) => (
            <ArtifactRow key={a.id} a={a} projectId={projectId} />
          ))}
        </section>
      </div>
      {viewing && (
        <SourceViewer projectId={projectId} source={viewing} onClose={() => setViewing(null)} />
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

/** Renders a raw source asset in its own card via the authenticated asset
 * streaming route (browsers render PDF + HTML inline in an iframe). The `src`
 * is a URL, not a fetch. */
function SourceViewer({
  projectId,
  source,
  onClose,
}: {
  projectId: string;
  source: SourceOut;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4">
      <div
        className="flex h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-lg bg-white shadow-xl"
        data-testid="source-viewer"
      >
        <div className="flex items-center gap-3 border-b border-slate-200 p-3">
          <h3 className="truncate text-sm font-semibold">{source.title}</h3>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded px-2 py-1 text-slate-500 hover:text-slate-900"
          >
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto bg-slate-50">
          <iframe
            title={source.title}
            src={apiUrl(`/v1/projects/${projectId}/assets/source/${source.id}`)}
            className="h-full w-full"
            data-testid="source-viewer-frame"
          />
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
          {a.drifted && (
            <Pill tone="amber">
              <span data-testid={`artifact-drifted-${a.id}`}>drifted</span>
            </Pill>
          )}
        </span>
      }
      footer={`created ${new Date(a.created_at).toLocaleString()}`}
    >
      {a.description && <p className="text-xs text-slate-600">{a.description}</p>}
      {ready && (
        <a
          href={apiUrl(`/v1/projects/${projectId}/assets/artifact-version/${a.current_version_id}`)}
          download
          className="mt-1 inline-block text-xs font-medium text-slate-700 hover:text-slate-900"
        >
          ⬇ Download latest
        </a>
      )}
    </CardShell>
  );
}
