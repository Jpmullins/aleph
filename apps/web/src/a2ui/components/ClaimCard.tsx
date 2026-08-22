import { confidenceLabel, confidenceTone, isConfidence } from "../confidence";
import { useSurface } from "../surface-context";
import { CardShell, FeedbackButton, Pill, type RendererProps } from "./_shared";

export function ClaimCard({ component, onAction }: RendererProps) {
  const { surface } = useSurface();
  const p = component.props as {
    claim_id: string;
    text: string;
    confidence: string;
    citations?: Array<{ marker: string; source_short_id?: string | null }>;
  };
  // Three of the six states the engine can emit had no branch here and fell
  // through to slate — including `refuted`, so a claim the evidence had
  // disproved looked exactly like one nobody had assessed. `confidenceTone` is
  // exhaustive over the union and fails the build if a state is added without
  // a colour; the `isConfidence` guard is for the wire, where the value is a
  // plain string and can predate the vocabulary being unified.
  const confidence = p.confidence;
  const known = isConfidence(confidence);
  const tone = known ? confidenceTone(confidence) : "slate";
  return (
    <CardShell
      subtitle={
        <Pill tone={tone}>{known ? confidenceLabel(confidence) : `? ${confidence}`}</Pill>
      }
      actions={
        <FeedbackButton
          onAction={onAction}
          targetKind="claim"
          targetId={p.claim_id}
          surface={surface}
        />
      }
    >
      <p className="text-sm text-ink-soft">{p.text}</p>
      {p.citations && p.citations.length > 0 && (
        <div className="mt-2 text-xs text-ink-muted">
          {p.citations.map((c) => (
            <span
              key={c.marker}
              className="mr-1 inline-flex rounded bg-amber-50 px-1 py-0.5 text-amber-900"
              title={c.source_short_id ? `from ${c.source_short_id}` : undefined}
            >
              {c.marker}
            </span>
          ))}
        </div>
      )}
      <button
        type="button"
        onClick={() => onAction("open", { target_id: p.claim_id, target_kind: "claim" })}
        className="mt-2 text-xs text-ink-muted hover:text-ink"
      >
        Open claim →
      </button>
    </CardShell>
  );
}
