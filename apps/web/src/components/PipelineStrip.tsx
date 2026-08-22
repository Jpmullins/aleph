import { useQuery } from "@tanstack/react-query";

import { api, type PipelineOut } from "@/lib/api";

/**
 * Corpus-level progress: how far the whole source set has actually got.
 *
 * A source's journey — fetch → normalize → chunk+embed → wiki — runs entirely
 * in workers. Until now nothing surfaced it, so "is my library ready to ask
 * questions of?" could not be answered without reading container logs, and a
 * run that stalled after normalization looked identical to one that finished.
 *
 * Two deliberate choices:
 *
 * - **Stages are cumulative.** A source that reached `indexed` has necessarily
 *   been normalized. Counting each status exclusively made sources appear to
 *   *leave* earlier stages as they advanced, which reads as work being lost.
 * - **Failures are never folded in.** A failed source is shown as failed, not
 *   quietly dropped from the totals — a corpus that silently shrinks is the
 *   same class of failure as a join that silently returns nothing.
 */

const REFRESH_MS = 15_000;

export function PipelineStrip({ projectId }: { projectId: string }) {
  const pipeline = useQuery<PipelineOut>({
    queryKey: ["pipeline", projectId],
    queryFn: () => api.get<PipelineOut>(`/v1/projects/${projectId}/pipeline`),
    // Ingest is a background process; without polling the strip would show a
    // snapshot from whenever the workspace happened to mount.
    refetchInterval: REFRESH_MS,
  });

  const data = pipeline.data;
  if (!data || data.total === 0) return null;

  const first = data.stages[0]?.count ?? 0;
  return (
    <div
      className="flex items-center gap-2 overflow-x-auto px-3 py-1.5 text-[11px]"
      data-testid="pipeline-strip"
      aria-label="Corpus ingest progress"
    >
      {data.stages.map((stage, i) => {
        const done = first > 0 && stage.count === first;
        return (
          <div key={stage.key} className="flex shrink-0 items-center gap-2">
            {i > 0 && <span aria-hidden className="text-ink-muted">›</span>}
            <span
              className="flex items-center gap-1.5"
              title={`${stage.count} of ${first} sources have reached "${stage.label}"`}
              data-testid={`pipeline-stage-${stage.key}`}
            >
              <span
                aria-hidden
                className={
                  "inline-block h-1.5 w-1.5  " +
                  (stage.count === 0
                    ? "bg-line-strong"
                    : done
                      ? "bg-sunken"
                      : "bg-sunken")
                }
              />
              <span className="text-ink-muted">{stage.label}</span>
              <span className="font-mono tabular-nums text-ink-soft">{stage.count}</span>
            </span>
          </div>
        );
      })}
      {data.failed > 0 && (
        <span
          className="ml-1 shrink-0 bg-badge-failed-bg px-1.5 py-0.5 font-medium text-badge-failed-fg"
          title="Sources that failed and will not progress without intervention"
          data-testid="pipeline-failed"
        >
          {data.failed} failed
        </span>
      )}
    </div>
  );
}
