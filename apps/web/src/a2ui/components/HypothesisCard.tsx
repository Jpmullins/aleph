import { confidenceLabel, confidenceTone, isConfidence } from "../confidence";
import { CardShell, Pill, type RendererProps } from "./_shared";

export function HypothesisCard({ component, onAction }: RendererProps) {
  const p = component.props as {
    hypothesis_id: string;
    title: string;
    confidence?: string;
    evidence_count?: number;
  };
  // Was `<Pill tone="sky">{p.confidence ?? "initial"}</Pill>` — one colour for
  // every state, and a default of `initial`, a word the confidence engine has
  // never emitted. A hypothesis carries the same six states a claim does
  // (`Hypothesis.confidence` defaults to `under_investigation` and is written
  // only by `next_confidence_from_evidence`), so it gets the same badge.
  const confidence = p.confidence ?? "under_investigation";
  const known = isConfidence(confidence);
  return (
    <CardShell
      title={p.title}
      subtitle={
        <span className="flex items-center gap-2">
          <Pill tone={known ? confidenceTone(confidence) : "slate"}>
            {known ? confidenceLabel(confidence) : `? ${confidence}`}
          </Pill>
          <span className="text-xs text-ink-muted">{p.evidence_count ?? 0} evidence</span>
        </span>
      }
    >
      <button
        type="button"
        onClick={() =>
          onAction("open", { target_id: p.hypothesis_id, target_kind: "hypothesis" })
        }
        className="text-xs text-ink-muted hover:text-ink"
      >
        Open hypothesis →
      </button>
    </CardShell>
  );
}
