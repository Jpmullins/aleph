import { CardShell, Pill, type RendererProps } from "./_shared";

export function MapCard({ component }: RendererProps) {
  const p = component.props as {
    title?: string;
    dataset_version_id?: string | null;
    geo_features?: unknown[];
  };
  return (
    <CardShell
      title={p.title || "Map"}
      subtitle={<Pill tone="slate">MapLibre — geo features land in Increment 6</Pill>}
    >
      <p className="text-xs text-slate-500">
        {p.geo_features?.length ?? 0} features available.
      </p>
    </CardShell>
  );
}
