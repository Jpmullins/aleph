import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { useSurface } from "../surface-context";
import { api } from "@/lib/api";
import { CardShell, FeedbackButton, Pill, type RendererProps } from "./_shared";

interface ChartSpec {
  vega_lite_spec: Record<string, unknown>;
  rows: Array<Record<string, unknown>>;
}

export function ChartCard({ component }: RendererProps) {
  const { projectId, surface } = useSurface();
  const p = component.props as {
    dataset_version_id?: string | null;
    title?: string;
    vega_lite_spec?: Record<string, unknown>;
    chart_id?: string;
    _placeholder?: boolean;
  };
  const ref = useRef<HTMLDivElement>(null);

  // Fetch full chart spec + data rows when we only have an ID.
  const chartQuery = useQuery<ChartSpec>({
    queryKey: ["chart-spec", projectId, p.dataset_version_id],
    queryFn: () =>
      api.get<ChartSpec>(
        `/v1/projects/${projectId}/datasets/versions/${p.dataset_version_id}/chart-spec`,
      ),
    enabled: !!p.dataset_version_id && !p.vega_lite_spec,
  });

  const spec = p.vega_lite_spec ?? chartQuery.data?.vega_lite_spec;
  const rows = chartQuery.data?.rows;

  useEffect(() => {
    if (!ref.current || !spec) return;
    let disposed = false;
    void import("vega-embed").then(({ default: embed }) => {
      if (disposed || !ref.current) return;
      const fullSpec = rows
        ? { ...spec, data: { values: rows } }
        : spec;
      embed(ref.current, fullSpec as never, {
        actions: false,
        renderer: "canvas",
        config: {
          background: "transparent",
          axis: { labelFontSize: 10, titleFontSize: 11, labelColor: "#475569", titleColor: "#0f172a" },
          legend: { labelFontSize: 10, titleFontSize: 11 },
          view: { stroke: "transparent" },
        },
      }).catch((err: unknown) => {
        if (!disposed && ref.current) {
          ref.current.innerHTML = `<div class="text-xs text-red-700 p-2">Chart render failed: ${String(err)}</div>`;
        }
      });
    });
    return () => {
      disposed = true;
    };
  }, [spec, rows]);

  if (p._placeholder || (!p.dataset_version_id && !p.vega_lite_spec)) {
    return (
      <CardShell
        title={p.title || "Chart"}
        subtitle={<Pill tone="slate">No dataset bound</Pill>}
      >
        <p className="text-xs text-slate-500">
          Bind a DatasetVersion to this card to see the chart render.
        </p>
      </CardShell>
    );
  }

  return (
    <CardShell
      title={p.title || "Chart"}
      actions={
        p.chart_id ? (
          <FeedbackButton
            projectId={projectId}
            targetKind="chart"
            targetId={p.chart_id}
            surface={surface}
          />
        ) : null
      }
    >
      {chartQuery.isPending && !spec && (
        <p className="text-xs text-slate-400">Loading chart…</p>
      )}
      {chartQuery.isError && (
        <p className="text-xs text-red-700">Failed to load chart spec.</p>
      )}
      <div ref={ref} className="min-h-[180px] w-full" data-testid="chart-card-vega" />
    </CardShell>
  );
}
