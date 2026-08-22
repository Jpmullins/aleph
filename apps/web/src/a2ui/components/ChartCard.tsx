import { useEffect, useMemo, useRef } from "react";

import { readToken, useThemeEpoch } from "../../lib/theme-tokens";
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
  // Vega paints onto a canvas, so the axis colours must be resolved literals —
  // it cannot read `var(--text-secondary)`. Re-embedding on a theme change is
  // the other half: a chart that resolves the token once is correct until
  // somebody uses the theme toggle, and then wrong with no way to notice.
  const themeEpoch = useThemeEpoch();

  // Memoised because it is the embed effect's dependency. Rebuilt inline, this
  // object had a new identity on every render, so the effect fired on every
  // render too — a full Vega compile and canvas repaint each time the parent
  // re-rendered for any reason. It also made `themeEpoch` below decorative:
  // the redraw on a theme change happened by accident, and would have kept
  // happening after somebody deleted the dependency.
  const spec = p.vega_lite_spec;
  const source: Record<string, unknown> | null = useMemo(() => {
    if (!spec || Object.keys(spec).length === 0) return null;
    return {
      ...spec,
      // Normalize to the installed Vega-Lite major version.
      $schema: "https://vega.github.io/schema/vega-lite/v6.json",
    };
  }, [spec]);

  useEffect(() => {
    if (!ref.current || source === null) return;
    let disposed = false;
    void import("vega-embed").then(({ default: embed }) => {
      if (disposed || !ref.current) return;
      // A token that resolves to nothing means tokens.css did not load. Omit
      // the key rather than substituting a literal: Vega's own default is at
      // least a stated default, where a hardcoded hex is a second palette that
      // only appears when the first one fails.
      const axis: Record<string, unknown> = { labelFontSize: 10, titleFontSize: 11 };
      const labelColor = readToken("--text-secondary");
      const titleColor = readToken("--text-primary");
      if (labelColor) axis.labelColor = labelColor;
      if (titleColor) axis.titleColor = titleColor;
      embed(ref.current, source as never, {
        actions: false,
        renderer: "canvas",
        // Amended rule 8: reject every remote data/image URL (no network from
        // an agent-authored spec rendered in the app origin).
        loader: _NO_NET_LOADER,
        config: {
          background: "transparent",
          axis,
          legend: { labelFontSize: 10, titleFontSize: 11 },
          view: { stroke: "transparent" },
        },
      }).catch((err: unknown) => {
        if (!disposed && ref.current) {
          ref.current.innerHTML = `<div class="text-xs text-bad p-2">Chart render failed: ${String(err)}</div>`;
        }
      });
    });
    return () => {
      disposed = true;
    };
  }, [source, themeEpoch]);

  if (source === null) {
    return (
      <CardShell title={p.title || "Chart"} subtitle={<Pill tone="neutral">No chart data</Pill>}>
        <p className="text-xs text-ink-muted">
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
