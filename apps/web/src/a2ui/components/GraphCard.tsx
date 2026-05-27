import { CardShell, Pill, type RendererProps } from "./_shared";

export function GraphCard({ component }: RendererProps) {
  const p = component.props as {
    title?: string;
    dataset_version_id?: string | null;
    nodes?: unknown[];
    edges?: unknown[];
  };
  return (
    <CardShell
      title={p.title || "Graph"}
      subtitle={<Pill tone="slate">React Flow — graph layout lands in Increment 6</Pill>}
    >
      <p className="text-xs text-slate-500">
        {p.nodes?.length ?? 0} nodes · {p.edges?.length ?? 0} edges
      </p>
    </CardShell>
  );
}
