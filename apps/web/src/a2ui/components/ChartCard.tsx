import { CardShell, Pill, type RendererProps } from "./_shared";

export function ChartCard({ component }: RendererProps) {
  const p = component.props as {
    dataset_version_id?: string | null;
    title?: string;
    vega_lite_spec?: Record<string, unknown>;
    _placeholder?: boolean;
  };
  if (p._placeholder || !p.dataset_version_id) {
    return (
      <CardShell
        title={p.title || "Chart"}
        subtitle={<Pill tone="slate">Datasets land in Increment 6</Pill>}
      >
        <p className="text-xs text-slate-500">
          This chart will render once a DatasetVersion is bound to it.
        </p>
      </CardShell>
    );
  }
  return (
    <CardShell title={p.title || "Chart"}>
      <pre className="overflow-x-auto rounded bg-slate-50 p-2 text-[10px] text-slate-700">
        {JSON.stringify(p.vega_lite_spec, null, 2)}
      </pre>
    </CardShell>
  );
}
