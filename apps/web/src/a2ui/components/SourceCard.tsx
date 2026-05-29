import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { useSurface } from "../register";
import { WikiBodyMarkdown } from "@/components/WikiBodyMarkdown";
import { api } from "@/lib/api";
import { CardShell, FeedbackButton, Pill, type RendererProps } from "./_shared";

interface NormalizedOut {
  markdown: string;
  char_count: number;
  parser: string;
}

function SourceReader({ projectId, sourceId }: { projectId: string; sourceId: string }) {
  const q = useQuery<NormalizedOut>({
    queryKey: ["source-normalized", projectId, sourceId],
    queryFn: () =>
      api.get<NormalizedOut>(`/v1/projects/${projectId}/sources/${sourceId}/normalized`),
  });
  if (q.isPending) return <p className="mt-2 text-xs text-slate-400">Loading source…</p>;
  if (q.isError)
    return (
      <p className="mt-2 text-xs text-slate-400">
        Source text isn’t available yet (still normalizing).
      </p>
    );
  return (
    <div className="mt-2 max-h-[28rem] overflow-y-auto rounded-md border border-[var(--border-muted,#e2e8f0)] bg-[var(--surface-sunken,#f8fafc)] p-3">
      <WikiBodyMarkdown body={q.data.markdown} />
    </div>
  );
}

export function SourceCard({ component, onAction }: RendererProps) {
  const { projectId, surface } = useSurface();
  const [reading, setReading] = useState(false);
  const p = component.props as {
    source_id: string;
    short_id: string;
    title: string;
    url?: string | null;
    status: string;
  };
  const tone =
    p.status === "wiki_done"
      ? "emerald"
      : p.status.includes("failed")
        ? "red"
        : p.status === "indexed"
          ? "sky"
          : "slate";
  return (
    <CardShell
      title={`${p.short_id} · ${p.title}`}
      subtitle={
        <span className="flex items-center gap-2">
          <Pill tone={tone}>{p.status}</Pill>
          {p.url && (
            <a
              href={p.url}
              target="_blank"
              rel="noreferrer"
              className="truncate text-xs text-slate-500 hover:text-slate-900"
            >
              {p.url}
            </a>
          )}
        </span>
      }
      actions={
        <FeedbackButton
          projectId={projectId}
          targetKind="source"
          targetId={p.source_id}
          surface={surface}
        />
      }
    >
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => onAction("open", { target_id: p.source_id, target_kind: "source" })}
          className="text-xs text-slate-500 hover:text-slate-900"
        >
          Open source
        </button>
        <button
          type="button"
          onClick={() =>
            onAction("navigate_wiki", { page_id: p.source_id })
          }
          className="text-xs text-slate-500 hover:text-slate-900"
        >
          Open source page
        </button>
        <button
          type="button"
          onClick={() => setReading((r) => !r)}
          className="ml-auto text-xs font-medium text-[var(--accent,#f97316)] hover:opacity-80"
          data-testid={`source-read-${p.source_id}`}
        >
          {reading ? "Hide text ▲" : "Read ▾"}
        </button>
      </div>
      {reading && <SourceReader projectId={projectId} sourceId={p.source_id} />}
    </CardShell>
  );
}
