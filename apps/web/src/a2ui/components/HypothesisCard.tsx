import { CardShell, Pill, type RendererProps } from "./_shared";

export function HypothesisCard({ component, onAction }: RendererProps) {
  const p = component.props as {
    hypothesis_id: string;
    title: string;
    confidence?: string;
    evidence_count?: number;
  };
  return (
    <CardShell
      title={p.title}
      subtitle={
        <span className="flex items-center gap-2">
          <Pill tone="sky">{p.confidence ?? "initial"}</Pill>
          <span className="text-xs text-slate-500">
            {p.evidence_count ?? 0} evidence
          </span>
        </span>
      }
    >
      <button
        type="button"
        onClick={() =>
          onAction("open", { target_id: p.hypothesis_id, target_kind: "hypothesis" })
        }
        className="text-xs text-slate-500 hover:text-slate-900"
      >
        Open hypothesis →
      </button>
    </CardShell>
  );
}
