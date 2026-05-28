import { useMemo } from "react";

import { CardShell, Pill, type RendererProps } from "./_shared";

interface Node {
  id: string;
  label: string;
  group?: string;
}
interface Edge {
  source: string;
  target: string;
  label?: string;
}

export function GraphCard({ component }: RendererProps) {
  const p = component.props as {
    title?: string;
    nodes?: Node[];
    edges?: Edge[];
    _placeholder?: boolean;
  };

  const layout = useMemo(() => {
    if (!p.nodes || p.nodes.length === 0) return null;
    const cols = Math.max(2, Math.ceil(Math.sqrt(p.nodes.length)));
    return p.nodes.map((n, i) => ({
      id: n.id,
      label: n.label,
      group: n.group ?? "default",
      x: 40 + (i % cols) * 130,
      y: 40 + Math.floor(i / cols) * 90,
    }));
  }, [p.nodes]);

  if (p._placeholder || !p.nodes || !layout) {
    return (
      <CardShell title={p.title || "Graph"} subtitle={<Pill tone="slate">No graph data</Pill>}>
        <p className="text-xs text-slate-500">Provide `nodes` + `edges` arrays to render.</p>
      </CardShell>
    );
  }

  const byId = new Map(layout.map((n) => [n.id, n]));
  const w = Math.max(400, Math.ceil(Math.sqrt(layout.length)) * 130 + 40);
  const h = Math.max(200, Math.ceil(layout.length / Math.ceil(Math.sqrt(layout.length))) * 90 + 40);

  return (
    <CardShell title={p.title || "Graph"} subtitle={`${layout.length} nodes, ${p.edges?.length ?? 0} edges`}>
      <div className="overflow-auto rounded bg-slate-50 p-2" data-testid="graph-card">
        <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="block">
          {(p.edges ?? []).map((e, i) => {
            const a = byId.get(e.source);
            const b = byId.get(e.target);
            if (!a || !b) return null;
            return (
              <g key={i}>
                <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="#94a3b8" strokeWidth={1} />
                {e.label && (
                  <text
                    x={(a.x + b.x) / 2}
                    y={(a.y + b.y) / 2 - 4}
                    fontSize={9}
                    fill="#64748b"
                    textAnchor="middle"
                  >
                    {e.label}
                  </text>
                )}
              </g>
            );
          })}
          {layout.map((n) => (
            <g key={n.id} transform={`translate(${n.x},${n.y})`}>
              <circle r={18} fill="#fff" stroke="#0f172a" strokeWidth={1.5} />
              <text fontSize={10} fill="#0f172a" textAnchor="middle" dy={32}>
                {n.label}
              </text>
            </g>
          ))}
        </svg>
      </div>
    </CardShell>
  );
}
