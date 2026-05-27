import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { SourceUploadModal } from "@/components/SourceUploadModal";
import { api } from "@/lib/api";

interface SourceListItem {
  id: string;
  title: string;
  short_id: string;
  status: string;
  failure_reason: string | null;
  created_at: string;
}

interface Props {
  projectId: string;
  onOpenSource: (sourceId: string) => void;
}

export function SourcesTab({ projectId, onOpenSource }: Props) {
  const [showUpload, setShowUpload] = useState(false);
  const sources = useQuery<SourceListItem[]>({
    queryKey: ["sources", projectId],
    queryFn: () => api.get<SourceListItem[]>(`/v1/projects/${projectId}/sources`),
    refetchInterval: 5_000,
  });

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <h3 className="text-sm font-semibold text-slate-900">Sources</h3>
        <button
          type="button"
          onClick={() => setShowUpload(true)}
          className="rounded-md bg-slate-900 px-3 py-1 text-xs font-medium text-white hover:bg-slate-700"
        >
          Upload
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-4">
        {sources.isPending && (
          <p className="text-xs text-slate-500">Loading sources…</p>
        )}
        {sources.isSuccess && sources.data.length === 0 && (
          <p className="text-xs text-slate-500">
            No sources yet. Upload a PDF, markdown, DOCX, HTML, or EPUB to get
            started.
          </p>
        )}
        <ul className="space-y-2">
          {sources.data?.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                onClick={() => onOpenSource(s.id)}
                className="block w-full rounded-md border border-slate-200 px-3 py-2 text-left text-sm hover:border-slate-400"
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium text-slate-900">
                    {s.short_id} · {s.title}
                  </span>
                  <StatusBadge status={s.status} />
                </div>
                {s.failure_reason && (
                  <p className="mt-1 text-xs text-red-600">{s.failure_reason}</p>
                )}
              </button>
            </li>
          ))}
        </ul>
      </div>
      {showUpload && (
        <SourceUploadModal
          projectId={projectId}
          onClose={() => setShowUpload(false)}
          onUploaded={() => setShowUpload(false)}
        />
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const tone =
    status === "wiki_done"
      ? "bg-emerald-100 text-emerald-800"
      : status.includes("failed")
        ? "bg-red-100 text-red-800"
        : status === "indexed"
          ? "bg-sky-100 text-sky-800"
          : "bg-slate-100 text-slate-700";
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${tone}`}
    >
      {status}
    </span>
  );
}
