import { useEffect, useRef } from "react";

import { useSurface } from "../surface-context";
import { CardShell, FeedbackButton, Pill, type RendererProps } from "./_shared";

// Amended rule 8: an agent-authored Vega spec renders inline in the app origin,
// so it must NOT pull any remote data/image URL (ambient-auth same-origin fetch
// or external egress). The custom loader rejects every network resource; only
// inline `values` data (the code_runner path) renders. A persisted spec must be
// inlined by the producer — ChartCard never fetches (no-self-fetch rule).
const _NET_BLOCKED = "network disabled for agent charts";
function _rejectStr(uri: string): Promise<string> {
  return Promise.reject(new Error(`${_NET_BLOCKED} (blocked: ${uri})`));
}
// A full vega Loader (load/sanitize/http/file) that rejects every resource.
const _NO_NET_LOADER = {
  load: _rejectStr,
  http: _rejectStr,
  file: _rejectStr,
  sanitize: (uri: string): Promise<{ href: string }> =>
    Promise.reject(new Error(`${_NET_BLOCKED} (blocked: ${uri})`)),
};

/**
 * WP-4c rebuild — NO self-fetch. The chart renders ONLY from a BOUND inline
 * `vega_lite_spec` (the code_runner returns the spec and the pin embeds it
 * inline, with inline `values` data). No `useQuery`, no URL loading, no
 * `fetch` — a persisted spec must be inlined by the producer (or shown as an
 * ImageCard/HtmlFrameCard artifact). The vega loader rejects all network so a
 * spec carrying a remote `data.url`/image cannot fetch from the app origin
 * (amended rule 8).
 */
export function ChartCard({ component, onAction }: RendererProps) {
  const { surface } = useSurface();
  const p = component.props as {
    title?: string;
    vega_lite_spec?: Record<string, unknown>;
    chart_id?: string;
  };
  const ref = useRef<HTMLDivElement>(null);

  const hasInlineSpec = !!p.vega_lite_spec && Object.keys(p.vega_lite_spec).length > 0;
  const source: Record<string, unknown> | null = hasInlineSpec
    ? {
        ...(p.vega_lite_spec as Record<string, unknown>),
        // Normalize to the installed Vega-Lite major version.
        $schema: "https://vega.github.io/schema/vega-lite/v6.json",
      }
    : null;

  useEffect(() => {
    if (!ref.current || source === null) return;
    let disposed = false;
    void import("vega-embed").then(({ default: embed }) => {
      if (disposed || !ref.current) return;
      embed(ref.current, source as never, {
        actions: false,
        renderer: "canvas",
        // Amended rule 8: reject every remote data/image URL (no network from
        // an agent-authored spec rendered in the app origin).
        loader: _NO_NET_LOADER,
        config: {
          background: "transparent",
          axis: {
            labelFontSize: 10,
            titleFontSize: 11,
            labelColor: "#475569",
            titleColor: "#0f172a",
          },
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
  }, [source]);

  if (source === null) {
    return (
      <CardShell title={p.title || "Chart"} subtitle={<Pill tone="slate">No chart data</Pill>}>
        <p className="text-xs text-slate-500">
          This chart has no bound spec. An agent produces one via the sandbox
          (code_runner) or supplies an inline Vega-Lite spec.
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
            onAction={onAction}
            targetKind="chart"
            targetId={p.chart_id}
            surface={surface}
          />
        ) : null
      }
    >
      <div ref={ref} className="min-h-[180px] w-full" data-testid="chart-card-vega" />
    </CardShell>
  );
}
